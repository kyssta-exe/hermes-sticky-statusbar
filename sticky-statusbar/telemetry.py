"""Rolling agent telemetry for the sticky status board.

Every number comes from a **documented Hermes plugin hook** — no core
internals, no private agent attributes, no monkey-patching:

* ``pre_api_request`` / ``post_api_request`` — per-request usage, latency and
  context window, plus an in-flight marker so "thinking" shows before the first
  usage row lands.
* ``api_request_error`` — provider failures, so the board can count them.
* ``pre_tool_call`` / ``post_tool_call`` — what the agent is doing right now,
  and a short ledger of the calls that just finished (name, duration, ok/error).
* ``on_session_end`` — the turn is over; the board stops claiming to work and
  the publisher gets its chance to repost (``mode: bump``).

Thread-safety: hooks fire on the agent thread while the publisher runs on the
platform's event loop, so every read and write goes through one lock and the
publisher only ever sees an immutable snapshot.

Throughput and cache-hit maths mirror the Hermes status bar on purpose, so the
sticky board and the desktop bottom bar agree:

* throughput is ``sum(output) / sum(duration)`` over the window — true
  throughput, not a mean of per-request ratios;
* cache hit is ``cache_read / prompt`` where
  ``prompt = input + cache_read + cache_write``;
* a zero-read window reports ``None`` rather than an alarming ``0%`` — no data
  is not the same thing as a bad cache regime.

Throughput, latency and tool durations only aggregate samples with a usable
duration. A provider that reports a zero or negative ``api_duration`` would
otherwise put its output tokens in the numerator with nothing in the
denominator and inflate tokens/sec without bound, so that sample is dropped from
those figures while cache and context still see it.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

DEFAULT_WINDOW = 12

# A turn is considered live only while activity is recent. The hooks normally
# clear it in ``on_session_end``, but a crashed session, a gateway restart or an
# interrupted turn never fires that — without a lease the board would claim
# "working" forever. Two minutes of silence ends the turn.
TURN_LEASE_SECONDS = 120

# A prompt-token drop this large between consecutive requests means the context
# was rewritten (compression) rather than the model simply saying less.
_COMPRESSION_DROP_RATIO = 0.5
_COMPRESSION_MIN_PROMPT = 1000

_STATE_THINKING = "thinking"
_STATE_TOOL = "tool"
_STATE_WORKING = "working"
_STATE_IDLE = "idle"

# Argument keys worth echoing beside a tool name, most specific first. A status
# line is not a transcript: one short identifier is all that fits.
_HINT_KEYS = ("command", "path", "file_path", "pattern", "url", "query", "name", "skill")


def _int(value: Any) -> int:
    """Coerce to a non-negative int; anything unusable becomes 0."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _float(value: Any) -> float:
    """Coerce to a finite non-negative float.

    Providers do emit impossible timings (a negative ``api_duration`` has been
    seen in the wild); the status bar drops them and so do we.
    """
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if number != number or number < 0 or number > 1e6:  # NaN / negative / absurd
        return 0.0
    return number


def _clean(value: Any) -> str:
    """Strip and return a non-empty string, else ``""``."""
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _int_or_none(value: Any) -> int | None:
    """``_int`` that keeps "absent" distinguishable from "zero"."""
    if value is None or value == "":
        return None
    return _int(value)


def hint_for(args: Any) -> str:
    """A short identifier from a tool's arguments (``""`` when nothing fits).

    Reads a dict only — providers have been seen to hand back a list, a string
    or ``None`` for ``args``, and indexing that blindly is how a hook callback
    starts raising and silently stops recording.
    """
    if not isinstance(args, dict):
        return ""
    for key in _HINT_KEYS:
        value = args.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)[:40]
        text = _clean(value)
        if not text:
            continue
        text = text.splitlines()[0].strip()
        if key in {"path", "file_path"}:
            text = text.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        return text[:40]
    return ""


@dataclass(frozen=True)
class ToolRun:
    """One finished tool call, reduced to what a status line can show."""

    name: str = ""
    ok: bool = True
    duration_s: float = 0.0
    ended_at: float = 0.0
    hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Snapshot:
    """One immutable read of the collector. Every field is JSON-safe."""

    model: str = ""
    provider: str = ""
    platform: str = ""
    session_id: str = ""
    turn_live: bool = False
    inflight: bool = False
    current_tool: str = ""
    current_tool_hint: str = ""
    current_tool_started: float = 0.0

    requests: int = 0          # requests inside the rolling window
    api_calls: int = 0         # lifetime API calls observed this process
    errors: int = 0            # lifetime provider errors observed
    turns: int = 0             # turns finished this process
    failed_turns: int = 0

    tps: float = 0.0           # windowed throughput: sum(output) / sum(duration)
    instant_tps: float = 0.0   # the single most recent request
    cache_hit_pct: float | None = None
    context_used: int = 0
    context_length: int = 0
    context_pct: float | None = None
    latency: float = 0.0       # mean request duration over the window

    output_tokens: int = 0     # windowed output tokens
    total_tokens: int = 0      # lifetime prompt + completion tokens
    compressions: int = 0      # estimated context rewrites (see module docstring)

    tools: tuple[ToolRun, ...] = ()
    tool_calls: int = 0        # lifetime tool calls observed this process
    tool_errors: int = 0

    turn_started: float = 0.0
    turn_ended: float = 0.0
    last_activity: float = 0.0
    started_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["tools"] = [run.as_dict() for run in self.tools]
        return data

    # ── derived state ────────────────────────────────────────────────────────

    def busy_at(self, now: float | None = None) -> bool:
        """Whether the agent is working *right now*.

        ``turn_live`` is a lease, not a latch: it is set by the hooks and cleared
        two minutes after the last activity, so a session that dies without
        firing ``on_session_end`` cannot leave the board stuck on "working".
        """
        if self.inflight or self.current_tool:
            return True
        if not self.turn_live:
            return False
        return self.idle_seconds(now) < TURN_LEASE_SECONDS

    @property
    def busy(self) -> bool:
        """:meth:`busy_at` at wall-clock now."""
        return self.busy_at(None)

    def idle_seconds(self, now: float | None = None) -> float:
        """Seconds since the last provider activity (0.0 before any)."""
        if not self.last_activity:
            return 0.0
        return max(0.0, (now if now is not None else time.time()) - self.last_activity)

    def state_at(self, now: float | None = None) -> str:
        """``thinking`` / ``tool`` / ``working`` / ``idle`` for the header line."""
        if self.current_tool:
            return _STATE_TOOL
        if self.inflight:
            return _STATE_THINKING
        if self.busy_at(now):
            return _STATE_WORKING
        return _STATE_IDLE

    @property
    def state(self) -> str:
        """:meth:`state_at` at wall-clock now."""
        return self.state_at(None)

    def elapsed_seconds(self, now: float | None = None) -> float:
        """How long the current turn has been running (0.0 when idle)."""
        if not self.turn_started or not self.busy_at(now):
            return 0.0
        current = now if now is not None else time.time()
        return max(0.0, current - self.turn_started)


@dataclass
class _Request:
    """One provider request, reduced to what the board needs."""

    ended_at: float
    duration: float
    output_tokens: int
    prompt_tokens: int
    cache_read_tokens: int
    context_length: int


@dataclass
class _PendingTool:
    """A tool call that has started and not yet reported back."""

    name: str
    hint: str
    started_at: float


class MetricsCollector:
    """Thread-safe rolling window over live agent telemetry."""

    def __init__(self, window: int = DEFAULT_WINDOW, tool_history: int = 5) -> None:
        self._lock = threading.Lock()
        self._window_size = max(1, int(window or DEFAULT_WINDOW))
        self._tool_history = max(0, int(tool_history or 0))
        self._requests: deque[_Request] = deque(maxlen=self._window_size)
        self._tools: deque[ToolRun] = deque(maxlen=max(1, self._tool_history))
        self._pending: dict[str, _PendingTool] = {}
        self._reset_locked()

    # ── internals ────────────────────────────────────────────────────────────

    def _reset_locked(self) -> None:
        self._requests.clear()
        self._tools.clear()
        self._pending.clear()
        self._model = ""
        self._provider = ""
        self._platform = ""
        self._session_id = ""
        self._turn_live = False
        self._inflight = False
        self._current_tool = ""
        self._context_length = 0
        self._api_calls = 0
        self._errors = 0
        self._turns = 0
        self._failed_turns = 0
        self._tool_calls = 0
        self._tool_errors = 0
        self._total_tokens = 0
        self._compressions = 0
        self._last_prompt_tokens = 0
        self._last_activity = 0.0
        self._turn_started = 0.0
        self._turn_ended = 0.0
        self._started_at = time.time()

    def reset(self) -> None:
        """Drop every counter (used by tests and by an explicit refresh)."""
        with self._lock:
            self._reset_locked()

    def set_tool_history(self, size: int) -> None:
        """Resize the tool ledger (a settings change must not need a restart)."""
        with self._lock:
            self._tool_history = max(0, int(size or 0))
            self._tools = deque(self._tools, maxlen=max(1, self._tool_history))

    # ── hook entry points ────────────────────────────────────────────────────

    def on_turn_start(self, **kwargs: Any) -> None:
        """A user message was admitted — a turn is about to start."""
        now = time.time()
        with self._lock:
            if not self._turn_live:
                self._turn_started = now
            self._turn_live = True
            self._last_activity = now
            self._platform = _clean(kwargs.get("platform")) or self._platform

    def on_pre_api_request(self, **kwargs: Any) -> None:
        """Mark a provider request in flight and remember the context window."""
        context_length = _int_or_none(kwargs.get("context_length"))
        now = time.time()
        with self._lock:
            self._inflight = True
            self._last_activity = now
            if not self._turn_started:
                self._turn_started = now
            if context_length:
                self._context_length = context_length
            self._model = _clean(kwargs.get("model")) or self._model
            self._provider = _clean(kwargs.get("provider")) or self._provider
            self._platform = _clean(kwargs.get("platform")) or self._platform
            self._session_id = _clean(kwargs.get("session_id")) or self._session_id

    def on_post_api_request(self, **kwargs: Any) -> None:
        """Record one completed provider request."""
        raw_usage = kwargs.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        duration = _float(kwargs.get("api_duration"))
        context_length = _int(kwargs.get("context_length"))
        prompt_tokens = _int(usage.get("prompt_tokens"))
        if not prompt_tokens:
            prompt_tokens = (
                _int(usage.get("input_tokens"))
                + _int(usage.get("cache_read_tokens"))
                + _int(usage.get("cache_write_tokens"))
            )
        record = _Request(
            ended_at=time.time(),
            duration=duration,
            output_tokens=_int(usage.get("output_tokens")),
            prompt_tokens=prompt_tokens,
            cache_read_tokens=_int(usage.get("cache_read_tokens")),
            context_length=context_length,
        )
        with self._lock:
            self._inflight = False
            self._turn_live = True
            self._api_calls += 1
            self._last_activity = record.ended_at
            self._total_tokens += _int(usage.get("total_tokens")) or (
                prompt_tokens + record.output_tokens
            )
            if record.context_length:
                self._context_length = record.context_length
            self._model = _clean(kwargs.get("model")) or self._model
            self._provider = _clean(kwargs.get("provider")) or self._provider
            self._platform = _clean(kwargs.get("platform")) or self._platform
            self._session_id = _clean(kwargs.get("session_id")) or self._session_id
            if (
                self._last_prompt_tokens >= _COMPRESSION_MIN_PROMPT
                and record.prompt_tokens
                < self._last_prompt_tokens * _COMPRESSION_DROP_RATIO
            ):
                self._compressions += 1
            self._last_prompt_tokens = record.prompt_tokens
            self._requests.append(record)

    def on_api_request_error(self, **kwargs: Any) -> None:
        """A provider call raised — count it and clear the in-flight marker."""
        with self._lock:
            self._inflight = False
            self._errors += 1
            self._last_activity = time.time()

    def on_pre_tool_call(self, *, tool_name: str = "", args: Any = None, **kwargs: Any) -> None:
        """The agent started a tool — that is what it is "doing"."""
        name = _clean(tool_name) or "tool"
        now = time.time()
        with self._lock:
            self._current_tool = name
            self._last_activity = now
            self._turn_live = True
            if not self._turn_started:
                self._turn_started = now
            key = _clean(kwargs.get("tool_call_id")) or name
            self._pending[key] = _PendingTool(name=name, hint=hint_for(args), started_at=now)

    def on_post_tool_call(self, **kwargs: Any) -> None:
        """Record a finished tool call and clear the "running" line."""
        name = _clean(kwargs.get("tool_name")) or "tool"
        key = _clean(kwargs.get("tool_call_id")) or name
        now = time.time()
        status = _clean(kwargs.get("status")).lower()
        raw_duration = kwargs.get("duration_ms")
        with self._lock:
            pending = self._pending.pop(key, None)
            if pending is None and self._pending:
                # No id match (older host, or the id was never passed): the most
                # recent start is this call's. Only when there is exactly one
                # candidate, so a nested call is never mis-attributed.
                if len(self._pending) == 1:
                    pending = self._pending.pop(next(iter(self._pending)))
            if pending is not None and self._current_tool == pending.name:
                # The call that just finished was the one on screen. What is on
                # screen now is whatever else is still running (a nested call
                # leaves its parent in flight), so read that off the pending map
                # rather than blanking the line.
                if self._pending:
                    self._current_tool = max(
                        self._pending.values(), key=lambda item: item.started_at
                    ).name
                else:
                    self._current_tool = ""
            if raw_duration is not None:
                duration = _float(raw_duration) / 1000.0
            elif pending is not None:
                duration = max(0.0, now - pending.started_at)
            else:
                duration = 0.0
            ok = status != "error"
            if status == "" and isinstance(kwargs.get("result"), str):
                ok = not kwargs["result"].startswith("Error")
            self._tool_calls += 1
            if not ok:
                self._tool_errors += 1
            self._last_activity = now
            run = ToolRun(
                name=pending.name if pending is not None else name,
                ok=ok,
                duration_s=duration,
                ended_at=now,
                hint=(pending.hint if pending is not None else hint_for(kwargs.get("args"))),
            )
            if self._tool_history:
                self._tools.append(run)

    def on_turn_end(self, **kwargs: Any) -> None:
        """The turn finished (``on_session_end``): stop claiming to work."""
        now = time.time()
        with self._lock:
            self._turn_live = False
            self._inflight = False
            self._current_tool = ""
            self._pending.clear()
            self._last_activity = now
            self._turns += 1
            if kwargs.get("failed"):
                self._failed_turns += 1
            self._turn_ended = now
            if not self._turn_started:
                self._turn_started = now

    # ── read side ────────────────────────────────────────────────────────────

    def snapshot(self) -> Snapshot:
        """Aggregate the rolling window into one immutable snapshot."""
        with self._lock:
            requests = list(self._requests)
            tools = tuple(self._tools)
            timed = [r for r in requests if r.duration > 0]
            total_duration = sum(r.duration for r in timed)
            total_output = sum(r.output_tokens for r in timed)
            total_prompt = sum(r.prompt_tokens for r in requests)
            total_read = sum(r.cache_read_tokens for r in requests)
            last = requests[-1] if requests else None
            last_timed = timed[-1] if timed else None
            pending = next(iter(self._pending.values()), None)
            current_tool = self._current_tool
            current_hint = pending.hint if pending is not None else ""

            cache_hit: float | None = None
            if total_prompt > 0 and total_read > 0:
                cache_hit = max(0.0, min(100.0, total_read / total_prompt * 100.0))

            context_used = last.prompt_tokens if last else 0
            context_length = self._context_length or (last.context_length if last else 0)
            context_pct: float | None = None
            if context_length > 0:
                context_pct = max(0.0, min(100.0, context_used / context_length * 100.0))

            return Snapshot(
                model=self._model,
                provider=self._provider,
                platform=self._platform,
                session_id=self._session_id,
                turn_live=self._turn_live,
                inflight=self._inflight,
                current_tool=current_tool,
                current_tool_hint=current_hint,
                current_tool_started=pending.started_at if pending is not None else 0.0,
                requests=len(requests),
                api_calls=self._api_calls,
                errors=self._errors,
                turns=self._turns,
                failed_turns=self._failed_turns,
                tps=(total_output / total_duration) if total_duration > 0 else 0.0,
                instant_tps=(
                    (last_timed.output_tokens / last_timed.duration)
                    if last_timed and last_timed.duration > 0
                    else 0.0
                ),
                cache_hit_pct=cache_hit,
                context_used=context_used,
                context_length=context_length,
                context_pct=context_pct,
                latency=(total_duration / len(timed)) if timed else 0.0,
                output_tokens=total_output,
                total_tokens=self._total_tokens,
                compressions=self._compressions,
                tools=tools,
                tool_calls=self._tool_calls,
                tool_errors=self._tool_errors,
                turn_started=self._turn_started,
                turn_ended=self._turn_ended,
                last_activity=self._last_activity,
                started_at=self._started_at,
            )
