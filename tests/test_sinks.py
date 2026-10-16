"""The transports: exactly which platform calls are made, and with what.

This is the layer that touches someone's chat, so the assertions are on the
arguments — a status message that pings a channel, breaks on a ``·`` in the text,
or edits the wrong message id is a real bug, not a cosmetic one.
"""

from __future__ import annotations

import asyncio

from sticky_statusbar.sinks import AdapterSink, DiscordSink, Sink, TelegramSink, build_sink


def run(coro):
    return asyncio.run(coro)


# ── Telegram ─────────────────────────────────────────────────────────────────


class FakeTelegramMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class FakeTelegramBot:
    def __init__(self, *, message_id: int = 77, explode: str = "") -> None:
        self.message_id = message_id
        self.explode = explode            # a method name that should raise
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.deleted: list[dict] = []
        self.pins: list[dict] = []

    def _maybe_raise(self, name: str) -> None:
        if self.explode == name:
            raise RuntimeError(f"{name} exploded")

    async def send_message(self, **kwargs):
        self._maybe_raise("send_message")
        self.sent.append(kwargs)
        return FakeTelegramMessage(self.message_id)

    async def edit_message_text(self, **kwargs):
        self._maybe_raise("edit_message_text")
        self.edits.append(kwargs)

    async def delete_message(self, **kwargs):
        self._maybe_raise("delete_message")
        self.deleted.append(kwargs)

    async def pin_chat_message(self, **kwargs):
        self._maybe_raise("pin_chat_message")
        self.pins.append(kwargs)


class FakeApplication:
    def __init__(self, bot) -> None:
        self.bot = bot


def telegram_sink(*, explode: str = "") -> tuple[TelegramSink, FakeTelegramBot]:
    bot = FakeTelegramBot(explode=explode)
    return TelegramSink(FakeApplication(bot), None), bot


def test_telegram_send_is_silent_plain_text_and_targets_the_right_chat():
    sink, bot = telegram_sink()
    message_id = run(sink.send("-1001234567890", "📊 Hermes · working"))
    assert message_id == "77"
    assert bot.sent == [
        {
            "chat_id": -1001234567890,        # numeric ids are coerced for PTB
            "text": "📊 Hermes · working",
            "disable_notification": True,     # a status message never pings
        }
    ]
    assert "parse_mode" not in bot.sent[0]    # so a '·' or '_' cannot break MarkdownV2


def test_telegram_send_carries_the_topic_for_a_forum_thread():
    sink, bot = telegram_sink()
    run(sink.send("-100", "x", "44"))
    assert bot.sent[0]["message_thread_id"] == 44


def test_telegram_send_leaves_a_channel_handle_alone():
    sink, bot = telegram_sink()
    run(sink.send("@my_channel", "x"))
    assert bot.sent[0]["chat_id"] == "@my_channel"


def test_a_failed_telegram_send_reports_nothing_rather_than_raising():
    sink, _ = telegram_sink(explode="send_message")
    assert run(sink.send("-100", "x")) is None


def test_telegram_edit_targets_the_message_it_was_given():
    sink, bot = telegram_sink()
    assert run(sink.edit("-100", "77", "new board")) is True
    assert bot.edits == [{"chat_id": -100, "message_id": 77, "text": "new board"}]


def test_an_unchanged_edit_is_not_a_failure():
    """Telegram raises BadRequest("message is not modified") for identical text."""

    class Bot(FakeTelegramBot):
        async def edit_message_text(self, **kwargs):
            raise RuntimeError("Bad Request: message is not modified")

    sink = TelegramSink(FakeApplication(Bot()), None)
    assert run(sink.edit("-100", "77", "same")) is True


def test_a_real_edit_failure_asks_for_a_repost():
    sink, _ = telegram_sink(explode="edit_message_text")
    assert run(sink.edit("-100", "77", "x")) is False


def test_telegram_delete_and_pin_are_both_silent():
    sink, bot = telegram_sink()
    assert run(sink.delete("-100", "77")) is True
    assert run(sink.pin("-100", "77")) is True
    assert bot.deleted == [{"chat_id": -100, "message_id": 77}]
    assert bot.pins == [{"chat_id": -100, "message_id": 77, "disable_notification": True}]


def test_a_refused_pin_is_reported_not_raised():
    """Bots without pin rights are normal in groups."""
    sink, _ = telegram_sink(explode="pin_chat_message")
    assert run(sink.pin("-100", "77")) is False
    assert sink.supports_pin is True


def test_a_telegram_sink_without_a_bot_is_unavailable():
    sink = TelegramSink(object(), None)
    assert sink.available() is False
    assert run(sink.send("-100", "x")) is None
    assert run(sink.edit("-100", "1", "x")) is False


# ── Discord ──────────────────────────────────────────────────────────────────


class FakeDiscordMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


class FakePartialMessage:
    def __init__(self, channel, message_id: int) -> None:
        self.channel = channel
        self.id = message_id

    async def edit(self, content=None):
        self.channel.edits.append((self.id, content))

    async def delete(self):
        self.channel.deletes.append(self.id)


class FakeDiscordChannel:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[tuple] = []
        self.deletes: list[int] = []

    async def send(self, content=None, silent=False):
        self.sent.append({"content": content, "silent": silent})
        return FakeDiscordMessage(900 + len(self.sent))

    def get_partial_message(self, message_id: int) -> FakePartialMessage:
        return FakePartialMessage(self, message_id)


class FakeDiscordBot:
    def __init__(self, channel=None, *, cached: bool = True, fetchable: bool = True) -> None:
        self._channel = channel
        self._cached = cached
        self._fetchable = fetchable
        self.fetched: list[int] = []
        self.listeners: list[tuple] = []

    def add_listener(self, callback, name: str = "on_message") -> None:
        """discord.py's listener registration — one entry per (callback, event)."""
        self.listeners.append((callback, name))

    def get_channel(self, channel_id):
        if not self._cached:
            return None                        # not in the client's cache yet
        return self._channel

    async def fetch_channel(self, channel_id):
        self.fetched.append(channel_id)
        if not self._fetchable:
            raise RuntimeError("unknown channel")
        return self._channel


def test_discord_send_is_silent_and_returns_the_message_id():
    channel = FakeDiscordChannel()
    sink = DiscordSink(FakeDiscordBot(channel), None)
    assert run(sink.send("123", "📊 Hermes")) == "901"
    assert channel.sent == [{"content": "📊 Hermes", "silent": True}]


def test_discord_falls_back_to_fetching_the_channel():
    channel = FakeDiscordChannel()
    bot = FakeDiscordBot(channel, cached=False)
    sink = DiscordSink(bot, None)
    assert run(sink.send("123", "x")) == "901"
    assert bot.fetched == [123]


def test_discord_survives_an_older_client_without_the_silent_kwarg():
    class OldChannel(FakeDiscordChannel):
        async def send(self, content=None):          # no ``silent`` parameter
            self.sent.append({"content": content, "silent": False})
            return FakeDiscordMessage(950)

    channel = OldChannel()
    sink = DiscordSink(FakeDiscordBot(channel), None)
    assert run(sink.send("123", "x")) == "950"


def test_discord_edits_and_deletes_through_the_native_client():
    """The adapter exposes no delete for Discord, so the anchor is cleaned up here."""
    channel = FakeDiscordChannel()
    sink = DiscordSink(FakeDiscordBot(channel), None)
    assert run(sink.edit("123", "901", "new")) is True
    assert channel.edits == [(901, "new")]
    assert run(sink.delete("123", "901")) is True
    assert channel.deletes == [901]


def test_discord_failures_are_reported_not_raised():
    bot = FakeDiscordBot(FakeDiscordChannel(), cached=False, fetchable=False)
    sink = DiscordSink(bot, None)
    assert run(sink.send("123", "x")) is None
    assert run(sink.edit("123", "1", "x")) is False
    assert run(sink.delete("123", "1")) is False


def test_discord_does_not_claim_to_pin():
    sink = DiscordSink(FakeDiscordBot(FakeDiscordChannel()), None)
    assert sink.supports_pin is False
    assert run(sink.pin("123", "1")) is False


# ── the generic adapter transport ────────────────────────────────────────────


class SendResult:
    def __init__(self, success, message_id=None, error=None) -> None:
        self.success = success
        self.message_id = message_id
        self.error = error


class FakeAdapter:
    platform = "slack"

    def __init__(self, *, send_ok=True, edit_ok=True, delete_ok=True) -> None:
        self.send_ok = send_ok
        self.edit_ok = edit_ok
        self.delete_ok = delete_ok
        self.calls: list[tuple] = []

    def normalize_chat_id(self, chat_id):
        return f"chan-{chat_id}"

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.calls.append(("send", chat_id, content))
        return SendResult(self.send_ok, "42" if self.send_ok else None, "nope")

    async def edit_message(self, chat_id, message_id, content, finalize=False):
        self.calls.append(("edit", chat_id, message_id, content))
        return SendResult(self.edit_ok, message_id)

    async def delete_message(self, chat_id, message_id):
        self.calls.append(("delete", chat_id, message_id))
        return self.delete_ok


def test_adapter_sink_uses_the_documented_surface():
    adapter = FakeAdapter()
    sink = AdapterSink(adapter, "slack")
    assert run(sink.send("1", "board")) == "42"
    assert run(sink.edit("1", "42", "new")) is True
    assert run(sink.delete("1", "42")) is True
    assert adapter.calls == [
        ("send", "chan-1", "board"),
        ("edit", "chan-1", "42", "new"),
        ("delete", "chan-1", "42"),
    ]


def test_adapter_sink_reports_a_refusal_as_a_failure():
    sink = AdapterSink(FakeAdapter(send_ok=False, edit_ok=False, delete_ok=False), "feishu")
    assert run(sink.send("1", "x")) is None
    assert run(sink.edit("1", "2", "x")) is False
    assert run(sink.delete("1", "2")) is False


def test_adapter_sink_survives_an_adapter_that_raises():
    class Angry(FakeAdapter):
        async def send(self, *args, **kwargs):
            raise RuntimeError("transport down")

        async def edit_message(self, *args, **kwargs):
            raise RuntimeError("transport down")

        async def delete_message(self, *args, **kwargs):
            raise RuntimeError("transport down")

    sink = AdapterSink(Angry(), "matrix")
    assert run(sink.send("1", "x")) is None
    assert run(sink.edit("1", "2", "x")) is False
    assert run(sink.delete("1", "2")) is False


# ── picking the transport ────────────────────────────────────────────────────


def test_build_sink_prefers_the_native_client_where_one_exists():
    assert isinstance(build_sink("telegram", FakeApplication(FakeTelegramBot()), None),
                      TelegramSink)
    assert isinstance(build_sink("discord", FakeDiscordBot(FakeDiscordChannel()), None),
                      DiscordSink)


def test_build_sink_falls_back_to_the_adapter_for_everything_else():
    assert isinstance(build_sink("slack", None, FakeAdapter()), AdapterSink)
    assert isinstance(build_sink("telegram", None, FakeAdapter()), AdapterSink)
    assert isinstance(build_sink("", None, FakeAdapter()), AdapterSink)


# ── watching for new messages ────────────────────────────────────────────────


class ListeningTelegramApplication:
    """PTB's ``Application`` surface the sink needs to subscribe."""

    def __init__(self) -> None:
        self.bot = FakeTelegramBot()
        self.handlers: list[tuple] = []

    def add_handler(self, handler, group: int = 0):
        self.handlers.append((handler, group))


class FakeTelegramUpdate:
    def __init__(self, chat_id, message_id, is_bot=False) -> None:
        self.effective_chat = type("Chat", (), {"id": chat_id})()
        self.effective_message = type("Msg", (), {"message_id": message_id})()
        self.effective_user = type("User", (), {"is_bot": is_bot})()


def test_the_base_sink_cannot_watch():
    assert Sink().watch(lambda *args: None) is False


def test_telegram_watch_observes_updates_in_its_own_handler_group():
    """Group 99: PTB runs groups in order, so an observer there can never
    pre-empt the core handlers (approvals, model pickers)."""
    app = ListeningTelegramApplication()
    sink = TelegramSink(app, None)
    seen: list[tuple] = []
    assert sink.watch(lambda *args: seen.append(args)) is True
    assert len(app.handlers) == 1
    handler, group = app.handlers[0]
    assert group == 99
    assert handler.block is False                 # off the dispatcher's critical path

    run(handler.callback(FakeTelegramUpdate(-100123, 77, is_bot=False), None))
    assert seen == [("-100123", "77", False)]


def test_telegram_watch_is_idempotent_and_marks_the_sender():
    app = ListeningTelegramApplication()
    sink = TelegramSink(app, None)
    seen: list[tuple] = []
    sink.watch(lambda *args: seen.append(args))
    sink.watch(lambda *args: seen.append(("second",)))     # ignored: already watching
    assert len(app.handlers) == 1

    handler, _ = app.handlers[0]
    run(handler.callback(FakeTelegramUpdate(5, 6, is_bot=True), None))
    assert seen == [("5", "6", True)]


def test_telegram_watch_never_raises_out_of_the_observer():
    app = ListeningTelegramApplication()
    sink = TelegramSink(app, None)

    def explode(*args):
        raise RuntimeError("consumer blew up")

    sink.watch(explode)
    handler, _ = app.handlers[0]
    run(handler.callback(FakeTelegramUpdate(1, 2), None))   # must not propagate


def test_discord_watch_uses_the_existing_gateway_socket():
    """No extra REST call: discord.py already delivers every message."""
    bot = FakeDiscordBot(FakeDiscordChannel())
    sink = DiscordSink(bot, None)
    seen: list[tuple] = []
    assert sink.watch(lambda *args: seen.append(args)) is True
    assert [name for _, name in bot.listeners] == ["on_message"]

    callback, _ = bot.listeners[0]
    message = type(
        "Message", (),
        {
            "id": 4242,
            "channel": type("Channel", (), {"id": 1555124062546825278})(),
            "author": type("Author", (), {"bot": True})(),
        },
    )()
    run(callback(message))
    assert seen == [("1555124062546825278", "4242", True)]


def test_discord_watch_is_idempotent_and_survives_a_broken_callback():
    bot = FakeDiscordBot(FakeDiscordChannel())
    sink = DiscordSink(bot, None)
    sink.watch(lambda *args: None)
    sink.watch(lambda *args: None)                  # ignored: already watching
    assert len(bot.listeners) == 1

    def boom(*args):
        raise RuntimeError("consumer blew up")

    sink._watching = False
    sink.watch(boom)
    callback, _ = bot.listeners[-1]
    run(callback(type("M", (), {"id": 1, "channel": None, "author": None})()))


def test_a_client_without_listener_support_is_reported_not_crashed():
    assert DiscordSink(object(), None).watch(lambda *args: None) is False
    assert TelegramSink(object(), None).watch(lambda *args: None) is False
