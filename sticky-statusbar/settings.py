"""Settings resolution for sticky-statusbar.

Reads ``plugins.entries.sticky-statusbar.settings.*`` through ``ctx.get_config``
so the Desktop Settings → Plugins form, ``hermes config set`` and this module
agree on one source of truth. Every value is coerced defensively: a typo in
``config.yaml`` must never stop the status from updating.

The field names here are the contract: they must match the ``config_schema``
keys in ``plugin.yaml`` one-for-one (a mismatch is invisible drift — the form
writes one name while the publisher reads another). ``tests/test_plugin.py``
asserts the two sets are identical.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PLUGIN_ID = "sticky-statusbar"

MODES = ("edit", "bump")

# ``mode: bump`` + ``bump_scope``: what makes the message move.
#   turn_end — repost when a turn finishes, so it lands below the answer
#   always   — also repost while a turn runs, whenever something new landed below
#              the board (throttled by bump_min_gap_seconds, because each repost
#              is a delete plus a send against the channel's rate limit)
BUMP_SCOPES = ("turn_end", "always")

# Per-platform floor for ``interval_seconds``. The chat is the scarce resource:
# Telegram counts a send and an edit against the SAME per-chat allowance (and
# the gateway's own streaming preview edits draw from it), Discord allows about
# five message edits per five seconds per channel. Going under these floors is
# how a status plugin gets the agent's own replies throttled or dropped.
PLATFORM_MIN_INTERVAL = {
    "telegram": 3,
    "discord": 5,
    "slack": 3,
    "matrix": 3,
    "mattermost": 3,
}
DEFAULT_MIN_INTERVAL = 5

_DEFAULT_MODE = "edit"
_DEFAULT_HEADER = "📊 Hermes"
_DEFAULT_INTERVAL = 5
_DEFAULT_IDLE = "idle · waiting for you"
_DEFAULT_SEPARATOR = " · "


def _bool(value: Any, default: bool) -> bool:
    """Truthy coercion that also accepts the YAML spellings of false."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
    return default


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, min(high, number))


def _choice(value: Any, allowed: tuple[str, ...], default: str) -> str:
    if isinstance(value, str) and value.strip().lower() in allowed:
        return value.strip().lower()
    return default


def parse_chats(raw: Any) -> tuple[str, ...]:
    """``"telegram:1\\ndiscord:2"`` → ``("telegram:1", "discord:2")``.

    Accepts commas, newlines and semicolons; ignores anything without a
    ``platform:chat_id`` shape rather than tracking a chat nobody named.
    """
    if not isinstance(raw, str):
        return ()
    out: list[str] = []
    for chunk in raw.replace(",", "\n").replace(";", "\n").splitlines():
        item = chunk.strip()
        if not item or ":" not in item:
            continue
        platform, _, chat_id = item.partition(":")
        platform, chat_id = platform.strip().lower(), chat_id.strip()
        if not platform or not chat_id:
            continue
        key = f"{platform}:{chat_id}"
        if key not in out:
            out.append(key)
    return tuple(out)


@dataclass(frozen=True)
class StickyConfig:
    """Everything the publisher needs, already validated."""

    enabled: bool = True
    mode: str = _DEFAULT_MODE                 # edit | bump
    bump_scope: str = "turn_end"              # turn_end | always
    bump_min_gap_seconds: int = 30
    header: str = _DEFAULT_HEADER
    interval_seconds: int = _DEFAULT_INTERVAL
    pin_telegram: bool = True
    auto_track: bool = True
    chats: tuple[str, ...] = ()

    show_tools: bool = True
    tool_history: int = 3
    show_metrics: bool = True
    show_model: bool = True
    show_elapsed: bool = True
    show_totals: bool = False

    idle_text: str = _DEFAULT_IDLE
    bar_width: int = 10
    separator: str = _DEFAULT_SEPARATOR
    max_chars: int = 1800

    @property
    def bump_mode(self) -> bool:
        """``mode: bump`` — repost at turn end instead of editing in place."""
        return self.mode == "bump"

    @property
    def keep_at_bottom(self) -> bool:
        """Repost while a turn runs too, not only when it ends."""
        return self.mode == "bump" and self.bump_scope == "always"

    def interval_for(self, platform: str) -> int:
        """The configured interval, floored at the platform's safe minimum."""
        floor = PLATFORM_MIN_INTERVAL.get((platform or "").lower(), DEFAULT_MIN_INTERVAL)
        return max(floor, self.interval_seconds)

    @classmethod
    def from_ctx(cls, ctx: Any) -> StickyConfig:
        """Build from a live ``PluginContext`` (defaults for anything unset)."""
        get = getattr(ctx, "get_config", None)
        if get is None:
            return cls()

        def cfg(key: str, default: Any) -> Any:
            try:
                value = get(key, default)
            except Exception:
                return default
            return default if value is None else value

        header = str(cfg("header", _DEFAULT_HEADER) or _DEFAULT_HEADER).strip()
        idle = str(cfg("idle_text", _DEFAULT_IDLE) or _DEFAULT_IDLE).strip()
        separator = str(cfg("separator", _DEFAULT_SEPARATOR) or _DEFAULT_SEPARATOR)

        return cls(
            enabled=_bool(cfg("enabled", True), True),
            mode=_choice(cfg("mode", _DEFAULT_MODE), MODES, _DEFAULT_MODE),
            bump_scope=_choice(cfg("bump_scope", "turn_end"), BUMP_SCOPES, "turn_end"),
            bump_min_gap_seconds=_int(
                cfg("bump_min_gap_seconds", 30), 30, 5, 3600
            ),
            header=header[:80] or _DEFAULT_HEADER,
            interval_seconds=_int(
                cfg("interval_seconds", _DEFAULT_INTERVAL), _DEFAULT_INTERVAL, 2, 600
            ),
            pin_telegram=_bool(cfg("pin_telegram", True), True),
            auto_track=_bool(cfg("auto_track", True), True),
            chats=parse_chats(cfg("chats", "")),
            show_tools=_bool(cfg("show_tools", True), True),
            tool_history=_int(cfg("tool_history", 3), 3, 0, 10),
            show_metrics=_bool(cfg("show_metrics", True), True),
            show_model=_bool(cfg("show_model", True), True),
            show_elapsed=_bool(cfg("show_elapsed", True), True),
            show_totals=_bool(cfg("show_totals", False), False),
            idle_text=idle[:120] or _DEFAULT_IDLE,
            bar_width=_int(cfg("bar_width", 10), 10, 3, 20),
            separator=separator[:8] or _DEFAULT_SEPARATOR,
            max_chars=_int(cfg("max_chars", 1800), 1800, 200, 1900),
        )


def describe(config: StickyConfig | None = None) -> str:
    """One-line human summary, used by ``/sticky`` and the CLI command."""
    cfg = config or StickyConfig()
    pin = " · pinned" if cfg.pin_telegram else ""
    chats = f" · {len(cfg.chats)} fixed chat(s)" if cfg.chats else ""
    scope = f" ({cfg.bump_scope})" if cfg.bump_mode else ""
    return (
        f"sticky-statusbar: {'on' if cfg.enabled else 'off'} · {cfg.mode}{scope} · "
        f"every {cfg.interval_seconds}s{pin}{chats}"
    )
