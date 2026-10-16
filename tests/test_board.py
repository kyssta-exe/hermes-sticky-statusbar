"""Rendering: what a person scrolling past the sticky actually reads."""

from __future__ import annotations

from dataclasses import replace

from sticky_statusbar import board
from sticky_statusbar.settings import StickyConfig
from sticky_statusbar.telemetry import Snapshot, ToolRun


def snapshot(**kwargs) -> Snapshot:
    base = dict(
        model="deepseek-v4.1-flash",
        provider="opencode-go",
        platform="telegram",
        turn_live=True,
        context_used=24_000,
        context_length=200_000,
        context_pct=12.0,
        tps=96.4,
        instant_tps=91.0,
        cache_hit_pct=79.0,
        latency=1.4,
        api_calls=12,
        requests=12,
        total_tokens=1_200_000,
        turns=3,
        tool_calls=5,
        turn_started=1_000_000.0,
        last_activity=1_000_040.0,
    )
    base.update(kwargs)
    return Snapshot(**base)


NOW = 1_000_042.0


# ── helpers ──────────────────────────────────────────────────────────────────


def test_compact_tokens():
    assert board._compact_tokens(0) == "0"
    assert board._compact_tokens(999) == "999"
    assert board._compact_tokens(1_234) == "1.2K"
    assert board._compact_tokens(1_200_000) == "1.2M"
    assert board._compact_tokens(2_500_000_000) == "2.5B"


def test_context_bar_is_clamped_and_exact():
    assert board.build_context_bar(0, 4) == "▱▱▱▱"
    assert board.build_context_bar(100, 4) == "▰▰▰▰"
    assert board.build_context_bar(50, 4) == "▰▰▱▱"
    assert board.build_context_bar(None, 4) == "▱▱▱▱"
    assert len(board.build_context_bar(12.0, 10)) == 10
    assert board.build_context_bar(-5, 4) == "▱▱▱▱"
    assert board.build_context_bar(500, 4) == "▰▰▰▰"


def test_fmt_duration():
    assert board.fmt_duration(0) == "0s"
    assert board.fmt_duration(42) == "42s"
    assert board.fmt_duration(192) == "3m12s"
    assert board.fmt_duration(7_500) == "2h05m"
    assert board.fmt_duration(-3) == "0s"


def test_fmt_ago_is_coarse_on_purpose():
    """The age is the only piece that would change every tick; minute
    granularity is what makes an idle board cost zero API calls."""
    assert board.fmt_ago(0) == "just now"
    assert board.fmt_ago(44) == "just now"
    assert board.fmt_ago(50) == "1m ago"
    assert board.fmt_ago(600) == "10m ago"
    assert board.fmt_ago(7_200) == "2h ago"


# ── the board ────────────────────────────────────────────────────────────────


def test_idle_board_is_short_and_quiet():
    idle = snapshot(turn_live=False, inflight=False, context_pct=None, tps=0.0, turns=0,
                    api_calls=0, tool_calls=0, model="", current_tool="", last_activity=0.0,
                    cache_hit_pct=None, latency=0.0)
    text = board.render_board(idle, StickyConfig(), NOW)
    lines = text.splitlines()
    assert lines[0].startswith("📊 Hermes")
    assert "idle · waiting for you" in lines[0]
    assert len(lines) == 1
    assert "⧉" not in text


def test_working_board_shows_state_tools_metrics_and_counters():
    text = board.render_board(snapshot(current_tool="terminal",
                                       current_tool_hint="pytest -q",
                                       current_tool_started=NOW - 2.0), StickyConfig(), NOW)
    lines = text.splitlines()
    assert "running ⧉ terminal pytest -q" in lines[0]
    assert "⏱ 42s" in text
    assert "⧉ terminal (2s) pytest -q" in text
    assert "⚡ 96 tok/s" in text
    assert "◎ 79% cache" in text
    assert "24.0K/200.0K ctx" in text
    assert "☤ deepseek-v4.1-flash" in text
    assert "↻ 3 turns" in text
    assert "12 calls" in text


def test_thinking_outranks_the_metrics_on_the_header():
    text = board.render_board(snapshot(inflight=True, current_tool=""), StickyConfig(), NOW)
    assert "thinking…" in text.splitlines()[0]


def test_tool_ledger_marks_success_and_failure_newest_last():
    snap = snapshot(
        tools=(
            ToolRun(name="read_file", ok=True, duration_s=0.4, hint="main.py"),
            ToolRun(name="write_file", ok=False, duration_s=0.2, hint="broken.py"),
        )
    )
    line = board.tool_line(snap, StickyConfig(), NOW)
    assert "✓ read_file 0.4s main.py" in line
    assert "✗ write_file 0.2s broken.py" in line
    assert line.index("read_file") < line.index("write_file")


def test_tool_history_length_is_configurable():
    snap = snapshot(tools=tuple(
        ToolRun(name=f"tool{i}", ok=True, duration_s=0.1) for i in range(6)
    ))
    line = board.tool_line(snap, replace(StickyConfig(), tool_history=2), NOW)
    assert "tool4" in line and "tool5" in line
    assert "tool0" not in line


def test_tool_history_can_be_switched_off():
    snap = snapshot(tools=(ToolRun(name="terminal", ok=True, duration_s=1.0),))
    line = board.tool_line(snap, replace(StickyConfig(), tool_history=0), NOW)
    assert line == ""


def test_show_tools_off_drops_the_ledger_line():
    snap = snapshot(current_tool="terminal", tools=(ToolRun(name="read_file", ok=True),))
    text = board.render_board(snap, replace(StickyConfig(), show_tools=False), NOW)
    assert "terminal" not in text
    assert "read_file" not in text
    assert "working" in text.splitlines()[0]     # the state is still reported


def test_show_metrics_off_keeps_the_model_line():
    text = board.render_board(snapshot(), replace(StickyConfig(), show_metrics=False), NOW)
    assert "⚡" not in text
    assert "◎" not in text
    assert "☤" in text


def test_a_segment_with_no_data_is_omitted_not_shown_as_placeholder():
    snap = snapshot(cache_hit_pct=None, tps=0.0, context_pct=None)
    text = board.render_board(snap, StickyConfig(), NOW)
    assert "cache" not in text
    assert "--" not in text


def test_footer_switches_to_age_when_idle():
    snap = snapshot(turn_live=False, last_activity=NOW - 600)
    text = board.render_board(snap, StickyConfig(), NOW)
    assert "⌛ 10m ago" in text


def test_board_is_capped_at_max_chars():
    snap = snapshot(current_tool="t" * 300, current_tool_hint="h" * 300,
                    tools=tuple(ToolRun(name="x" * 50, ok=True, duration_s=1.0) for _ in range(5)))
    text = board.render_board(snap, replace(StickyConfig(), max_chars=120), NOW)
    assert len(text) <= 120
    assert text.endswith("…")


def test_the_same_state_renders_byte_identical_text_within_an_age_bucket():
    """The publisher skips an edit when the text is unchanged, so an idle board
    that re-rendered differently every tick would burn the chat's rate limit.
    The age footer is at minute granularity precisely so this holds."""
    idle = snapshot(turn_live=False, last_activity=1_000_000.0)
    config = StickyConfig()
    first = board.render_board(idle, config, 1_000_065.0)
    assert "1m ago" in first
    for tick in range(1, 50):                   # 1_000_065 → 1_000_114, same bucket
        assert board.render_board(idle, config, 1_000_065.0 + tick) == first


def test_an_empty_snapshot_renders_without_raising():
    text = board.render_board(Snapshot(), StickyConfig(), NOW)
    assert text
    assert "Hermes" in text


# ── the /sticky report ───────────────────────────────────────────────────────


def test_report_has_the_sections_the_slash_command_promises():
    snap = snapshot(current_tool="terminal",
                    tools=(ToolRun(name="terminal", ok=True, duration_s=1.0, hint="ls"),))
    text = board.render_report(snap, StickyConfig())
    assert "**Sticky statusbar**" in text
    assert "State: **running ⧉ terminal**" in text
    assert "tok/s" in text
    assert "Cache hit" in text
    assert "Recent tools" in text
    assert "✓ `terminal` 1.00s `ls`" in text
    assert "12 API calls · 0 errors" in text
    assert "5 tool calls" in text


def test_report_says_so_when_there_is_no_cache_data():
    text = board.render_report(snapshot(cache_hit_pct=None), StickyConfig())
    assert "no cached reads" in text
