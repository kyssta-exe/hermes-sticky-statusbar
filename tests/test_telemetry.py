"""Telemetry maths and defensive coercion.

The numbers here are the same ones the desktop status bar shows, so the tests
pin the *formulas* (windowed throughput, cache-hit ratio) rather than a sample
value: a change that makes the sticky disagree with the status bar should fail
here.
"""

from __future__ import annotations

import json
import time

import pytest
from sticky_statusbar.telemetry import (
    TURN_LEASE_SECONDS,
    MetricsCollector,
    Snapshot,
    hint_for,
)


@pytest.fixture
def collector():
    return MetricsCollector(window=4)


def usage(output=0, prompt=0, cache_read=0, cache_write=0, input_tokens=0):
    return {
        "output_tokens": output,
        "prompt_tokens": prompt,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "input_tokens": input_tokens,
    }


# ── throughput ───────────────────────────────────────────────────────────────


def test_throughput_is_tokens_over_time_not_a_mean_of_ratios(collector):
    """A single slow request must not tank the number the way averaging
    per-request ratios would."""
    collector.on_post_api_request(usage=usage(output=100, prompt=1000), api_duration=1.0)
    collector.on_post_api_request(usage=usage(output=300, prompt=1000), api_duration=2.0)
    snap = collector.snapshot()
    assert snap.tps == pytest.approx(400 / 3)
    assert snap.instant_tps == pytest.approx(150.0)


def test_zero_duration_samples_are_dropped_from_throughput(collector):
    collector.on_post_api_request(usage=usage(output=1000, prompt=10), api_duration=0.0)
    collector.on_post_api_request(usage=usage(output=100, prompt=10), api_duration=1.0)
    snap = collector.snapshot()
    assert snap.tps == pytest.approx(100.0)
    assert snap.requests == 2          # still visible to cache and context
    assert snap.latency == pytest.approx(1.0)


def test_negative_and_absurd_durations_are_ignored(collector):
    collector.on_post_api_request(usage=usage(output=100), api_duration=-4.0)
    collector.on_post_api_request(usage=usage(output=100), api_duration=float("inf"))
    snap = collector.snapshot()
    assert snap.tps == 0.0
    assert snap.latency == 0.0


# ── cache and context ────────────────────────────────────────────────────────


def test_cache_hit_is_reads_over_the_whole_prompt(collector):
    collector.on_post_api_request(usage=usage(output=1, prompt=1000, cache_read=750))
    assert collector.snapshot().cache_hit_pct == pytest.approx(75.0)


def test_no_cached_reads_reports_none_not_zero(collector):
    """No data is not the same thing as a bad cache regime."""
    collector.on_post_api_request(usage=usage(output=1, prompt=500))
    assert collector.snapshot().cache_hit_pct is None


def test_prompt_is_derived_when_the_provider_only_reports_the_parts(collector):
    collector.on_post_api_request(
        usage={"input_tokens": 100, "cache_read_tokens": 300, "output_tokens": 5}
    )
    snap = collector.snapshot()
    assert snap.context_used == 400
    assert snap.cache_hit_pct == pytest.approx(75.0)


def test_context_percentage_uses_the_window_the_provider_declared(collector):
    collector.on_pre_api_request(context_length=200_000)
    collector.on_post_api_request(usage=usage(output=1, prompt=50_000), api_duration=1.0)
    snap = collector.snapshot()
    assert snap.context_pct == pytest.approx(25.0)
    assert snap.context_length == 200_000


def test_a_smaller_prompt_after_a_large_one_counts_as_a_compression(collector):
    collector.on_post_api_request(usage=usage(output=1, prompt=40_000), api_duration=1.0)
    collector.on_post_api_request(usage=usage(output=1, prompt=4_000), api_duration=1.0)
    assert collector.snapshot().compressions == 1


def test_a_merely_shorter_reply_is_not_a_compression(collector):
    collector.on_post_api_request(usage=usage(output=1, prompt=40_000), api_duration=1.0)
    collector.on_post_api_request(usage=usage(output=1, prompt=30_000), api_duration=1.0)
    assert collector.snapshot().compressions == 0


# ── the tool ledger ──────────────────────────────────────────────────────────


def test_a_finished_tool_lands_in_the_ledger_with_its_duration(collector):
    collector.on_pre_tool_call(tool_name="terminal", args={"command": "pytest -q"},
                               tool_call_id="c1")
    assert collector.snapshot().current_tool == "terminal"
    collector.on_post_tool_call(tool_name="terminal", args={"command": "pytest -q"},
                                tool_call_id="c1", duration_ms=2500, status="ok")
    snap = collector.snapshot()
    assert snap.current_tool == ""
    assert snap.tool_calls == 1
    assert snap.tools[-1].name == "terminal"
    assert snap.tools[-1].duration_s == pytest.approx(2.5)
    assert snap.tools[-1].ok is True
    assert snap.tools[-1].hint == "pytest -q"


def test_a_failed_tool_is_marked_and_counted(collector):
    collector.on_pre_tool_call(tool_name="write_file", args={}, tool_call_id="c1")
    collector.on_post_tool_call(tool_name="write_file", tool_call_id="c1", status="error",
                                duration_ms=10)
    snap = collector.snapshot()
    assert snap.tools[-1].ok is False
    assert snap.tool_errors == 1


def test_status_is_derived_from_the_result_when_the_host_omits_it(collector):
    collector.on_post_tool_call(tool_name="terminal", result="Error: no such file")
    assert collector.snapshot().tools[-1].ok is False


def test_a_tool_without_ids_is_still_recorded(collector):
    collector.on_pre_tool_call(tool_name="web_search", args={"query": "weather"})
    collector.on_post_tool_call(tool_name="web_search", duration_ms=100)
    snap = collector.snapshot()
    assert snap.tools[-1].name == "web_search"
    assert snap.current_tool == ""


def test_a_nested_call_is_not_mis_attributed(collector):
    collector.on_pre_tool_call(tool_name="outer", args={}, tool_call_id="a")
    collector.on_pre_tool_call(tool_name="inner", args={}, tool_call_id="b")
    collector.on_post_tool_call(tool_name="inner", tool_call_id="b", duration_ms=5)
    snap = collector.snapshot()
    assert snap.tools[-1].name == "inner"
    assert snap.current_tool == "outer"          # the outer call is still running


def test_the_ledger_keeps_only_the_configured_number_of_runs(collector):
    collector.set_tool_history(2)
    for index in range(4):
        collector.on_post_tool_call(tool_name=f"tool{index}", duration_ms=1, status="ok")
    names = [run.name for run in collector.snapshot().tools]
    assert names == ["tool2", "tool3"]


def test_tool_history_zero_keeps_no_ledger(collector):
    collector.set_tool_history(0)
    collector.on_post_tool_call(tool_name="terminal", duration_ms=1)
    assert collector.snapshot().tools == ()


# ── hints ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"command": "ls -la\nsecond line"}, "ls -la"),
        ({"path": "/tmp/deep/file.py"}, "file.py"),
        ({"file_path": "src\\win\\thing.ts"}, "thing.ts"),
        ({"query": "weather in oslo"}, "weather in oslo"),
        ({"unrelated": "x"}, ""),
        ({}, ""),
        (None, ""),
        ("just-a-string", ""),
        (["a", "b"], ""),
        ({"command": 42}, "42"),
        ({"command": "x" * 100}, "x" * 40),
    ],
)
def test_hint_for(args, expected):
    assert hint_for(args) == expected


# ── state ────────────────────────────────────────────────────────────────────


def test_state_walks_from_idle_to_thinking_to_tool(collector):
    assert collector.snapshot().state == "idle"
    collector.on_pre_api_request(model="m", context_length=1000)
    assert collector.snapshot().state == "thinking"
    collector.on_post_api_request(usage=usage(output=1, prompt=10), api_duration=1.0)
    assert collector.snapshot().state == "working"
    collector.on_pre_tool_call(tool_name="terminal", args={}, tool_call_id="c")
    assert collector.snapshot().state == "tool"


def test_a_turn_that_dies_without_ending_still_stops_claiming_to_work(collector):
    collector.on_pre_api_request(model="m")
    collector.on_post_api_request(usage=usage(output=1, prompt=10), api_duration=1.0)
    snap = collector.snapshot()
    assert snap.busy_at(snap.last_activity + 10) is True
    assert snap.busy_at(snap.last_activity + TURN_LEASE_SECONDS + 1) is False


def test_turn_end_counts_the_turn_and_clears_the_working_state(collector):
    collector.on_turn_start(platform="telegram")
    collector.on_pre_tool_call(tool_name="terminal", args={}, tool_call_id="c")
    collector.on_turn_end(completed=True)
    snap = collector.snapshot()
    assert snap.turns == 1
    assert snap.failed_turns == 0
    assert snap.current_tool == ""
    assert snap.state == "idle"


def test_a_failed_turn_is_counted_separately(collector):
    collector.on_turn_end(failed=True, interrupted=True)
    snap = collector.snapshot()
    assert (snap.turns, snap.failed_turns) == (1, 1)


def test_elapsed_time_only_runs_while_the_turn_does(collector):
    collector.on_turn_start()
    snap = collector.snapshot()
    assert snap.elapsed_seconds(snap.turn_started + 30) == pytest.approx(30.0)
    collector.on_turn_end()
    assert collector.snapshot().elapsed_seconds(None) == 0.0


def test_api_errors_are_counted_and_clear_the_in_flight_marker(collector):
    collector.on_pre_api_request(model="m")
    collector.on_api_request_error(error="boom")
    snap = collector.snapshot()
    assert snap.errors == 1
    assert snap.inflight is False


# ── defensive coercion ───────────────────────────────────────────────────────


def test_impossible_payloads_do_not_raise_and_do_not_corrupt_the_numbers(collector):
    collector.on_post_api_request(usage=["not", "a", "dict"], api_duration="soon")
    collector.on_post_api_request(usage={"output_tokens": float("inf"),
                                         "prompt_tokens": -5}, api_duration=1.0)
    collector.on_pre_tool_call(tool_name=None, args=None, tool_call_id=None)
    collector.on_post_tool_call(tool_name=None, duration_ms=float("inf"))
    collector.on_pre_api_request(context_length="lots")
    snap = collector.snapshot()
    assert snap.api_calls == 2
    assert snap.total_tokens >= 0
    assert snap.tps >= 0
    assert snap.context_pct is None


def test_a_window_sized_by_config_never_drops_below_one_sample(collector):
    tiny = MetricsCollector(window=0)
    tiny.on_post_api_request(usage=usage(output=10, prompt=10), api_duration=1.0)
    assert tiny.snapshot().requests == 1


def test_snapshot_is_json_safe(collector):
    collector.on_pre_tool_call(tool_name="terminal", args={"command": "ls"}, tool_call_id="c")
    collector.on_post_tool_call(tool_name="terminal", tool_call_id="c", duration_ms=5)
    payload = json.dumps(collector.snapshot().as_dict())
    assert "terminal" in payload


def test_reset_clears_everything(collector):
    collector.on_post_api_request(usage=usage(output=10, prompt=10), api_duration=1.0)
    collector.on_turn_end()
    collector.reset()
    snap = collector.snapshot()
    assert snap == Snapshot(started_at=snap.started_at)
    assert snap.tools == ()


def test_started_at_is_wall_clock(collector):
    assert abs(time.time() - collector.snapshot().started_at) < 5
