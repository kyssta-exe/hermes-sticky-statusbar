"""Redaction at the chat boundary.

A normal Hermes reply is redacted on its way to the platform; a message this
plugin posts is not, because it goes out through the platform client directly.
These tests pin the two things that matter: a credential-shaped string never
reaches the chat, and ordinary board text is left exactly as rendered.
"""

from __future__ import annotations

import asyncio

import pytest
import sticky_statusbar.redact as redact

ORDINARY_BOARD = (
    "📊 Hermes · running ⧉ terminal pytest -q\n"
    "⚡ 96 tok/s · ◎ 79% cache · ▰▰▱▱▱▱▱▱▱▱ 24.8K/200.0K ctx · ☤ deepseek-v4.1-flash\n"
    "⏱ 42s · ↻ 3 turns · 12 calls · 5 tools"
)


def run(coro):
    return asyncio.run(coro)


# ── what must never survive ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "secret",
    [
        "sk-live-abcdef0123456789",
        "sk-ant-api03-abcdefghijklmnop",
        "ghp_0123456789abcdefghijklmnop",
        "github_pat_11ABCDEFG0123456789_abcdefghijklmnop",
        "xoxb-1234567890-abcdefghijkl",
        "AKIAIOSFODNN7EXAMPLE",
        "AIzaSyA1234567890abcdefghijklmnopqrst",
    ],
)
def test_prefixed_credentials_are_masked(secret):
    scrubbed = redact.scrub(f"⧉ terminal curl --key {secret}")
    assert secret not in scrubbed
    assert redact.MASK in scrubbed


@pytest.mark.parametrize(
    ("command", "value"),
    [
        ('curl -H "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcdefgh"',
         "eyJhbGciOiJIUzI1NiJ9.abcdefgh"),
        ("env ANTHROPIC_API_KEY=abcdef0123456789abcdef python run.py",
         "abcdef0123456789abcdef"),
        ("mysql --password hunter2secretvalue1234", "hunter2secretvalue1234"),
        ("deploy --client_secret: 0123456789abcdefghij", "0123456789abcdefghij"),
        ("aws s3 cp --token AAAAbbbbCCCCddddEEEE1234", "AAAAbbbbCCCCddddEEEE1234"),
    ],
)
def test_credential_shaped_assignments_lose_their_value(command, value):
    scrubbed = redact.scrub(f"⧉ terminal {command}")
    assert value not in scrubbed, command
    assert redact.MASK in scrubbed
    assert "⧉ terminal " in scrubbed            # the useful part is still there


def test_url_userinfo_is_masked():
    scrubbed = redact.scrub("⧉ terminal git clone https://user:ghp_secretvalue@host/repo")
    assert "user:ghp_secretvalue" not in scrubbed


def test_a_long_opaque_blob_is_masked():
    blob = "abcdef0123456789abcdef0123456789abcdef"
    assert blob not in redact.scrub(f"key {blob}")


def test_a_long_commit_sha_is_treated_as_opaque():
    """Documented trade: a 40-hex sha in a command hint is masked. A status line
    loses nothing important, and a real key looks exactly the same."""
    sha = "5e3e7705de505c031595bc4d03e96d9187417f40"
    assert sha not in redact.scrub(f"git checkout {sha}")


# ── what must survive untouched ──────────────────────────────────────────────


def test_ordinary_board_text_is_untouched():
    assert redact.scrub(ORDINARY_BOARD) == ORDINARY_BOARD


@pytest.mark.parametrize(
    "text",
    [
        "v1.2.3-rc.1",
        "/usr/local/lib/hermes-agent",
        "GPT-5.6",
        "sk-",
        "token: none",
        "password required",
        "12345678901234567890123456789012345678",        # pure digits, not a token
        "read_file 0.4s config.py",
    ],
)
def test_ordinary_fragments_survive(text):
    assert redact.scrub(text) == text


def test_empty_and_non_string_input_is_passed_through():
    assert redact.scrub("") == ""
    assert redact.scrub(None) is None
    assert redact.scrub(42) == 42


def test_scrub_never_raises_on_hostile_input():
    assert redact.scrub("sk-" * 500)
    assert redact.scrub("\x00\x01://:@")
    assert redact.scrub("Authorization: Bearer " + "x" * 10_000)


# ── the boundary the publisher actually uses ─────────────────────────────────


class RecordingSink:
    platform = "telegram"
    supports_pin = False

    def __init__(self) -> None:
        self.sent: list[str] = []

    def available(self) -> bool:
        return True

    async def send(self, chat_id: str, text: str, thread_id: str = "") -> str:
        self.sent.append(text)
        return "m1"

    async def edit(self, chat_id: str, message_id: str, text: str) -> bool:
        self.sent.append(text)
        return True

    async def delete(self, chat_id: str, message_id: str) -> bool:
        return True


def test_a_secret_in_a_tool_hint_never_reaches_the_message(collector, store):
    """The board echoes the first characters of a command, which is exactly where
    an inline credential would sit."""
    from sticky_statusbar.publisher import StickyPublisher
    from sticky_statusbar.settings import StickyConfig

    sink = RecordingSink()
    publisher = StickyPublisher("telegram", sink, collector, StickyConfig(), store=store)
    publisher.track("-100")
    collector.on_pre_tool_call(
        tool_name="terminal",
        args={"command": "curl -H 'Authorization: Bearer sk-live-abcdef0123456789'"},
        tool_call_id="c1",
    )
    assert run(publisher.tick()) == 1
    published = sink.sent[0]
    assert "sk-live-abcdef0123456789" not in published
    assert "terminal" in published            # the board itself is still informative


def test_a_normal_board_is_published_unredacted(collector, store):
    from sticky_statusbar.publisher import StickyPublisher
    from sticky_statusbar.settings import StickyConfig

    sink = RecordingSink()
    publisher = StickyPublisher("telegram", sink, collector, StickyConfig(), store=store)
    publisher.track("-100")
    collector.on_pre_tool_call(tool_name="read_file", args={"path": "config.py"},
                               tool_call_id="c1")
    run(publisher.tick())
    published = sink.sent[0]
    assert "read_file" in published
    assert "config.py" in published
    assert redact.MASK not in published


def test_the_rendered_board_still_respects_the_character_cap_after_scrubbing():
    from sticky_statusbar import board
    from sticky_statusbar.settings import StickyConfig
    from sticky_statusbar.telemetry import Snapshot, ToolRun

    snapshot = Snapshot(model="m", tools=(ToolRun(name="terminal", ok=True),))
    config = StickyConfig(max_chars=200)
    assert len(redact.scrub(board.render_board(snapshot, config))) <= 200
