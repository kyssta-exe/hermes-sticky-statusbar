"""End-to-end proof against the REAL Hermes plugin machinery.

The other test modules stub a ``PluginContext``. This one installs the plugin
into an isolated ``HERMES_HOME``, lets Hermes discover and load it exactly as the
gateway does, fires the payload shapes the agent actually emits through the live
hook registry, calls the registered platform factory the way an adapter does at
connect time, and then reads what a chat member would see.

Skipped automatically when Hermes is not importable (a bare checkout with no
``hermes-agent`` on the path), so the unit suite still runs standalone.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "sticky-statusbar"
PLUGIN_NAME = "sticky-statusbar"

# ── an isolated home, prepared BEFORE Hermes is imported ─────────────────────
# conftest has already pointed HERMES_HOME at a scratch directory. Install the
# plugin into it and enable it there, so discovery is real and nothing touches
# the profile that is currently live.

HOME = Path(os.environ.get("HERMES_HOME") or "/tmp/sticky-statusbar-tests")
# Refuse to install anything into a home that is not our scratch directory: this
# module WRITES config.yaml, and writing it into a live profile would wipe that
# profile's settings. conftest forces HERMES_HOME to a fresh temp dir; this is
# the second lock on the same door.
if not HOME.name.startswith("sticky-statusbar-tests-"):
    raise RuntimeError(
        f"refusing to install the plugin into {HOME} — the E2E suite may only "
        "touch its own scratch HERMES_HOME (see tests/conftest.py)"
    )
_installed = HOME / "plugins" / PLUGIN_NAME
if not (_installed / "plugin.yaml").is_file():
    _installed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        PLUGIN_DIR, _installed, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    (HOME / "config.yaml").write_text(
        f"plugins:\n  enabled:\n    - {PLUGIN_NAME}\n", encoding="utf-8"
    )

HERMES_SRC_CANDIDATES = (
    Path("/usr/local/lib/hermes-agent"),
    Path.home() / ".hermes" / "hermes-agent",
)

try:
    for _candidate in HERMES_SRC_CANDIDATES:
        if (_candidate / "hermes_cli" / "plugins.py").is_file():
            if str(_candidate) not in sys.path:
                sys.path.insert(0, str(_candidate))
            break
    from hermes_cli.lifecycle import has_hook, invoke_hook  # noqa: F401
    from hermes_cli.plugins import discover_plugins, get_plugin_manager  # noqa: F401

    HERMES_AVAILABLE = True
except Exception:  # pragma: no cover - bare checkout
    HERMES_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not HERMES_AVAILABLE, reason="Hermes source not importable — run inside a Hermes checkout"
)

HOOKS = (
    "pre_api_request",
    "post_api_request",
    "api_request_error",
    "pre_tool_call",
    "post_tool_call",
    "post_gateway_admission",
    "on_session_end",
)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def loaded():
    """Discover plugins for real and hand back the loaded plugin's module."""
    discover_plugins()
    manager = get_plugin_manager()
    entries = [p for p in manager.list_plugins() if PLUGIN_NAME in str(p.get("key", ""))]
    if not entries:
        pytest.skip(
            f"{PLUGIN_NAME} not enabled in this HERMES_HOME ({HOME}) — "
            "install it into $HERMES_HOME/plugins and re-run"
        )
    # Hermes imports a directory plugin under a namespaced module name
    # (``hermes_plugins.sticky_statusbar``), so find the live object rather than
    # guessing the spelling — and skip the flat alias conftest registered, which
    # is a *different* module object with its own collector.
    candidates = [
        (name, module)
        for name, module in list(sys.modules.items())
        if "sticky_statusbar" in name and hasattr(module, "get_collector")
    ]
    namespaced = [pair for pair in candidates if "." in pair[0]]
    if not namespaced:
        seen = [name for name, _ in candidates]
        pytest.skip(f"the namespaced plugin module is not loaded (saw {seen})")
    return namespaced[0][1]


@pytest.fixture(autouse=True)
def _fresh_state(loaded):
    loaded.get_collector().reset()
    loaded.get_store().clear_all()      # anchors persist in files; tests must not inherit
    yield
    loaded.get_collector().reset()
    loaded.publisher.stop_all()
    loaded.get_store().clear_all()


# A turn as ``agent/turn_response_intake.py`` actually reports it.
USAGE = {
    "input_tokens": 900,
    "output_tokens": 240,
    "cache_read_tokens": 3300,
    "cache_write_tokens": 0,
    "prompt_tokens": 4200,
    "total_tokens": 4440,
}


def fire_a_turn(*, tool="terminal", duration=2.5, platform="telegram"):
    """Fire one realistic turn through the LIVE hook registry."""
    invoke_hook(
        "pre_api_request",
        task_id="t1",
        turn_id="turn-1",
        api_request_id="turn-1:api:1",
        session_id="sess-e2e",
        platform=platform,
        model="gpt-5.6",
        provider="openai",
        api_mode="chat_completions",
        api_call_count=1,
        message_count=12,
        tool_count=88,
        approx_input_tokens=4200,
        max_tokens=4096,
    )
    invoke_hook(
        "post_api_request",
        task_id="t1",
        turn_id="turn-1",
        api_request_id="turn-1:api:1",
        session_id="sess-e2e",
        platform=platform,
        model="gpt-5.6",
        provider="openai",
        api_call_count=1,
        api_duration=duration,
        finish_reason="tool_calls",
        usage=USAGE,
        context_length=200_000,
        assistant_content_chars=880,
        assistant_tool_call_count=2,
    )
    if tool:
        invoke_hook(
            "pre_tool_call",
            tool_name=tool,
            args={"command": "pytest -q"},
            task_id="t1",
            session_id="sess-e2e",
            turn_id="turn-1",
            api_request_id="turn-1:api:1",
            tool_call_id="call-1",
        )
    else:
        invoke_hook(
            "post_tool_call",
            tool_name=tool or "",
            args={},
            result="ok",
            task_id="t1",
            session_id="sess-e2e",
            tool_call_id="call-1",
            duration_ms=0,
            status="ok",
        )


class FakeTelegramMessage:
    message_id = 4242


class FakeTelegramBot:
    """The PTB surface the sink uses, recording instead of sending."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.deleted: list[dict] = []
        self.pins: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return FakeTelegramMessage()

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)
        return True

    async def delete_message(self, **kwargs):
        self.deleted.append(kwargs)
        return True

    async def pin_chat_message(self, **kwargs):
        self.pins.append(kwargs)
        return True


class FakeApplication:
    def __init__(self, bot: FakeTelegramBot) -> None:
        self.bot = bot


class FakeAdapter:
    name = "Telegram"


# ── the plugin is genuinely loaded ───────────────────────────────────────────


class TestLoadedForReal:
    def test_every_declared_hook_is_in_the_live_registry(self, loaded):
        for hook in HOOKS:
            assert has_hook(hook), f"{hook} was not registered by the loaded plugin"

    def test_manifest_matches_what_register_did(self, loaded):
        import yaml

        manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
        registered_hooks = sorted(
            hook for hook in HOOKS if has_hook(hook)
        )
        assert registered_hooks == sorted(manifest["provides_hooks"])
        assert manifest["provides_tools"] == ["agent_status"]

    def test_platform_factories_are_queued_for_connect_time(self, loaded):
        manager = get_plugin_manager()
        for platform in ("telegram", "discord"):
            factories = manager.get_platform_handler_factories(platform)
            assert factories, f"no {platform} factory queued for connect time"

    def test_the_plugin_loaded_without_any_platform_sdk_installed(self, loaded):
        """register() must not need python-telegram-bot or discord.py present."""
        assert loaded.get_config() is not None
        assert not getattr(loaded, "_sdk_imported", False)


# ── the numbers are right ────────────────────────────────────────────────────


class TestLiveNumbers:
    def test_a_real_turn_lands_in_the_snapshot(self, loaded):
        fire_a_turn()
        snap = loaded.get_collector().snapshot()
        assert snap.tps == pytest.approx(96.0)                       # 240 / 2.5
        assert snap.cache_hit_pct == pytest.approx(3300 / 4200 * 100)
        assert snap.context_pct == pytest.approx(4200 / 200_000 * 100)
        assert snap.latency == pytest.approx(2.5)
        assert snap.current_tool == "terminal"
        assert snap.model == "gpt-5.6"
        assert snap.platform == "telegram"
        assert snap.state == "tool"

    def test_finishing_the_tool_moves_it_into_the_ledger(self, loaded):
        fire_a_turn()
        invoke_hook(
            "post_tool_call",
            tool_name="terminal",
            args={"command": "pytest -q"},
            result="ok",
            task_id="t1",
            session_id="sess-e2e",
            tool_call_id="call-1",
            duration_ms=2500,
            status="ok",
        )
        snap = loaded.get_collector().snapshot()
        assert snap.current_tool == ""
        assert snap.tool_calls == 1
        assert snap.tools[-1].hint == "pytest -q"
        assert snap.tools[-1].duration_s == pytest.approx(2.5)

    def test_the_turn_ends_and_stops_claiming_to_work(self, loaded):
        fire_a_turn()
        invoke_hook(
            "on_session_end",
            session_id="sess-e2e",
            task_id="t1",
            turn_id="turn-1",
            completed=True,
            failed=False,
            interrupted=False,
            turn_exit_reason="text_response(stop)",
            model="gpt-5.6",
            platform="telegram",
        )
        snap = loaded.get_collector().snapshot()
        assert snap.state == "idle"
        assert snap.turns == 1


# ── the sticky message itself ────────────────────────────────────────────────


class TestTheSticky:
    def test_the_factory_builds_a_working_publisher_and_posts_a_board(self, loaded):
        bot = FakeTelegramBot()

        async def scenario():
            manager = get_plugin_manager()
            factory = manager.get_platform_handler_factories("telegram")[0][0]
            factory(FakeApplication(bot), FakeAdapter())

            invoke_hook(
                "post_gateway_admission",
                session_key="telegram:-100",
                platform="telegram",
                source={"platform": "telegram", "chat_id": "-100", "chat_type": "group"},
                message_id="5",
                text="how is it going?",
            )
            fire_a_turn()
            pub = loaded.publisher.get("telegram")
            assert pub is not None, "the factory did not wire a publisher"
            await pub.tick()

        asyncio.run(scenario())

        assert len(bot.sent) == 1
        message = bot.sent[0]
        assert message["chat_id"] == -100
        assert message["disable_notification"] is True        # a status never pings
        assert "📊 Hermes" in message["text"]
        assert "terminal" in message["text"]
        assert "96 tok/s" in message["text"]

    def test_the_message_is_pinned_once_and_then_edited_in_place(self, loaded):
        bot = FakeTelegramBot()

        async def scenario():
            manager = get_plugin_manager()
            factory = manager.get_platform_handler_factories("telegram")[0][0]
            factory(FakeApplication(bot), FakeAdapter())
            invoke_hook(
                "post_gateway_admission",
                session_key="telegram:-100",
                platform="telegram",
                source={"platform": "telegram", "chat_id": "-100", "chat_type": "group"},
                message_id="5",
                text="hi",
            )
            pub = loaded.publisher.get("telegram")
            await pub.tick()
            invoke_hook(
                "pre_tool_call", tool_name="read_file", args={"path": "x.py"},
                tool_call_id="c2", session_id="sess-e2e",
            )
            await pub.tick()

        asyncio.run(scenario())

        assert len(bot.sent) == 1                                       # one anchor
        assert len(bot.pins) == 1
        assert bot.pins[0]["disable_notification"] is True
        assert len(bot.edits) == 1
        assert bot.edits[0]["message_id"] == 4242
        assert "read_file" in bot.edits[0]["text"]

    def test_an_unchanged_board_makes_no_api_call(self, loaded):
        bot = FakeTelegramBot()

        async def scenario():
            manager = get_plugin_manager()
            factory = manager.get_platform_handler_factories("telegram")[0][0]
            factory(FakeApplication(bot), FakeAdapter())
            invoke_hook(
                "post_gateway_admission",
                session_key="telegram:-100",
                platform="telegram",
                source={"platform": "telegram", "chat_id": "-100", "chat_type": "group"},
                message_id="5",
                text="hi",
            )
            pub = loaded.publisher.get("telegram")
            await pub.tick(1_000_000.0)
            await pub.tick(1_000_000.0)          # same instant, same state

        asyncio.run(scenario())

        assert len(bot.sent) == 1
        assert bot.edits == []

    def test_auto_tracked_chats_are_the_ones_that_spoke(self, loaded):
        bot = FakeTelegramBot()

        async def scenario():
            manager = get_plugin_manager()
            factory = manager.get_platform_handler_factories("telegram")[0][0]
            factory(FakeApplication(bot), FakeAdapter())
            invoke_hook(
                "post_gateway_admission",
                session_key="telegram:7",
                platform="telegram",
                source={"platform": "telegram", "chat_id": "7", "chat_type": "private"},
                message_id="1",
                text="hello",
            )
            return loaded.publisher.get("telegram").tracked_chats()

        chats = asyncio.run(scenario())
        assert [chat.chat_id for chat in chats] == ["7"]


# ── what a chat member reads, and the on-demand surfaces ─────────────────────


class TestRenderedBoard:
    def test_the_board_carries_every_headline_number(self, loaded):
        fire_a_turn()
        snapshot = loaded.get_collector().snapshot()
        board = importlib.import_module(f"{loaded.__name__}.board")
        # Render a few seconds into the turn so the elapsed-time segment is live
        # (it is deliberately omitted for a turnaround under a second).
        text = board.render_board(snapshot, loaded.get_config(), snapshot.last_activity + 5)
        for expected in ("📊 Hermes", "running ⧉ terminal", "⚡ 96 tok/s", "◎ 79% cache",
                         "ctx", "☤ gpt-5.6", "⏱ 5s"):
            assert expected in text, f"{expected!r} missing from:\n{text}"

    def test_the_board_respects_the_character_cap(self, loaded):
        fire_a_turn()
        board = importlib.import_module(f"{loaded.__name__}.board")
        text = board.render_board(loaded.get_collector().snapshot(), loaded.get_config())
        assert len(text) <= loaded.get_config().max_chars

    def test_the_slash_command_answers_with_the_live_board(self, loaded):
        fire_a_turn()
        text = loaded._handle_sticky("")
        assert "**Sticky statusbar**" in text
        assert "terminal" in text
        assert "sticky-statusbar:" in text

    def test_the_tool_reports_live_numbers_through_the_registry(self, loaded):
        fire_a_turn()
        status_tool = importlib.import_module(f"{loaded.__name__}.status_tool")
        payload = json.loads(status_tool.agent_status({}, task_id="t1", session_id="sess-e2e"))
        assert payload["ok"] is True
        assert payload["tokens_per_second"] == pytest.approx(96.0)
        assert payload["cache_hit_pct"] == pytest.approx(3300 / 4200 * 100, abs=0.01)
        assert payload["current_tool"] == "terminal"
        assert payload["state"] == "tool"
