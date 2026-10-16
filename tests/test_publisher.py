"""The publisher's contract: post once, edit quietly, never lose the anchor.

Every test here drives ``tick()`` through a fake transport, which is the only
part of the plugin that talks to a platform. What is asserted is the *number and
kind of API calls* — that is what a chat rate limit actually cares about.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from sticky_statusbar.publisher import StickyPublisher
from sticky_statusbar.settings import StickyConfig


class FakeSink:
    """A transport that records every call instead of making one."""

    platform = "telegram"
    supports_pin = True

    def __init__(self, *, fail_send: bool = False, fail_edit: bool = False) -> None:
        self.fail_send = fail_send
        self.fail_edit = fail_edit
        self.sent: list[tuple[str, str, str]] = []
        self.edits: list[tuple[str, str, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self.pinned: list[tuple[str, str]] = []
        self.calls: list[str] = []

    def available(self) -> bool:
        return True

    async def send(self, chat_id: str, text: str, thread_id: str = "") -> str | None:
        self.calls.append("send")
        if self.fail_send:
            return None
        self.sent.append((chat_id, text, thread_id))
        return f"m{len(self.sent)}"

    async def edit(self, chat_id: str, message_id: str, text: str) -> bool:
        self.calls.append("edit")
        if self.fail_edit:
            return False
        self.edits.append((chat_id, message_id, text))
        return True

    async def delete(self, chat_id: str, message_id: str) -> bool:
        self.calls.append("delete")
        self.deleted.append((chat_id, message_id))
        return True

    async def pin(self, chat_id: str, message_id: str) -> bool:
        self.calls.append("pin")
        self.pinned.append((chat_id, message_id))
        return True

    @property
    def writes(self) -> int:
        """API calls that changed something visible in the chat."""
        return len(self.sent) + len(self.edits) + len(self.deleted)


def make_publisher(
    collector, config=None, store=None, sink=None
) -> tuple[StickyPublisher, FakeSink]:
    sink = sink or FakeSink()
    publisher = StickyPublisher(
        "telegram", sink, collector, config or StickyConfig(), store=store
    )
    return publisher, sink


def run(coro):
    return asyncio.run(coro)


# ── creating and maintaining the anchor ──────────────────────────────────────


def test_first_tick_posts_and_records_the_anchor(collector, store):
    publisher, sink = make_publisher(collector, store=store)
    publisher.track("42")

    assert run(publisher.tick()) == 1
    assert len(sink.sent) == 1
    assert store.get("telegram", "42") == "m1"
    # One send, no edits: the first frame creates the message.
    assert sink.edits == []


def test_a_chat_is_only_posted_to_once_it_is_tracked(collector, store):
    publisher, sink = make_publisher(collector, store=store)
    assert run(publisher.tick()) == 0
    assert sink.calls == []


def test_an_unchanged_board_costs_no_api_calls(collector, store, config):
    publisher, sink = make_publisher(collector, config, store)
    publisher.track("42")
    now = 1_000_000.0
    run(publisher.tick(now))
    run(publisher.tick(now))
    assert len(sink.sent) == 1
    assert sink.edits == []


def test_a_changed_board_is_edited_in_place(collector, store, config):
    publisher, sink = make_publisher(collector, config, store)
    publisher.track("42")
    run(publisher.tick(1_000_000.0))
    collector.on_pre_tool_call(tool_name="terminal", args={"command": "ls"})
    run(publisher.tick(1_000_005.0))

    assert len(sink.sent) == 1              # the anchor never moves in edit mode
    assert len(sink.edits) == 1
    chat_id, message_id, text = sink.edits[0]
    assert (chat_id, message_id) == ("42", "m1")
    assert "terminal" in text


def test_an_uneditable_anchor_is_replaced_without_losing_the_chat(collector, store):
    publisher, sink = make_publisher(collector, store=store, sink=FakeSink(fail_edit=True))
    publisher.track("42")
    run(publisher.tick(1_000_000.0))
    # Force a different board so the tick actually tries an edit.
    publisher._last_text.clear()
    run(publisher.tick(1_000_010.0))

    assert len(sink.sent) == 2                     # a fresh anchor
    assert store.get("telegram", "42") == "m2"     # the record follows it
    # The dead anchor goes away only after the replacement exists.
    assert sink.calls.index("send") < sink.calls.index("delete")
    assert sink.deleted == [("42", "m1")]


def test_cancelled_tick_does_not_touch_the_other_chats(collector, store):
    publisher, sink = make_publisher(collector, store=store)
    publisher.track("1")
    publisher.track("2")
    run(publisher.tick())
    assert sorted(chat for chat, _, _ in sink.sent) == ["1", "2"]


# ── tracking rules ───────────────────────────────────────────────────────────


def test_auto_track_off_means_only_explicit_chats(collector, store):
    config = replace(StickyConfig(), auto_track=False)
    publisher, sink = make_publisher(collector, config, store)
    assert publisher.track("42") is False
    assert publisher.tracked_chats() == []
    assert publisher.track("42", force=True) is True
    run(publisher.tick())
    assert len(sink.sent) == 1


def test_tracking_the_same_chat_twice_keeps_one_entry(collector, store):
    publisher, _ = make_publisher(collector, store=store)
    assert publisher.track("42") is True
    assert publisher.track("42") is False
    assert len(publisher.tracked_chats()) == 1


def test_track_remembers_the_thread_for_telegram_topics(collector, store):
    publisher, sink = make_publisher(collector, store=store)
    publisher.track("42", thread_id="77", chat_type="group")
    run(publisher.tick())
    assert sink.sent[0] == ("42", sink.sent[0][1], "77")


def test_untrack_forgets_the_anchor(collector, store):
    publisher, _ = make_publisher(collector, store=store)
    publisher.track("42")
    run(publisher.tick())
    assert publisher.untrack("42") is True
    assert publisher.tracked_chats() == []
    assert store.get("telegram", "42") == ""
    assert publisher.untrack("42") is False


def test_empty_chat_id_is_ignored(collector, store):
    publisher, _ = make_publisher(collector, store=store)
    assert publisher.track("   ") is False


# ── mode: bump ───────────────────────────────────────────────────────────────


def test_edit_mode_never_reposts_even_at_turn_end(collector, store, config):
    """A requested bump is a no-op in edit mode: the message keeps its position
    and an unchanged board costs nothing at all."""
    publisher, sink = make_publisher(collector, config, store)
    publisher.track("42")
    run(publisher.tick(1_000_000.0))
    publisher.request_bump("42")
    run(publisher.tick(1_000_005.0))
    assert len(sink.sent) == 1
    assert sink.edits == []


def test_bump_mode_reposts_after_the_answer_and_never_deletes_first(collector, store):
    config = replace(StickyConfig(), mode="bump")
    publisher, sink = make_publisher(collector, config, store)
    publisher.track("42")
    run(publisher.tick(1_000_000.0))
    collector.on_pre_api_request(model="m", context_length=1000)
    collector.on_post_api_request(usage={"output_tokens": 10, "prompt_tokens": 100},
                                  api_duration=1.0, context_length=1000)
    publisher.request_bump("42")
    run(publisher.tick(1_000_005.0))

    assert len(sink.sent) == 2
    assert store.get("telegram", "42") == "m2"
    first_send = sink.calls.index("send")
    delete = sink.calls.index("delete")
    assert first_send < delete                       # the chat never shows a gap
    assert sink.deleted == [("42", "m1")]


def test_request_bump_targets_the_most_recently_active_chat(collector, store):
    config = replace(StickyConfig(), mode="bump")
    publisher, _ = make_publisher(collector, config, store)
    publisher.track("old", now=100.0)
    publisher.track("new", now=200.0)
    assert publisher.request_bump() == ["telegram:new"]


def test_bump_without_a_pending_request_does_nothing(collector, store):
    config = replace(StickyConfig(), mode="bump")
    publisher, sink = make_publisher(collector, config, store)
    publisher.track("42")
    run(publisher.tick(1_000_000.0))
    run(publisher.tick(1_000_005.0))
    assert len(sink.sent) == 1


# ── pinning ──────────────────────────────────────────────────────────────────


def test_pin_happens_once_per_anchor(collector, store, config):
    publisher, sink = make_publisher(collector, config, store)
    publisher.track("42")
    run(publisher.tick(1_000_000.0))
    collector.on_pre_tool_call(tool_name="terminal", args={})
    run(publisher.tick(1_000_005.0))
    assert sink.pinned == [("42", "m1")]


def test_pinning_can_be_switched_off(collector, store):
    config = replace(StickyConfig(), pin_telegram=False)
    publisher, sink = make_publisher(collector, config, store)
    publisher.track("42")
    run(publisher.tick())
    assert sink.pinned == []


# ── failure handling ─────────────────────────────────────────────────────────


def test_repeated_send_failures_back_off_instead_of_retrying_every_tick(collector, store):
    publisher, sink = make_publisher(collector, store=store, sink=FakeSink(fail_send=True))
    publisher.track("42")
    run(publisher.tick(1_000_000.0))
    assert sink.calls == ["send"]
    run(publisher.tick(1_000_001.0))          # inside the backoff window
    assert sink.calls == ["send"]
    assert publisher.failures == 1
    assert publisher._retry_after["telegram:42"] > 1_000_001.0


def test_a_chat_that_succeeds_after_a_failure_clears_its_backoff(collector, store):
    sink = FakeSink(fail_send=True)
    publisher, _ = make_publisher(collector, store=store, sink=sink)
    publisher.track("42")
    run(publisher.tick(1_000_000.0))
    sink.fail_send = False
    run(publisher.tick(1_000_000.0 + publisher._retry_after["telegram:42"]))
    assert publisher._failures == {}
    assert len(sink.sent) == 1


# ── lifecycle ────────────────────────────────────────────────────────────────


def test_a_rewire_with_new_settings_retargets_the_running_publisher(collector, store):
    """A settings change must land on the publisher that is already running:
    reloading the plugin re-runs register(), but the live task holds its own
    config, so without this every change waits for a gateway restart."""
    from sticky_statusbar import publisher as publisher_mod

    class Bot:
        async def send_message(self, **kwargs):        # pragma: no cover - not ticked
            raise AssertionError("no send expected")

    class Native:
        bot = Bot()

    async def scenario():
        first = publisher_mod.wire("telegram", Native(), None, collector, StickyConfig(),
                                   store=store)
        first.start()
        await asyncio.sleep(0)
        assert first.running is True

        bumped = replace(StickyConfig(), mode="bump", interval_seconds=60)
        second = publisher_mod.wire("telegram", Native(), None, collector, bumped, store=store)

        result = (second is first, first._config.mode, first._config.interval_for("telegram"),
                  publisher_mod.active_publishers())
        first.stop()
        await asyncio.sleep(0)
        return result

    same_object, mode, interval, publishers = run(scenario())
    assert same_object is True                     # no second loop stacked
    assert mode == "bump"                          # the new settings were adopted
    assert interval == 60
    assert publishers == 1


def test_a_dead_publisher_is_replaced_rather_than_adopted(collector, store):
    from sticky_statusbar import publisher as publisher_mod

    class Native:
        bot = object()

    async def scenario():
        first = publisher_mod.wire("telegram", Native(), None, collector, StickyConfig(),
                                   store=store)
        first.start()
        await asyncio.sleep(0)
        first.stop()
        await asyncio.sleep(0)
        second = publisher_mod.wire("telegram", Native(), None, collector, StickyConfig(),
                                    store=store)
        result = second is not first
        publisher_mod.stop_all()
        return result

    assert run(scenario()) is True


def test_a_bump_request_is_honoured_after_the_mode_changes(collector, store):
    publisher, sink = make_publisher(collector, store=store)
    publisher.track("42")
    run(publisher.tick(1_000_000.0))
    publisher.update_config(replace(StickyConfig(), mode="bump"))
    publisher.request_bump("42")
    run(publisher.tick(1_000_005.0))
    assert len(sink.sent) == 2                     # reposted, not edited in place


def test_start_requires_a_loop_and_is_idempotent(collector, store):
    publisher, _ = make_publisher(collector, store=store)
    assert publisher.running is False

    async def scenario():
        assert publisher.start() is True
        first = publisher._task
        assert publisher.start() is True          # idempotent
        assert publisher._task is first
        await asyncio.sleep(0)
        running = publisher.running
        publisher.stop()                          # on the loop it was created on
        await asyncio.sleep(0)
        return running

    assert run(scenario()) is True


def test_the_loop_publishes_without_being_asked(collector, store):
    """The task, not the hook, is what makes a sticky self-updating."""
    sink = FakeSink()
    publisher, _ = make_publisher(collector, store=store, sink=sink)
    publisher.track("42")

    async def scenario():
        publisher.start()
        for _ in range(20):
            await asyncio.sleep(0.01)
            if sink.sent:
                break
        publisher.stop()
        return bool(sink.sent)

    assert run(scenario()) is True


def test_cancelled_loop_does_not_raise_out(collector, store):
    publisher, _ = make_publisher(collector, store=store)

    async def scenario():
        publisher.start()
        await asyncio.sleep(0)
        publisher.stop()
        await asyncio.sleep(0)

    run(scenario())          # must not raise
