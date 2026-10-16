"""The sticky itself: one message per chat, kept current.

One publisher per platform client, started on that client's event loop when the
platform connects (``register_platform_handler``). The hooks run on the agent
thread and only ever touch this object's small locked state (which chats are
tracked, which message is the anchor, whether a repost was asked for); the
publisher's own task does all the awaiting. That split is deliberate: it keeps
every platform call on the loop that owns the client, with no cross-thread
``run_coroutine_threadsafe`` juggling for a status message.

Update policy, in order of what it protects:

1. **Never post into a chat nobody asked for.** A chat is tracked when it
   messages the agent (``auto_track``), when it is listed in ``chats``, or when
   ``/sticky here`` is used in it.
2. **Never edit for nothing.** The rendered text is compared with what was last
   published for that chat; an unchanged board costs zero API calls, which is
   what lets the interval be aggressive without hitting chat rate limits.
3. **Never lose the anchor.** If an edit fails — the message was deleted, the
   record is stale after a restart, or the platform has no edit API at all — the
   publisher posts a fresh anchor, and only then forgets the old id.
4. **Never fight the rate limit.** A chat that keeps failing backs off
   exponentially (up to five minutes) instead of retrying every tick.

``mode: bump`` adds one thing on top: at the end of a turn the message is
reposted, so it lands *below* the answer it was describing instead of above it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from . import board, redact
from .anchors import AnchorStore, anchor_key
from .settings import StickyConfig
from .sinks import Sink
from .telemetry import MetricsCollector, Snapshot

logger = logging.getLogger(__name__)

# Backoff ceiling for a chat whose sends keep failing (rate limit, revoked
# rights, deleted channel): the publisher stops hammering it and tries again
# every few minutes instead.
_MAX_BACKOFF = 300.0

# One publisher per platform, keyed by platform name: a gateway process has one
# client per platform, and the adapter rebuilds it on reconnect.
PUBLISHERS: dict[str, StickyPublisher] = {}


@dataclass
class TrackedChat:
    """A chat the sticky lives in."""

    platform: str
    chat_id: str
    thread_id: str = ""
    chat_type: str = "dm"
    last_active: float = 0.0
    added_at: float = 0.0


class StickyPublisher:
    """Keeps one self-updating message alive in every tracked chat."""

    def __init__(
        self,
        platform: str,
        sink: Sink,
        collector: MetricsCollector,
        config: StickyConfig,
        store: AnchorStore | None = None,
        label: str = "",
    ) -> None:
        self.platform = (platform or "").lower()
        self.label = label or self.platform
        self._sink = sink
        self._collector = collector
        self._config = config
        # Not ``store or AnchorStore()``: the store defines ``__len__``, so an
        # empty one is falsy and would be silently replaced by a second store
        # pointing at the live home.
        self._store = store if store is not None else AnchorStore()

        self._lock = threading.Lock()
        self._tracked: dict[str, TrackedChat] = {}
        self._last_text: dict[str, str] = {}
        self._bump_pending: set[str] = set()
        self._failures: dict[str, int] = {}
        self._retry_after: dict[str, float] = {}
        self._pinned: set[str] = set()

        self._task: asyncio.Task | None = None
        self._stopped: asyncio.Event | None = None
        self.ticks = 0
        self.published = 0
        self.failures = 0

    # ── what the hooks call (agent thread, never awaits) ─────────────────────

    def track(
        self,
        chat_id: str,
        *,
        thread_id: str = "",
        chat_type: str = "dm",
        force: bool = False,
        now: float | None = None,
    ) -> bool:
        """Start tracking a chat. False when it is already tracked.

        ``force`` skips the ``auto_track`` gate — used by ``chats`` and by
        ``/sticky here``, where the user asked for it explicitly.
        """
        chat = str(chat_id or "").strip()
        if not chat:
            return False
        if not force and not self._config.auto_track:
            return False
        stamp = now if now is not None else time.time()
        key = anchor_key(self.platform, chat)
        with self._lock:
            existing = self._tracked.get(key)
            if existing is not None:
                existing.last_active = stamp
                if thread_id:
                    existing.thread_id = str(thread_id)
                return False
            self._tracked[key] = TrackedChat(
                platform=self.platform,
                chat_id=chat,
                thread_id=str(thread_id or ""),
                chat_type=str(chat_type or "dm"),
                last_active=stamp,
                added_at=stamp,
            )
            return True

    def update_config(self, config: StickyConfig) -> None:
        """Swap in freshly-resolved settings.

        A settings change (the Desktop form, ``hermes config set``) has to land on
        a publisher that is already running: reloading the plugin re-runs
        ``register()`` but the live task holds its own reference, so without this
        every change would wait for a gateway restart. The loop re-reads the
        interval each tick, so both the mode and the cadence follow.
        """
        with self._lock:
            self._config = config

    def untrack(self, chat_id: str) -> bool:
        """Stop tracking a chat (the message itself is left where it is)."""
        key = anchor_key(self.platform, chat_id)
        with self._lock:
            removed = self._tracked.pop(key, None) is not None
            self._last_text.pop(key, None)
            self._bump_pending.discard(key)
            self._retry_after.pop(key, None)
            self._failures.pop(key, None)
        if removed:
            self._store.clear(self.platform, chat_id)
        return removed

    def request_bump(self, chat_id: str = "") -> list[str]:
        """Ask for a repost (``mode: bump``) — all chats, or the most recent one.

        Called at turn end, where the hook payload carries the platform but not
        the chat: the chat that just spoke is the most recently active one.
        """
        with self._lock:
            if chat_id:
                keys = [anchor_key(self.platform, chat_id)]
            else:
                live = [c for c in self._tracked.values()]
                if not live:
                    return []
                newest = max(live, key=lambda c: c.last_active)
                keys = [anchor_key(self.platform, newest.chat_id)]
            keys = [k for k in keys if k in self._tracked]
            self._bump_pending.update(keys)
            return keys

    def tracked_chats(self) -> list[TrackedChat]:
        with self._lock:
            return list(self._tracked.values())

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> bool:
        """Start the update loop on *loop* (the platform client's). Idempotent."""
        if self._task is not None and not self._task.done():
            return True
        target = loop or _running_loop()
        if target is None:
            logger.warning(
                "[%s] sticky-statusbar: no event loop to publish on — "
                "the status message stays off on this platform", self.label,
            )
            return False
        if self._stopped is None:
            self._stopped = asyncio.Event()
        self._task = target.create_task(self._run())
        return True

    def stop(self) -> None:
        if self._stopped is not None:
            self._stopped.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def available(self) -> bool:
        """Whether the transport can talk to its platform at all."""
        try:
            return bool(self._sink.available())
        except Exception:  # noqa: BLE001
            return False

    async def _run(self) -> None:
        stopped = self._stopped or asyncio.Event()
        logger.info(
            "[%s] sticky-statusbar: updating the status message every %ss (%s mode)",
            self.label, self._config.interval_for(self.platform), self._config.mode,
        )
        while not stopped.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never take the gateway down
                logger.debug("[%s] sticky tick failed: %s", self.label, exc)
            # Re-read per tick: a settings change must land on the next update
            # instead of holding the interval the loop was born with.
            interval = self._config.interval_for(self.platform)
            try:
                await asyncio.wait_for(stopped.wait(), timeout=interval)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    # ── one tick ─────────────────────────────────────────────────────────────

    async def tick(self, now: float | None = None) -> int:
        """Publish one frame into every tracked chat. Returns edits/posts made."""
        self.ticks += 1
        current = now if now is not None else time.time()
        targets = self.tracked_chats()
        if not targets:
            return 0
        snapshot = self._collector.snapshot()
        text = redact.scrub(board.render_board(snapshot, self._config, current))
        wrote = 0
        for target in targets:
            try:
                if await self._publish_chat(target, snapshot, text, current):
                    wrote += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad chat never stops the rest
                logger.debug(
                    "[%s] sticky update failed in %s: %s", self.label, target.chat_id, exc
                )
        self.published += wrote
        return wrote

    async def _publish_chat(
        self, target: TrackedChat, snapshot: Snapshot, text: str, now: float
    ) -> bool:
        key = anchor_key(target.platform, target.chat_id)
        with self._lock:
            if now < self._retry_after.get(key, 0.0):
                return False
            previous = self._last_text.get(key)
            bump = key in self._bump_pending
            self._bump_pending.discard(key)

        anchor = self._store.get(target.platform, target.chat_id)

        if not anchor:
            return await self._post(target, text)

        if bump and self._config.bump_mode:
            return await self._repost(target, anchor, text)

        if previous == text:
            return False                    # nothing changed: zero API calls

        if await self._sink.edit(target.chat_id, anchor, text):
            with self._lock:
                self._last_text[key] = text
                self._failures.pop(key, None)
            return True

        # The anchor is not editable any more (deleted by hand, a record left
        # over from another run, or a platform with no edit API). Post a fresh
        # one rather than giving up on the chat.
        logger.debug(
            "[%s] sticky anchor %s unusable in %s — posting a fresh one",
            self.label, anchor, target.chat_id,
        )
        return await self._post(target, text, stale=anchor)

    async def _post(self, target: TrackedChat, text: str, stale: str = "") -> bool:
        """Create the anchor (optionally replacing a stale one) and pin it."""
        message_id = await self._sink.send(target.chat_id, text, target.thread_id)
        if not message_id:
            self._note_failure(target)
            return False
        self._store.set(target.platform, target.chat_id, message_id)
        with self._lock:
            self._last_text[anchor_key(target.platform, target.chat_id)] = text
            self._failures.pop(anchor_key(target.platform, target.chat_id), None)
            self._pinned.discard(anchor_key(target.platform, target.chat_id))
        if stale and stale != message_id:
            # Delete only after the replacement exists: the chat never shows a
            # gap where the status used to be.
            await self._sink.delete(target.chat_id, stale)
        await self._maybe_pin(target, message_id)
        return True

    async def _repost(self, target: TrackedChat, anchor: str, text: str) -> bool:
        """``mode: bump`` — put the message below the turn's answer."""
        return await self._post(target, text, stale=anchor)

    async def _maybe_pin(self, target: TrackedChat, message_id: str) -> None:
        if not (self._config.pin_telegram and self._sink.supports_pin):
            return
        key = anchor_key(target.platform, target.chat_id)
        with self._lock:
            if key in self._pinned:
                return
        if await self._sink.pin(target.chat_id, message_id):
            with self._lock:
                self._pinned.add(key)

    def _note_failure(self, target: TrackedChat) -> None:
        key = anchor_key(target.platform, target.chat_id)
        with self._lock:
            count = self._failures.get(key, 0) + 1
            self._failures[key] = count
            base = self._config.interval_for(self.platform)
            backoff = min(_MAX_BACKOFF, base * (2 ** min(count, 6)))
            self._retry_after[key] = time.time() + backoff
        self.failures += 1
        logger.debug(
            "[%s] sticky send failed %d time(s) in %s — backing off %.0fs",
            self.label, count, target.chat_id, backoff,
        )


def _running_loop() -> asyncio.AbstractEventLoop | None:
    """The loop we are currently on, if any.

    Only a *running* loop counts. A loop fetched from the policy but never run
    would accept the task and never execute it, which looks exactly like a
    working publisher until a user notices the status never moves — so refusing
    is the honest outcome, and the caller says so in the log.
    """
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def get(platform: str) -> StickyPublisher | None:
    """The publisher for *platform*, if that platform is wired."""
    return PUBLISHERS.get((platform or "").lower())


def all_publishers() -> list[StickyPublisher]:
    return list(PUBLISHERS.values())


def wire(
    platform: str,
    native: Any,
    adapter: Any,
    collector: MetricsCollector,
    config: StickyConfig,
    store: AnchorStore | None = None,
    label: str = "",
) -> StickyPublisher:
    """Platform-handler factory body: attach one publisher to *native*.

    Called by a platform adapter at connect time, and again on a late plugin
    load. Idempotent per platform: re-wiring an already-running publisher keeps
    the live one (and its anchors) instead of stacking a second loop.
    """
    from .sinks import build_sink

    name = (platform or "").lower()
    existing = PUBLISHERS.get(name)
    if existing is not None:
        if existing.running:
            # Already publishing: adopt the new settings rather than stacking a
            # second loop, and rather than ignoring the change until a restart.
            existing.update_config(config)
            return existing
        existing.stop()

    publisher = StickyPublisher(
        name,
        build_sink(name, native, adapter),
        collector,
        config,
        store=store,
        label=label or name,
    )
    PUBLISHERS[name] = publisher
    return publisher


def start_all(loop: asyncio.AbstractEventLoop | None = None) -> int:
    """Start every wired publisher; returns how many are running."""
    started = 0
    for publisher in all_publishers():
        if publisher.start(loop):
            started += 1
    return started


def stop_all() -> None:
    """Tear every publisher down (tests, plugin unload)."""
    for publisher in all_publishers():
        try:
            publisher.stop()
        except Exception:  # noqa: BLE001
            pass
    PUBLISHERS.clear()


def active_publishers() -> int:
    """How many publishers are live — used by the CLI and the tests."""
    return sum(1 for p in all_publishers() if p.running)
