---
name: sticky-statusbar
description: "Use when reading or tuning the chat status board (the sticky message)."
version: 1.0.0
metadata:
  hermes:
    tags: [status, gateway, telegram, discord, observability]
---

# Sticky statusbar

The sticky statusbar is a **single message per chat** that Hermes rewrites on an
interval: the tool running right now, the calls that just finished, turn state
and elapsed time, tokens/sec, prompt-cache hit rate, context occupancy, the
active model and this process's counters. The conversation accumulates above it.

## Reading the board

```
📊 Hermes · running ⧉ terminal pytest -q
⧉ terminal (2s) pytest -q · ✓ read_file 0.4s config.py · ✗ write_file 0.2s main.py
⚡ 96 tok/s · ◎ 79% cache · ▰▰▱▱▱▱▱▱▱▱ 24.0K/200.0K ctx · ☤ deepseek-v4.1-flash · ◷ 1.4s
⏱ 42s · ↻ 3 turns · 12 calls · 5 tools · ⌛ 2m ago
```

- `⚡` windowed throughput — `sum(output tokens) / sum(request seconds)`, not a
  mean of per-request ratios, so one slow request cannot tank it.
- `◎` cache hit — `cache_read / prompt`. `100%` means the whole prompt prefix was
  reused. Hidden when the window has no cached reads at all: no data is not the
  same thing as a bad cache.
- Context — `used/window` and the bar; lower is better. Above ~80% expect a
  compression.
- `⧉` the running call with its argument hint; `✓`/`✗` are finished calls, newest
  last. A `✗` line that repeats is the signal to look at the actual tool result.
- `⌛ ... ago` is the age of the last activity — that plus `⏱` (elapsed) is how you
  tell "working" from "stalled two minutes ago".

## Controlling it

| Command | What it does |
|---|---|
| `/sticky` | The board plus the resolved settings |
| `/sticky here` | Track this chat when auto-tracking is off |
| `/sticky stop` | Stop updating this chat (the message stays) |
| `/sticky bump` | Repost the message at the bottom on the next tick |
| `hermes sticky-statusbar status\|chats\|config\|json` | The same from a shell |

## Tuning it

- **It updates too often / the chat is rate limited.** Raise `interval_seconds`.
  Each platform has a floor (Telegram 3s, Discord 5s) and the publisher never
  goes under it. Telegram counts a send and an edit against the same per-chat
  allowance the agent's own streaming previews use.
- **It never appears.** The chat must be tracked: message the agent in it, list
  it in `chats` as `telegram:<chat_id>`, or run `/sticky here`. A platform with
  no wired publisher (not connected, or a transport that cannot send) logs why.
- **It is not the newest message.** Set `mode: bump`: the message is deleted and
  reposted at the end of each turn, so it lands below the answer. In `edit` mode
  it keeps its original position — quieter, because Discord allows only about
  five edits per five seconds per channel and each repost can ping the channel.
- **It is stale after a restart.** Anchors are stored in
  `$HERMES_HOME/plugin-data/sticky-statusbar/anchors.json`; if that record is
  wrong the next tick posts a fresh message and deletes the old one.
