"""Tool schemas for sticky-statusbar."""

from __future__ import annotations

# The model can read its own board on request ("how fast are you going?"),
# which is also how a user can check the sticky from a surface the sticky does
# not exist on (CLI, an unlisted chat).
AGENT_STATUS = {
    "name": "agent_status",
    "description": (
        "Report the agent's live status board: turn state, the tool running now, "
        "recent tool calls, tokens/sec, prompt-cache hit rate, context occupancy, "
        "the active model and the counters for this process."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}
