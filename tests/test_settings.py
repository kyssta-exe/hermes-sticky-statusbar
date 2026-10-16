"""Settings: coercion, platform floors, and the manifest↔dataclass contract."""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from sticky_statusbar.settings import (
    PLATFORM_MIN_INTERVAL,
    StickyConfig,
    describe,
    parse_chats,
)


class StubCtx:
    """A ``PluginContext`` that only answers ``get_config``."""

    def __init__(self, values: dict | None = None) -> None:
        self.values = values or {}

    def get_config(self, key: str, default=None):
        return self.values.get(key, default)


# ── the manifest contract ────────────────────────────────────────────────────


def test_every_config_schema_key_is_a_real_field():
    """A schema key that is not a dataclass field is silent drift: the Settings
    form writes one name while the publisher reads another, and every value
    falls back to its default."""
    manifest = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "sticky-statusbar" / "plugin.yaml").read_text()
    )
    fields = {f.name for f in dataclasses.fields(StickyConfig)}
    schema = set(manifest["config_schema"])
    assert schema == fields, f"schema-only={schema - fields} fields-only={fields - schema}"


def test_config_schema_defaults_match_the_dataclass():
    manifest = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "sticky-statusbar" / "plugin.yaml").read_text()
    )
    default = StickyConfig()
    for key, spec in manifest["config_schema"].items():
        if "default" not in spec:
            continue
        declared = spec["default"]
        actual = getattr(default, key)
        if isinstance(actual, tuple):
            actual = ""
        assert declared == actual, f"{key}: schema={declared!r} dataclass={actual!r}"


# ── parsing ──────────────────────────────────────────────────────────────────


def test_defaults_are_the_documented_ones():
    config = StickyConfig()
    assert config.enabled is True
    assert config.mode == "edit"
    assert config.interval_seconds == 5
    assert config.pin_telegram is True
    assert config.auto_track is True
    assert config.chats == ()
    assert config.max_chars == 1800


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ()),
        ("telegram:42", ("telegram:42",)),
        ("telegram:42, discord:99", ("telegram:42", "discord:99")),
        ("Telegram:42\n\n  discord:99  ", ("telegram:42", "discord:99")),
        ("telegram:-100123;discord:7", ("telegram:-100123", "discord:7")),
        ("nonsense\nno-colon-here", ()),
        ("telegram:42,telegram:42", ("telegram:42",)),      # de-duplicated
        (None, ()),
        (42, ()),
    ],
)
def test_parse_chats(raw, expected):
    assert parse_chats(raw) == expected


def test_from_ctx_reads_every_documented_key():
    ctx = StubCtx(
        {
            "enabled": False,
            "mode": "BUMP",
            "header": "  custom  ",
            "interval_seconds": 30,
            "pin_telegram": "no",
            "auto_track": "yes",
            "chats": "telegram:1",
            "show_tools": 0,
            "tool_history": 7,
            "show_metrics": True,
            "show_model": True,
            "show_elapsed": True,
            "show_totals": 1,
            "idle_text": "nothing happening",
            "bar_width": 6,
            "separator": " | ",
            "max_chars": 500,
        }
    )
    config = StickyConfig.from_ctx(ctx)
    assert config.enabled is False
    assert config.mode == "bump"
    assert config.header == "custom"
    assert config.interval_seconds == 30
    assert config.pin_telegram is False
    assert config.auto_track is True
    assert config.chats == ("telegram:1",)
    assert config.show_tools is False
    assert config.tool_history == 7
    assert config.show_totals is True
    assert config.bar_width == 6
    assert config.separator == " | "
    assert config.max_chars == 500


def test_a_typo_falls_back_to_the_default_instead_of_raising():
    ctx = StubCtx(
        {
            "mode": "lsdskjf",
            "interval_seconds": "abc",
            "bar_width": -4,
            "tool_history": 999,
            "max_chars": 1,
            "enabled": "maybe",
            "separator": "",
        }
    )
    config = StickyConfig.from_ctx(ctx)
    assert config.mode == "edit"
    assert config.interval_seconds == 5
    assert config.bar_width == 3          # clamped to the floor, never negative
    assert config.tool_history == 10      # clamped to the ceiling
    assert config.max_chars == 200
    assert config.enabled is True
    assert config.separator == " · "


def test_a_ctx_without_get_config_still_yields_a_working_config():
    assert StickyConfig.from_ctx(object()) == StickyConfig()


def test_a_raising_get_config_never_propagates():
    class Angry:
        def get_config(self, key, default=None):
            raise RuntimeError("config store exploded")

    assert StickyConfig.from_ctx(Angry()) == StickyConfig()


# ── interval floors ──────────────────────────────────────────────────────────


def test_interval_is_floored_per_platform():
    config = replace(StickyConfig(), interval_seconds=2)
    assert config.interval_for("telegram") == 3
    assert config.interval_for("discord") == 5
    assert config.interval_for("slack") == 3
    assert config.interval_for("a-platform-we-never-met") == 5


def test_a_longer_interval_is_never_shortened():
    config = replace(StickyConfig(), interval_seconds=120)
    assert config.interval_for("telegram") == 120


def test_every_floored_platform_is_at_least_two_seconds():
    assert all(value >= 2 for value in PLATFORM_MIN_INTERVAL.values())


# ── summaries ────────────────────────────────────────────────────────────────


def test_describe_mentions_mode_and_interval():
    text = describe(replace(StickyConfig(), mode="bump", interval_seconds=9))
    assert "bump" in text
    assert "9s" in text


def test_describe_marks_disabled():
    assert "off" in describe(replace(StickyConfig(), enabled=False))
