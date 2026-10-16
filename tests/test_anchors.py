"""The anchor record: which message is the sticky, per chat.

A stale record is survivable (the edit fails and the publisher reposts), but a
*lost* record is not: without it every restart posts another status message and
the chat fills with dead boards.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from sticky_statusbar.anchors import AnchorStore, anchor_key, data_dir


def test_round_trip(tmp_path):
    store = AnchorStore(tmp_path / "a.json")
    store.set("telegram", "-100", "55")
    assert store.get("telegram", "-100") == "55"
    assert len(store) == 1


def test_two_platforms_can_share_a_chat_id(tmp_path):
    """Telegram and Discord both use numeric chat ids — the key must not collide."""
    store = AnchorStore(tmp_path / "a.json")
    store.set("telegram", "42", "t1")
    store.set("discord", "42", "d1")
    assert store.get("telegram", "42") == "t1"
    assert store.get("discord", "42") == "d1"


def test_unknown_chat_is_empty_not_an_error(tmp_path):
    store = AnchorStore(tmp_path / "a.json")
    assert store.get("telegram", "nothing-here") == ""


def test_records_survive_a_restart(tmp_path):
    path = tmp_path / "a.json"
    AnchorStore(path).set("telegram", "42", "9")
    assert AnchorStore(path).get("telegram", "42") == "9"


def test_clear_forgets_one_chat(tmp_path):
    store = AnchorStore(tmp_path / "a.json")
    store.set("telegram", "42", "9")
    store.clear("telegram", "42")
    assert store.get("telegram", "42") == ""
    assert json.loads((tmp_path / "a.json").read_text()) == {}


def test_a_corrupt_file_is_ignored_rather_than_fatal(tmp_path):
    path = tmp_path / "a.json"
    path.write_text("{not json at all")
    store = AnchorStore(path)
    assert store.get("telegram", "42") == ""
    store.set("telegram", "42", "1")          # and it self-heals on the next write
    assert json.loads(path.read_text()) == {"telegram:42": "1"}


def test_a_json_file_of_the_wrong_shape_is_ignored(tmp_path):
    path = tmp_path / "a.json"
    path.write_text('["not", "a", "map"]')
    assert AnchorStore(path).all() == {}


def test_non_string_values_are_coerced_or_dropped(tmp_path):
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"telegram:1": 77, "telegram:2": None, "3": "x"}))
    anchors = AnchorStore(path).all()
    assert anchors == {"telegram:1": "77", "3": "x"}


def test_writing_the_same_anchor_twice_does_not_touch_the_file(tmp_path):
    path = tmp_path / "a.json"
    store = AnchorStore(path)
    store.set("telegram", "42", "1")
    before = path.stat().st_mtime_ns
    time.sleep(0.01)
    store.set("telegram", "42", "1")
    assert path.stat().st_mtime_ns == before


def test_an_unwritable_path_does_not_raise(tmp_path):
    store = AnchorStore(tmp_path / "nope" / "a.json")   # parent does not exist yet
    store.set("telegram", "42", "1")                    # mkdir -p happens on write
    assert (tmp_path / "nope" / "a.json").is_file()

    blocked = AnchorStore(Path("/proc/1/cannot-write-here/a.json"))
    blocked.set("telegram", "42", "1")                  # best effort, no exception
    assert blocked.get("telegram", "42") == "1"         # still correct in memory


def test_key_is_platform_qualified():
    assert anchor_key("telegram", " 42 ") == "telegram:42"
    assert anchor_key("TELEGRAM", "42") == "telegram:42"


def test_data_dir_follows_the_active_hermes_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert data_dir() == tmp_path / "plugin-data" / "sticky-statusbar"


def test_data_dir_is_never_the_bare_home(tmp_path):
    """The store must live in a plugin-owned subdirectory, not next to config.yaml."""
    assert data_dir().parts[-2:] == ("plugin-data", "sticky-statusbar")
    assert os.sep in str(data_dir())
