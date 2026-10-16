"""sticky-statusbar — a self-updating status message that lives in your chat.

One message per chat, rewritten on an interval with what the agent is doing
right now: the tool running at this moment, the tool calls that just finished,
turn state and elapsed time, tokens/sec, prompt-cache hit rate, context
occupancy, the active model and the process counters. The conversation piles up
above it while the board keeps itself current.

Surfaces this plugin adds:

* **The sticky message** — created and maintained on the gateway's own client
  (``register_platform_handler``), one per tracked chat. On Telegram it also
  pins itself silently, so it stays visible above the composer; with
  ``mode: bump`` it reposts at the end of a turn so it is always the newest
  message in the channel.
* **``/sticky`` slash command** — the board plus the resolved settings, and
  ``here`` / ``stop`` / ``bump`` for the chat the command came from.
* **``agent_status`` tool** — the model can read its own board on request.
* **``hermes sticky-statusbar`` CLI** — status, chats and configuration from a shell.

Everything is observer-only: the hooks record numbers and track which chat spoke,
nothing rewrites a prompt, blocks a tool, or overrides a built-in.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import board, publisher, schemas, status_tool
from .anchors import AnchorStore
from .settings import StickyConfig, describe
from .telemetry import DEFAULT_WINDOW, MetricsCollector

logger = logging.getLogger(__name__)

# Platforms whose native client we can address. Each factory fires only when
# that platform actually connects, so listing one that is not configured costs
# nothing; a platform that is not listed simply never gets a sticky.
PLATFORMS = (
    "telegram",
    "discord",
    "slack",
    "matrix",
    "mattermost",
    "dingtalk",
    "feishu",
    "google_chat",
    "buzz",
)

# ── process-wide singletons ──────────────────────────────────────────────────
# The hooks fire on the agent thread and the publisher on the platform's event
# loop, so the collector owns its own lock and these are plain module handles.
# One gateway process = one collector, shared by every platform publisher.

_collector: MetricsCollector | None = None
_config: StickyConfig | None = None
_store: AnchorStore | None = None
_fixed_chats: dict[str, tuple[str, ...]] = {}


def get_collector() -> MetricsCollector:
    """The collector every hook writes to and every reader reads from."""
    global _collector
    if _collector is None:
        _collector = MetricsCollector(window=DEFAULT_WINDOW)
    return _collector


def get_config() -> StickyConfig:
    """The resolved config (defaults until ``register()`` runs)."""
    global _config
    if _config is None:
        _config = StickyConfig()
    return _config


def get_store() -> AnchorStore:
    """The durable anchor record (loaded lazily, never fatal)."""
    global _store
    if _store is None:
        _store = AnchorStore()
    return _store


# ── hook callbacks ───────────────────────────────────────────────────────────
# Thin adapters so the collector's signatures stay independent of the payloads.


def _on_pre_api_request(**kwargs: Any) -> None:
    get_collector().on_pre_api_request(**kwargs)


def _on_post_api_request(**kwargs: Any) -> None:
    get_collector().on_post_api_request(**kwargs)


def _on_api_request_error(**kwargs: Any) -> None:
    get_collector().on_api_request_error(**kwargs)


def _on_pre_tool_call(**kwargs: Any) -> None:
    get_collector().on_pre_tool_call(**kwargs)


def _on_post_tool_call(**kwargs: Any) -> None:
    get_collector().on_post_tool_call(**kwargs)


def _on_post_gateway_admission(*, platform: str = "", source: Any = None, **kwargs: Any) -> None:
    """An inbound message was admitted: that chat is live, and a turn is starting.

    The hook is directive-shaped (a plugin may consume the message) but this one
    has no opinion about flow — it returns ``None``, so whatever was going to
    happen still happens.
    """
    get_collector().on_turn_start(platform=platform)
    _track_source(platform, source)


def _on_session_end(*, platform: str = "", **kwargs: Any) -> None:
    """The turn finished: stop claiming to work, and repost if that is the mode."""
    get_collector().on_turn_end(**kwargs)
    if not get_config().bump_mode:
        return
    pub = publisher.get(platform)
    if pub is not None:
        pub.request_bump()


def _track_source(platform: str, source: Any) -> None:
    """Start tracking the chat a message came from (``source`` is a dict snapshot)."""
    if not isinstance(source, dict):
        return
    name = str(source.get("platform") or platform or "").strip().lower()
    pub = publisher.get(name)
    if pub is None:
        return
    chat_id = source.get("chat_id")
    if chat_id is None:
        return
    pub.track(
        str(chat_id),
        thread_id=str(source.get("thread_id") or ""),
        chat_type=str(source.get("chat_type") or "dm"),
    )


# ── slash command ────────────────────────────────────────────────────────────


def _latest_chat() -> tuple[publisher.StickyPublisher | None, str]:
    """The publisher and chat id of the most recently active tracked chat."""
    best: tuple[publisher.StickyPublisher | None, str] = (None, "")
    newest = -1.0
    for pub in publisher.all_publishers():
        for chat in pub.tracked_chats():
            if chat.last_active >= newest:
                newest = chat.last_active
                best = (pub, chat.chat_id)
    return best


def _handle_sticky(raw_args: str) -> str:
    """/sticky [here|stop|bump] — the live board, and control of this chat."""
    args = (raw_args or "").strip().lower()
    collector = get_collector()
    config = get_config()

    if args in {"config", "settings"}:
        return describe(config)

    if args == "here":
        pub, chat_id = _latest_chat()
        if pub is None:
            return (
                "No chat seen yet on a wired platform. Send the agent a message in "
                "this chat, then try `/sticky here` again."
            )
        added = pub.track(chat_id, force=True)
        state = "now tracking" if added else "already tracking"
        return f"{state} `{pub.platform}:{chat_id}` — the board appears within one tick."

    if args in {"stop", "off"}:
        pub, chat_id = _latest_chat()
        if pub is None or not pub.untrack(chat_id):
            return "Not tracking a chat here, so there is nothing to stop."
        return (
            f"Stopped tracking `{pub.platform}:{chat_id}`. The message already in the "
            "chat is left where it is — delete it by hand if you want it gone."
        )

    if args in {"bump", "now", "refresh"}:
        pub, chat_id = _latest_chat()
        if pub is None:
            return "No chat tracked yet — nothing to repost."
        pub.request_bump(chat_id)
        return "Reposting the status message on the next tick."

    snapshot = collector.snapshot()
    return "\n\n".join([board.render_report(snapshot, config), describe(config)])


# ── CLI command ──────────────────────────────────────────────────────────────


def _cli_command(args: Any) -> None:
    """``hermes sticky-statusbar [status|board|config|chats|reset]``."""
    import json

    sub = str(getattr(args, "sticky_statusbar_command", "") or "status").strip().lower()
    config = get_config()
    collector = get_collector()

    if sub in {"config", "settings"}:
        print(describe(config))
        for key in getattr(config, "__dataclass_fields__", {}):
            print(f"  {key:<20} {getattr(config, key)!r}")
        return

    if sub == "reset":
        collector.reset()
        print("sticky-statusbar: counters reset")
        return

    if sub == "chats":
        rows = [chat for pub in publisher.all_publishers() for chat in pub.tracked_chats()]
        if not rows:
            print("No chats tracked. They are added when a chat messages the agent,")
            print("when listed in `chats`, or with `/sticky here`.")
            return
        for chat in rows:
            kind = chat.chat_type or "dm"
            print(f"  {chat.platform}:{chat.chat_id} ({kind}) thread={chat.thread_id or '-'}")
        print(f"  anchors recorded: {len(get_store())}")
        return

    snapshot = collector.snapshot()
    print(describe(config))
    print()
    print(board.render_board(snapshot, config))
    print()
    tracked = sum(len(p.tracked_chats()) for p in publisher.all_publishers())
    print(f"  publishers live        {publisher.active_publishers()}")
    print(f"  chats tracked          {tracked}")
    if sub == "json":
        print(json.dumps(snapshot.as_dict(), indent=2))


def _setup_argparse(subparser: Any) -> None:
    subs = subparser.add_subparsers(dest="sticky_statusbar_command")
    subs.add_parser("status", help="Show the board as it renders in a chat")
    subs.add_parser("board", help="Alias for status")
    subs.add_parser("json", help="The raw telemetry snapshot as JSON")
    subs.add_parser("config", help="Show the resolved settings")
    subs.add_parser("chats", help="List the chats the sticky lives in")
    subs.add_parser("reset", help="Zero the counters")


# ── registration ─────────────────────────────────────────────────────────────


def _wire_factory(platform: str, ctx: Any = None):
    """Build the ``register_platform_handler`` factory for one platform.

    The adapter calls it with its native client at connect time (and again on a
    late plugin load, on the platform's event loop), which is where the publisher
    gets its loop from. ``ctx`` rides along so the running publisher can re-read
    settings later without a restart.
    """

    def _wire(native: Any, adapter: Any) -> None:
        config = get_config()
        pub = publisher.wire(
            platform,
            native,
            adapter,
            get_collector(),
            config,
            store=get_store(),
            label=getattr(adapter, "name", "") or platform,
            ctx=ctx,
        )
        if not pub.available:
            logger.warning(
                "[%s] sticky-statusbar: no usable transport for this platform — "
                "the status message stays off here", platform,
            )
            return
        for chat_id in _fixed_chats.get(platform, ()):
            pub.track(chat_id, force=True)
        pub.start()

    return _wire


def register(ctx: Any) -> None:
    """Wire hooks, the platform handlers, the slash command, the tool and the CLI."""
    global _config, _fixed_chats

    config = StickyConfig.from_ctx(ctx)
    _config = config

    # `chats` names platforms that may not be wired yet (the publisher only
    # exists once the platform connects), so remember them until then.
    fixed: dict[str, list[str]] = {}
    for entry in config.chats:
        platform, _, chat_id = entry.partition(":")
        fixed.setdefault(platform.strip().lower(), []).append(chat_id.strip())
    _fixed_chats = {name: tuple(ids) for name, ids in fixed.items()}

    collector = get_collector()
    collector.set_tool_history(config.tool_history if config.show_tools else 0)

    if not config.enabled:
        logger.info("sticky-statusbar: disabled via settings — nothing is posted")
        # The read surfaces still answer, so /sticky and the CLI explain the
        # state instead of looking like a plugin that failed to load.
        ctx.register_command(
            "sticky", _handle_sticky, description="Show the live agent status board",
        )
        ctx.register_tool(
            name="agent_status",
            toolset="sticky_statusbar",
            schema=schemas.AGENT_STATUS,
            handler=status_tool.agent_status,
        )
        ctx.register_cli_command(
            name="sticky-statusbar",
            help="Show the live agent status board (sticky-statusbar is disabled)",
            setup_fn=_setup_argparse,
            handler_fn=_cli_command,
        )
        return

    # Observers. Every one of these returns nothing; none can steer the agent.
    ctx.register_hook("pre_api_request", _on_pre_api_request)
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("api_request_error", _on_api_request_error)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("post_gateway_admission", _on_post_gateway_admission)
    ctx.register_hook("on_session_end", _on_session_end)

    # The sticky itself: one factory per platform, each handed the live native
    # client when that platform connects.
    for platform in PLATFORMS:
        ctx.register_platform_handler(platform, _wire_factory(platform, ctx))

    ctx.register_command(
        "sticky", _handle_sticky, description="Show the live agent status board",
        args_hint="[here|stop|bump|config]",
    )
    ctx.register_tool(
        name="agent_status",
        toolset="sticky_statusbar",
        schema=schemas.AGENT_STATUS,
        handler=status_tool.agent_status,
    )
    ctx.register_cli_command(
        name="sticky-statusbar",
        help="Show the live agent status board, its chats and its settings",
        setup_fn=_setup_argparse,
        handler_fn=_cli_command,
    )

    # Bundled skill: how to read the board and tune it.
    skills_dir = Path(__file__).parent / "skills"
    for child in sorted(skills_dir.iterdir()) if skills_dir.is_dir() else []:
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.is_file():
            ctx.register_skill(child.name, skill_md)

    logger.info(
        "sticky-statusbar: armed (%s mode, every %ss, %d platform(s) wired, %d pinned chat(s))",
        config.mode, config.interval_seconds, len(PLATFORMS), len(config.chats),
    )
