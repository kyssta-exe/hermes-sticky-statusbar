"""Registration: what this plugin actually wires into Hermes.

Hermes loads a directory plugin and calls ``register(ctx)`` once. Everything the
plugin claims in ``plugin.yaml`` has to be a callback it really registered — the
catalog's admission check compares the two sets exactly, so the tests do too.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

MANIFEST = yaml.safe_load(
    (Path(__file__).resolve().parent.parent / "sticky-statusbar" / "plugin.yaml").read_text()
)


class StubCtx:
    """A recording ``PluginContext`` — the whole surface the plugin may use."""

    def __init__(self, values: dict | None = None) -> None:
        self.values = values or {}
        self.hooks: list[str] = []
        self.commands: list[str] = []
        self.tools: list[str] = []
        self.cli: list[str] = []
        self.platform_handlers: list[str] = []
        self.skills: list[str] = []

    def get_config(self, key: str, default=None):
        return self.values.get(key, default)

    def register_hook(self, name, callback):
        assert callable(callback)
        self.hooks.append(name)

    def register_command(self, name, handler, description="", args_hint="", **kwargs):
        assert callable(handler)
        self.commands.append(name)

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        assert callable(handler)
        self.tools.append(name)

    def register_cli_command(self, name, help="", setup_fn=None, handler_fn=None, **kwargs):
        assert callable(setup_fn) and callable(handler_fn)
        self.cli.append(name)

    def register_platform_handler(self, platform, factory):
        assert callable(factory)
        self.platform_handlers.append(platform)

    def register_skill(self, name, path):
        self.skills.append(name)


@pytest.fixture
def ctx(plugin):
    ctx = StubCtx()
    plugin.register(ctx)
    return ctx


# ── the manifest contract ────────────────────────────────────────────────────


def test_registered_hooks_match_the_manifest_exactly(ctx):
    assert sorted(ctx.hooks) == sorted(MANIFEST["provides_hooks"])


def test_registered_tools_match_the_manifest_exactly(ctx):
    assert sorted(ctx.tools) == sorted(MANIFEST["provides_tools"])


def test_every_hook_is_a_real_hermes_hook(plugin, ctx):
    """A typo'd hook name loads fine and never fires — the silent failure mode."""
    try:
        sys.path.insert(0, "/usr/local/lib/hermes-agent")
        from hermes_cli.plugins import VALID_HOOKS
    except Exception:
        pytest.skip("Hermes source not importable — the E2E module covers this when it is")

    assert set(ctx.hooks) <= VALID_HOOKS


def test_slash_command_tool_and_cli_are_registered(ctx):
    assert ctx.commands == ["sticky"]
    assert ctx.tools == ["agent_status"]
    assert ctx.cli == ["sticky-statusbar"]


def test_a_platform_handler_is_registered_for_every_supported_platform(plugin, ctx):
    assert sorted(ctx.platform_handlers) == sorted(plugin.PLATFORMS)
    # The two the plugin is actually verified against must be in there.
    assert {"telegram", "discord"} <= set(ctx.platform_handlers)


def test_the_bundled_skill_is_registered(ctx):
    assert ctx.skills == ["sticky-statusbar"]


def test_no_core_module_is_patched(plugin):
    """Catalog rule 9: the plugin extends Hermes only through public surfaces."""
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (Path(__file__).resolve().parent.parent / "sticky-statusbar").glob("*.py")
    )
    for forbidden in ("setattr(", "sys.modules[", "monkeypatch"):
        assert forbidden not in source


# ── the disabled path ────────────────────────────────────────────────────────


def test_disabled_posts_nothing_but_still_answers(plugin):
    ctx = StubCtx({"enabled": False})
    plugin.register(ctx)
    assert ctx.hooks == []
    assert ctx.platform_handlers == []
    assert ctx.commands == ["sticky"]          # the read surfaces stay
    assert ctx.tools == ["agent_status"]
    assert ctx.cli == ["sticky-statusbar"]


# ── the slash command ────────────────────────────────────────────────────────


def test_sticky_with_no_arguments_reports_the_board(plugin, collector):
    collector.on_pre_tool_call(tool_name="terminal", args={"command": "ls"})
    text = plugin._handle_sticky("")
    assert "**Sticky statusbar**" in text
    assert "sticky-statusbar:" in text


def test_sticky_config_answers_with_the_settings(plugin):
    assert "sticky-statusbar:" in plugin._handle_sticky("config")


def test_sticky_here_without_a_wired_platform_explains_itself(plugin):
    text = plugin._handle_sticky("here")
    assert "No chat seen yet" in text


def test_sticky_stop_without_a_tracked_chat_says_so(plugin):
    assert "nothing to stop" in plugin._handle_sticky("stop")


def test_sticky_bump_without_a_tracked_chat_says_so(plugin):
    assert "No chat tracked" in plugin._handle_sticky("bump")


@pytest.fixture
def wired(plugin, collector, store, config):
    """A live-looking telegram publisher with a recording transport."""
    from sticky_statusbar.publisher import PUBLISHERS, StickyPublisher

    class FakeSink:
        platform = "telegram"
        supports_pin = True

        def __init__(self):
            self.sent = []

        def available(self):
            return True

        async def send(self, chat_id, text, thread_id=""):
            self.sent.append((chat_id, text))
            return "m1"

        async def edit(self, chat_id, message_id, text):
            return True

        async def delete(self, chat_id, message_id):
            return True

        async def pin(self, chat_id, message_id):
            return True

    sink = FakeSink()
    publisher = StickyPublisher("telegram", sink, collector, config, store=store)
    PUBLISHERS["telegram"] = publisher
    yield publisher, sink
    PUBLISHERS.pop("telegram", None)


def test_sticky_here_tracks_the_chat_the_command_came_from(plugin, wired, config):
    publisher, _ = wired
    publisher.track("42", now=100.0)
    assert "already tracking" in plugin._handle_sticky("here")


def test_sticky_stop_ends_tracking_that_chat(plugin, wired):
    publisher, _ = wired
    publisher.track("42", now=100.0)
    assert "Stopped tracking" in plugin._handle_sticky("stop")
    assert publisher.tracked_chats() == []


def test_sticky_bump_queues_a_repost(plugin, wired):
    publisher, _ = wired
    publisher.track("42", now=100.0)
    assert "Reposting" in plugin._handle_sticky("bump")
    assert publisher._bump_pending == {"telegram:42"}


# ── the tool ─────────────────────────────────────────────────────────────────


def test_agent_status_returns_a_json_board(plugin, collector):
    collector.on_pre_tool_call(tool_name="terminal", args={"command": "ls"}, tool_call_id="c")
    payload = json.loads(plugin.status_tool.agent_status({}))
    assert payload["ok"] is True
    assert "Hermes" in payload["board"]
    assert payload["current_tool"] == "terminal"
    assert payload["state"] == "tool"


def test_agent_status_full_detail_adds_the_counters(plugin):
    payload = json.loads(plugin.status_tool.agent_status({"detail": "full"}))
    assert payload["detail"] == "full"
    for key in ("api_calls", "errors", "turns", "report", "publishers_live"):
        assert key in payload


def test_agent_status_never_raises(plugin, monkeypatch):
    monkeypatch.setattr(
        plugin, "get_collector", lambda: (_ for _ in ()).throw(RuntimeError("gone"))
    )
    payload = json.loads(plugin.status_tool.agent_status({}))
    assert payload["ok"] is False
    assert "RuntimeError" in payload["error"]


# ── the CLI command ──────────────────────────────────────────────────────────


def test_cli_status_prints_the_board(plugin, collector, capsys):
    collector.on_pre_api_request(model="deepseek-v4.1-flash", context_length=1000)
    args = type("Args", (), {"sticky_statusbar_command": "status"})()
    plugin._cli_command(args)
    out = capsys.readouterr().out
    assert "sticky-statusbar:" in out
    assert "📊 Hermes" in out


def test_cli_config_lists_every_key(plugin, capsys):
    args = type("Args", (), {"sticky_statusbar_command": "config"})()
    plugin._cli_command(args)
    out = capsys.readouterr().out
    assert "interval_seconds" in out
    assert "pin_telegram" in out


def test_cli_chats_explains_an_empty_registry(plugin, capsys):
    args = type("Args", (), {"sticky_statusbar_command": "chats"})()
    plugin._cli_command(args)
    assert "No chats tracked" in capsys.readouterr().out


def test_cli_reset_zeroes_the_counters(plugin, collector, capsys):
    collector.on_post_api_request(usage={"output_tokens": 5}, api_duration=1.0)
    args = type("Args", (), {"sticky_statusbar_command": "reset"})()
    plugin._cli_command(args)
    assert collector.snapshot().api_calls == 0
    assert "counters reset" in capsys.readouterr().out


# ── the hook callbacks ───────────────────────────────────────────────────────


def test_the_metric_hooks_feed_the_collector(plugin, collector):
    plugin._on_pre_api_request(model="m", context_length=1000, platform="telegram")
    plugin._on_post_api_request(
        usage={"output_tokens": 20, "prompt_tokens": 100}, api_duration=1.0
    )
    plugin._on_pre_tool_call(tool_name="terminal", args={"command": "ls"}, tool_call_id="c")
    plugin._on_post_tool_call(tool_name="terminal", tool_call_id="c", duration_ms=500)
    plugin._on_api_request_error(error="boom")
    snap = collector.snapshot()
    assert snap.api_calls == 1
    assert snap.errors == 1
    assert snap.tools[-1].name == "terminal"


def test_the_admission_hook_tracks_the_chat_and_starts_a_turn(plugin, wired, collector):
    plugin._on_post_gateway_admission(
        platform="telegram",
        source={"platform": "telegram", "chat_id": "-100", "chat_type": "group",
                "thread_id": "7"},
        session_key="telegram:-100",
        message_id="5",
        text="hi",
    )
    chats = plugin.publisher.get("telegram").tracked_chats()
    assert [(c.chat_id, c.thread_id, c.chat_type) for c in chats] == [("-100", "7", "group")]
    assert collector.snapshot().turn_live is True


def test_the_admission_hook_ignores_a_message_from_an_unwired_platform(plugin, collector):
    plugin._on_post_gateway_admission(
        platform="signal", source={"platform": "signal", "chat_id": "1"}, text="hi"
    )
    assert collector.snapshot().turn_live is True        # the turn still counts


def test_the_admission_hook_survives_a_missing_source(plugin, collector):
    plugin._on_post_gateway_admission(platform="telegram", source=None, text="hi")


def test_the_session_end_hook_only_bumps_in_bump_mode(plugin, wired, collector):
    publisher, _ = wired
    publisher.track("42", now=100.0)
    plugin._on_session_end(platform="telegram", completed=True)
    assert publisher._bump_pending == set()

    plugin._config = replace(plugin.get_config(), mode="bump")
    try:
        plugin._on_session_end(platform="telegram", completed=True)
        assert publisher._bump_pending == {"telegram:42"}
    finally:
        plugin._config = None


def test_hooks_never_raise_on_an_empty_payload(plugin):
    plugin._on_pre_api_request()
    plugin._on_post_api_request()
    plugin._on_api_request_error()
    plugin._on_pre_tool_call()
    plugin._on_post_tool_call()
    plugin._on_session_end()


# ── the platform factory ─────────────────────────────────────────────────────


def test_the_telegram_factory_wires_and_starts_a_publisher(plugin, collector, monkeypatch):
    """The factory is what the adapter calls at connect time; it must attach a
    publisher without a live Telegram connection."""
    import asyncio

    from sticky_statusbar import publisher as publisher_mod

    class Bot:
        async def send_message(self, **kwargs):
            raise AssertionError("no tick expected in this test")

    class Native:
        bot = Bot()

    plugin._fixed_chats = {"telegram": ("-100",)}
    factory = plugin._wire_factory("telegram")

    async def scenario():
        factory(Native(), type("Adapter", (), {"name": "Telegram"})())
        await asyncio.sleep(0)
        pub = publisher_mod.get("telegram")
        result = (pub.available, pub.running, [c.chat_id for c in pub.tracked_chats()])
        pub.stop()
        return result

    available, running, chats = asyncio.run(scenario())
    assert available is True
    assert running is True                        # the loop started on the connect loop
    assert chats == ["-100"]                      # the configured chats are pre-tracked


def test_the_factory_refuses_to_fake_a_publisher_without_a_loop(plugin):
    """No loop means no task. Starting one anyway would look fine and never update."""
    from sticky_statusbar import publisher as publisher_mod

    class Native:
        bot = object()

    factory = plugin._wire_factory("discord")
    factory(Native(), type("Adapter", (), {"name": "Discord"})())
    pub = publisher_mod.get("discord")
    assert pub is not None
    assert pub.running is False


def test_build_sink_picks_the_native_transport_where_it_can():
    from sticky_statusbar.sinks import AdapterSink, DiscordSink, TelegramSink, build_sink

    class Bot:
        pass

    class Native:
        bot = Bot()

    assert isinstance(build_sink("telegram", Native(), None), TelegramSink)
    assert isinstance(build_sink("discord", object(), None), DiscordSink)
    assert isinstance(build_sink("slack", None, object()), AdapterSink)
