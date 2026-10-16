"""Durable record of which message is the sticky, per chat.

Without it, a gateway restart loses the message id and the next tick posts a
*second* status message, leaving the stale one (with stale numbers) sitting in
the chat forever. The record is tiny and self-healing: if the stored message was
deleted by hand — or the record is wrong for any reason — the platform's edit
fails and the publisher posts a fresh anchor over it.

Stored at ``$HERMES_HOME/plugin-data/sticky-statusbar/anchors.json`` (the plugin
data directory, resolved through Hermes's own home helper so a profile gets its
own file). Writes are atomic: a crash mid-write must not leave a truncated JSON
that makes every later tick post a duplicate.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

PLUGIN_ID = "sticky-statusbar"
_FILENAME = "anchors.json"
_MAX_ENTRIES = 500


def data_dir() -> Path:
    """The plugin's own data directory, under the active Hermes home."""
    try:
        from hermes_constants import get_hermes_home  # profile-aware by design

        home = Path(get_hermes_home())
    except Exception:  # pragma: no cover - very old host / bare checkout
        env = (os.environ.get("HERMES_HOME") or "").strip()
        home = Path(env) if env else Path.home() / ".hermes"
    return home / "plugin-data" / PLUGIN_ID


def anchor_key(platform: str, chat_id: str) -> str:
    """One chat's identity across platforms — never just ``chat_id``, which
    collides between a Telegram group and a Discord channel."""
    return f"{(platform or '').strip().lower()}:{str(chat_id).strip()}"


class AnchorStore:
    """Thread-safe ``{platform:chat_id → message_id}`` map backed by a JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path = path or (data_dir() / _FILENAME)
        self._anchors: dict[str, str] = {}
        self._loaded = False

    # ── persistence ──────────────────────────────────────────────────────────

    def load(self) -> dict[str, str]:
        """Read the file once; a malformed record is ignored, never fatal."""
        with self._lock:
            if not self._loaded:
                self._anchors = self._read_locked()
                self._loaded = True
            return dict(self._anchors)

    def _read_locked(self) -> dict[str, str]:
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except FileNotFoundError:
            return {}
        except Exception as exc:  # noqa: BLE001 - corrupt file must not break the plugin
            logger.debug("sticky-statusbar: unreadable anchor store %s: %s", self._path, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        anchors: dict[str, str] = {}
        for key, value in data.items():
            if isinstance(key, str) and isinstance(value, (str, int)):
                anchors[key] = str(value)
        return anchors

    def _write_locked(self) -> None:
        payload = json.dumps(self._anchors, indent=0, sort_keys=True)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self._path)
        except Exception as exc:  # noqa: BLE001 - a status message beats a perfect record
            logger.debug("sticky-statusbar: could not persist anchors: %s", exc)

    # ── read / write ─────────────────────────────────────────────────────────

    def get(self, platform: str, chat_id: str) -> str:
        with self._lock:
            if not self._loaded:
                self._anchors = self._read_locked()
                self._loaded = True
            return self._anchors.get(anchor_key(platform, chat_id), "")

    def set(self, platform: str, chat_id: str, message_id: str) -> None:
        key = anchor_key(platform, chat_id)
        with self._lock:
            if not self._loaded:
                self._anchors = self._read_locked()
                self._loaded = True
            if self._anchors.get(key) == str(message_id):
                return                      # unchanged: no write
            self._anchors[key] = str(message_id)
            if len(self._anchors) > _MAX_ENTRIES:
                for stale in sorted(self._anchors)[: len(self._anchors) - _MAX_ENTRIES]:
                    self._anchors.pop(stale, None)
            self._write_locked()

    def clear(self, platform: str, chat_id: str) -> None:
        key = anchor_key(platform, chat_id)
        with self._lock:
            if self._anchors.pop(key, None) is not None:
                self._write_locked()

    def clear_all(self) -> None:
        """Forget every anchor (tests, and a deliberate full reset).

        The messages themselves are left where they are: deleting them is the
        platform's business, and a status message that vanishes silently is a
        bigger surprise than a stale one.
        """
        with self._lock:
            if not self._loaded:
                self._anchors = self._read_locked()
                self._loaded = True
            self._anchors.clear()
            self._write_locked()

    def all(self) -> dict[str, str]:
        with self._lock:
            if not self._loaded:
                self._anchors = self._read_locked()
                self._loaded = True
            return dict(self._anchors)

    def __len__(self) -> int:
        return len(self.all())
