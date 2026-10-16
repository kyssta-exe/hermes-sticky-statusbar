"""Tool handler — the code that runs when the LLM calls ``agent_status``.

Contract from the plugin guide, applied literally:

1. ``def handler(args: dict, **kwargs) -> str``
2. Always returns a JSON string — success and error alike.
3. Never raises.
4. Accepts ``**kwargs`` so future context fields flow through untouched.
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import board, publisher
from .settings import StickyConfig
from .telemetry import MetricsCollector


def _collector() -> MetricsCollector:
    from . import get_collector

    return get_collector()


def _config() -> StickyConfig:
    from . import get_config

    return get_config()


def agent_status(args: dict | None = None, **kwargs: Any) -> str:
    """Return the live status board as JSON (``{"detail": "full"}`` for more)."""
    detail = str((args or {}).get("detail") or "summary").strip().lower()
    if detail not in {"summary", "full"}:
        detail = "summary"
    try:
        snapshot = _collector().snapshot()
        config = _config()
        payload: dict[str, Any] = {
            "ok": True,
            "detail": detail,
            "board": board.render_board(snapshot, config),
            "state": snapshot.state,
            "model": snapshot.model,
            "provider": snapshot.provider,
            "platform": snapshot.platform,
            "current_tool": snapshot.current_tool or None,
            "recent_tools": [
                {
                    "name": run.name,
                    "ok": run.ok,
                    "seconds": round(run.duration_s, 3),
                    "hint": run.hint,
                }
                for run in snapshot.tools
            ],
            "tokens_per_second": round(snapshot.tps, 2),
            "tokens_per_second_last_request": round(snapshot.instant_tps, 2),
            "cache_hit_pct": (
                None if snapshot.cache_hit_pct is None else round(snapshot.cache_hit_pct, 2)
            ),
            "context_used_tokens": snapshot.context_used,
            "context_length_tokens": snapshot.context_length,
            "context_pct": (
                None if snapshot.context_pct is None else round(snapshot.context_pct, 2)
            ),
            "turn_elapsed_seconds": round(snapshot.elapsed_seconds(), 1),
            "chats_tracked": [
                {
                    "platform": chat.platform,
                    "chat_id": chat.chat_id,
                    "chat_type": chat.chat_type,
                    "thread_id": chat.thread_id or None,
                }
                for pub in publisher.all_publishers()
                for chat in pub.tracked_chats()
            ],
        }
        if detail == "full":
            payload.update(
                {
                    "api_calls": snapshot.api_calls,
                    "errors": snapshot.errors,
                    "turns": snapshot.turns,
                    "failed_turns": snapshot.failed_turns,
                    "tool_calls": snapshot.tool_calls,
                    "tool_errors": snapshot.tool_errors,
                    "compressions_estimate": snapshot.compressions,
                    "total_tokens": snapshot.total_tokens,
                    "window_samples": snapshot.requests,
                    "idle_seconds": round(snapshot.idle_seconds(), 1),
                    "session_id": snapshot.session_id,
                    "publishers_live": publisher.active_publishers(),
                    "board_config": repr(_config()),
                    "report": board.render_report(snapshot, _config()),
                    "generated_at": time.time(),
                }
            )
        return json.dumps(payload)
    except Exception as exc:  # noqa: BLE001 - never break the agent loop
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
