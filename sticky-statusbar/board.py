"""Pure rendering: snapshot + config → the status message a chat shows.

Kept free of every platform SDK import so it is unit-testable and so
``register()`` works on a host with no gateway running.

Layout contract (mirrors the Hermes status bar's segment vocabulary, so a user
reading both sees the same glyphs):

* ``⚡`` tokens/sec       — throughput, windowed
* ``◎`` cache hit rate    — higher is better
* ``▰▰▰▱▱`` context bar + percent — lower is better
* ``⧉`` tool              — the call running right now
* ``✓`` / ``✗``           — a tool call that finished ok / failed
* ``☤`` model, ``⏱`` turn elapsed, ``↻`` turns, ``Σ`` lifetime tokens
* ``◷`` latency, ``⌛`` relative age of the last update

The board is a *stack of lines*, and a line with nothing to say is omitted
rather than rendered as ``--``: a status message that repeats "no data" is noise
in a chat that is meant to be read while scrolling past it.
"""

from __future__ import annotations

import time

from .telemetry import Snapshot, ToolRun

GLYPH_TPS = "⚡"
GLYPH_CACHE = "◎"
GLYPH_TOOL = "⧉"
GLYPH_MODEL = "☤"
GLYPH_LATENCY = "◷"
GLYPH_TOTALS = "Σ"
GLYPH_ELAPSED = "⏱"
GLYPH_TURNS = "↻"
GLYPH_AGO = "⌛"
OK_MARK = "✓"
ERR_MARK = "✗"

# Longest line the renderer will emit; the publisher applies ``max_chars`` to
# the whole message, this keeps a single runaway segment from eating the budget.
_MAX_LINE = 400


def _compact_tokens(count: int) -> str:
    """``1234`` → ``1.2K``, ``1234567`` → ``1.2M``."""
    value = float(max(0, count))
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= limit:
            return f"{value / limit:.1f}{suffix}"
    return str(int(value))


def build_context_bar(pct: float | None, width: int, fill: str = "▰", empty: str = "▱") -> str:
    """``▰▰▰▱▱▱▱▱▱▱``-style bar, clamped to 0-100%."""
    safe = 0.0 if pct is None else max(0.0, min(100.0, pct))
    cells = max(1, width)
    filled = max(0, min(cells, int(round((safe / 100.0) * cells))))
    return f"{fill * filled}{empty * (cells - filled)}"


def _fmt_pct(value: float | None, precision: int = 1) -> str:
    return "--" if value is None else f"{value:.{precision}f}%"


def fmt_duration(seconds: float) -> str:
    """``42`` → ``42s``, ``192`` → ``3m12s``, ``7500`` → ``2h05m``."""
    total = max(0, int(seconds or 0))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


def fmt_ago(seconds: float) -> str:
    """Coarse age (``just now`` / ``12m ago`` / ``3h ago``).

    Deliberately coarse: the footer's age is the one piece that would otherwise
    change on every tick, so at minute granularity an idle board renders the same
    text for a minute and the publisher's dedupe turns that into zero API calls.
    """
    total = max(0, int(seconds or 0))
    if total < 45:
        return "just now"
    if total < 5400:
        return f"{max(1, total // 60)}m ago"
    return f"{total // 3600}h ago"


def _join(parts: list[str], separator: str) -> str:
    return separator.join(part for part in parts if part)


def _fits(segments: list[str], separator: str, limit: int = _MAX_LINE) -> str:
    """Longest leading run of *segments* whose joined text fits in *limit*."""
    kept: list[str] = []
    for item in segments:
        if kept and len(_join(kept + [item], separator)) > limit:
            break
        kept.append(item)
    if kept and len(_join(kept, separator)) > limit:
        return _join([segments[0]], separator)[:limit]
    return _join(kept, separator)


def state_text(snapshot: Snapshot, config, now: float | None = None) -> str:
    """The word(s) after the header on line one."""
    state = snapshot.state_at(now)
    if state == "tool":
        if not config.show_tools:
            return "working"
        tool = snapshot.current_tool or "tool"
        hint = f" {snapshot.current_tool_hint}" if snapshot.current_tool_hint else ""
        return f"running {GLYPH_TOOL} {tool}{hint}"
    if state == "thinking":
        return "thinking…"
    if state == "working":
        return "working"
    return config.idle_text


def tool_line(snapshot: Snapshot, config, now: float | None = None) -> str:
    """The running call plus the recent ledger, newest last."""
    pieces: list[str] = []
    if snapshot.current_tool:
        running_for = 0.0
        if snapshot.current_tool_started:
            running_for = max(0.0, (now if now is not None else time.time())
                              - snapshot.current_tool_started)
        hint = f" {snapshot.current_tool_hint}" if snapshot.current_tool_hint else ""
        suffix = f" ({fmt_duration(running_for)})" if running_for >= 1 else ""
        pieces.append(f"{GLYPH_TOOL} {snapshot.current_tool}{suffix}{hint}")

    history = list(snapshot.tools)[-max(0, config.tool_history):] if config.tool_history else []
    for run in history:
        pieces.append(_tool_run_text(run))

    if not pieces:
        return ""
    return _fits(pieces, config.separator)


def _tool_run_text(run: ToolRun) -> str:
    mark = OK_MARK if run.ok else ERR_MARK
    duration = f" {run.duration_s:.1f}s" if run.duration_s >= 0.05 else ""
    hint = f" {run.hint}" if run.hint else ""
    return f"{mark} {run.name}{duration}{hint}"


def metrics_line(snapshot: Snapshot, config) -> str:
    """Throughput, cache, context and model, in the status bar's vocabulary."""
    pieces: list[str] = []
    if config.show_metrics:
        if snapshot.tps > 0:
            pieces.append(f"{GLYPH_TPS} {snapshot.tps:.0f} tok/s")
        if snapshot.cache_hit_pct is not None:
            pieces.append(f"{GLYPH_CACHE} {_fmt_pct(snapshot.cache_hit_pct, 0)} cache")
        if snapshot.context_pct is not None:
            bar = build_context_bar(snapshot.context_pct, config.bar_width)
            ctx = f"{bar} {snapshot.context_pct:.0f}% ctx"
            if snapshot.context_length:
                ctx = (
                    f"{bar} {_compact_tokens(snapshot.context_used)}/"
                    f"{_compact_tokens(snapshot.context_length)} ctx"
                )
            pieces.append(ctx)
        if snapshot.latency > 0:
            pieces.append(f"{GLYPH_LATENCY} {snapshot.latency:.1f}s")
    if config.show_model and snapshot.model:
        model = snapshot.model.split("/")[-1]
        pieces.append(f"{GLYPH_MODEL} {model[:32]}")
    if config.show_totals and snapshot.total_tokens > 0:
        pieces.append(f"{GLYPH_TOTALS} {_compact_tokens(snapshot.total_tokens)}")
    return _fits(pieces, config.separator)


def footer_line(snapshot: Snapshot, config, now: float | None = None) -> str:
    """Elapsed/when, turn count and the health counters — the "how is it going"
    line that makes a stalled agent visible at a glance."""
    pieces: list[str] = []
    current = now if now is not None else time.time()
    elapsed = snapshot.elapsed_seconds(current)
    if config.show_elapsed and elapsed >= 1:
        pieces.append(f"{GLYPH_ELAPSED} {fmt_duration(elapsed)}")
    if snapshot.turns:
        pieces.append(f"{GLYPH_TURNS} {snapshot.turns} turns")
    if snapshot.api_calls:
        pieces.append(f"{snapshot.api_calls} calls")
    if snapshot.errors:
        pieces.append(f"{snapshot.errors} errors")
    if snapshot.tool_calls:
        pieces.append(f"{snapshot.tool_calls} tools")
    if not snapshot.busy_at(current) and snapshot.last_activity:
        pieces.append(f"{GLYPH_AGO} {fmt_ago(current - snapshot.last_activity)}")
    return _fits(pieces, config.separator)


def render_board(snapshot: Snapshot, config, now: float | None = None) -> str:
    """The whole status message: header, tools, metrics, counters.

    Empty lines are dropped, so an idle board on a quiet session is two short
    lines instead of a wall of placeholders.
    """
    current = now if now is not None else time.time()
    header = _join([config.header, state_text(snapshot, config, current)], config.separator)

    lines = [header]
    if config.show_tools:
        line = tool_line(snapshot, config, current)
        if line:
            lines.append(line)
    line = metrics_line(snapshot, config)
    if line:
        lines.append(line)
    line = footer_line(snapshot, config, current)
    if line:
        lines.append(line)

    text = "\n".join(lines)
    if len(text) > config.max_chars:
        text = text[: max(1, config.max_chars - 1)].rstrip() + "…"
    return text


def render_report(snapshot: Snapshot, config) -> str:
    """Multi-line plain-text report for the ``/sticky`` slash command."""
    now = time.time()
    lines: list[str] = ["**Sticky statusbar**", ""]
    lines.append(f"State: **{state_text(snapshot, config, now)}**")
    if snapshot.model:
        route = f"{snapshot.provider}/{snapshot.model}" if snapshot.provider else snapshot.model
        lines.append(f"Model: `{route}`")
    if snapshot.session_id:
        lines.append(f"Session: `{snapshot.session_id}`")
    lines.append("")

    if snapshot.tps > 0:
        lines.append(
            f"{GLYPH_TPS} **{snapshot.tps:.1f} tok/s** windowed "
            f"({snapshot.instant_tps:.1f} tok/s last request, {snapshot.requests} samples)"
        )
    if snapshot.cache_hit_pct is not None:
        lines.append(f"{GLYPH_CACHE} Cache hit: **{_fmt_pct(snapshot.cache_hit_pct)}**")
    else:
        lines.append(f"{GLYPH_CACHE} Cache hit: no cached reads in this window")
    if snapshot.context_pct is not None:
        bar = build_context_bar(snapshot.context_pct, config.bar_width * 2)
        lines.append(
            f"Context: `{bar}` **{snapshot.context_pct:.1f}%** "
            f"({snapshot.context_used:,} / {snapshot.context_length:,} tokens)"
        )
    if snapshot.latency > 0:
        lines.append(f"{GLYPH_LATENCY} Mean latency: {snapshot.latency:.2f}s")

    if snapshot.tools:
        lines.append("")
        lines.append("**Recent tools**")
        for run in snapshot.tools:
            hint = f" `{run.hint}`" if run.hint else ""
            mark = OK_MARK if run.ok else ERR_MARK
            lines.append(f"{mark} `{run.name}` {run.duration_s:.2f}s{hint}")

    lines.append("")
    lines.append(
        f"{snapshot.api_calls} API calls · {snapshot.errors} errors · "
        f"{snapshot.tool_calls} tool calls ({snapshot.tool_errors} failed) · "
        f"{snapshot.turns} turns"
    )
    if snapshot.total_tokens:
        lines.append(f"{GLYPH_TOTALS} {snapshot.total_tokens:,} tokens this process")
    if snapshot.platform:
        lines.append(f"Surface: `{snapshot.platform}`")
    return "\n".join(lines)
