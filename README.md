# hermes-sticky-statusbar

A **self-updating status message that lives in your chat.** One message per chat,
rewritten on an interval with what the agent is doing right now — the tool
running at this moment, the tool calls that just finished, turn state and
elapsed time, tokens/sec, prompt-cache hit rate, context occupancy, the active
model, and this process's counters. The conversation piles up above it; the board
keeps itself current underneath.

It is the same telemetry as the [desktop status
bar](https://hermes-agent.nousresearch.com/docs/), in a message your team can
already see.

## What it looks like

Real output, rendered by this plugin's own `board.render_board()`:

**Working** — a tool is running, three calls are in the ledger:

```
📊 Hermes · running ⧉ terminal pytest -q
⧉ terminal (2s) pytest -q · ✓ read_file 0.4s config.py · ✓ web_search 1.1s oslo weather · ✗ write_file 0.2s main.py
⚡ 96 tok/s · ◎ 79% cache · ▰▱▱▱▱▱▱▱▱▱ 24.8K/200.0K ctx · ◷ 1.4s · ☤ deepseek-v4.1-flash
⏱ 42s · ↻ 3 turns · 12 calls · 5 tools
```

**Idle** — nothing running, so the board shrinks to what still matters:

```
📊 Hermes · idle · waiting for you
✓ read_file 0.4s config.py
⚡ 96 tok/s · ◎ 79% cache · ▰▱▱▱▱▱▱▱▱▱ 24.8K/200.0K ctx · ☤ deepseek-v4.1-flash
↻ 3 turns · 12 calls · 5 tools · ⌛ 4m ago
```

A line with nothing to say is **omitted**, never rendered as `--`: a status
message that repeats "no data" is noise in a chat that is meant to be read while
scrolling past it.

## How it moves (and the honest part)

**No chat platform has a native "sticky bottom message"** — that is a forum
concept, not a Discord/Telegram/Slack one. So the plugin builds it out of what
the platform does have, and you choose which trade you want:

| `mode` | What happens | Cost |
|---|---|---|
| `edit` (default) | one message, rewritten in place | quiet, never pings, **keeps its original position** — new messages appear below it |
| `bump` | in-place edits as usual, **plus** a delete-and-repost at the end of each turn | always the newest message in the channel; each repost costs two API calls and can ping the channel |

On Telegram the plugin also **pins the message silently** (`pin_telegram: true`),
so it stays visible above the composer even when the conversation scrolls past
it. That is the closest thing any of these platforms has to a sticky.

## Install

```bash
# The plugin lives in a subdirectory, so name it after the '#'
hermes plugins install kyssta-exe/hermes-sticky-statusbar#sticky-statusbar
hermes plugins enable sticky-statusbar
```

Once the catalog entry lands, the plain name is enough:

```bash
hermes plugins install sticky-statusbar
```

Or drop the directory in by hand:

```bash
git clone https://github.com/kyssta-exe/hermes-sticky-statusbar
cp -r hermes-sticky-statusbar/sticky-statusbar ~/.hermes/plugins/
hermes plugins enable sticky-statusbar
```

**Restart the gateway afterwards.** A `register_platform_handler` factory queued
after the platform already connected is not always invoked by the mid-run rewire,
so the sticky may not appear until the gateway restarts. Confirm it attached:

```bash
grep -iE "sticky-statusbar" ~/.hermes/logs/agent.log
```

The chat has to be **tracked** before anything is posted:

- it messages the agent (`auto_track: true`, the default), or
- it is listed in `chats` as `telegram:<chat_id>`, or
- you run `/sticky here` in it.

## Settings

Everything lives under `plugins.entries.sticky-statusbar.settings.*` — the
Desktop's Settings ▸ Plugins form, `hermes config set` and the plugin all read
the same keys.

| Key | Default | What it does |
|---|---|---|
| `enabled` | `true` | post and maintain the message at all |
| `mode` | `edit` | `edit` (rewrite in place) or `bump` (repost at turn end so it is always the newest message) |
| `header` | `📊 Hermes` | first line of the board |
| `interval_seconds` | `5` | seconds between updates (2–600); each platform enforces its own floor — Telegram 3s, Discord 5s |
| `pin_telegram` | `true` | pin the message silently so Telegram keeps it visible |
| `auto_track` | `true` | start tracking a chat the first time it messages the agent |
| `chats` | `""` | extra chats to track, one `platform:chat_id` per line |
| `show_tools` | `true` | show the running tool and the recent calls |
| `tool_history` | `3` | how many finished calls to list (0–10) |
| `show_metrics` | `true` | tokens/sec, cache hit, context, latency |
| `show_model` | `true` | the active model |
| `show_elapsed` | `true` | how long the current turn has been running |
| `show_totals` | `false` | lifetime tokens for this process |
| `idle_text` | `idle · waiting for you` | what the header says when nothing is running |
| `bar_width` | `10` | context-bar cells (3–20) |
| `separator` | ` · ` | text between segments |
| `max_chars` | `1800` | hard cap on the rendered message |

## Surfaces

| Surface | What it does |
|---|---|
| **The sticky message** | the board, one message per tracked chat, updated on the interval |
| **`/sticky`** | the board plus the resolved settings, in any gateway session |
| **`/sticky here`** | track this chat (for `auto_track: false`) |
| **`/sticky stop`** | stop updating this chat (the message stays put) |
| **`/sticky bump`** | repost at the bottom on the next tick |
| **`agent_status` tool** | the model can read its own board when you ask how it is doing |
| **`hermes sticky-statusbar`** | `status` / `board` / `json` / `config` / `chats` / `reset` from a shell |

## Platforms

| Platform | Transport | Verified |
|---|---|---|
| Telegram | PTB client — send / edit / delete / silent pin, plain text (`parse_mode=None`) | ✅ transport-level tests + end-to-end through the real Hermes machinery (factory, hooks, real ticks into a recording bot) |
| Discord | `discord.py` client — silent send / edit / delete (Discord's adapter exposes no delete, so the native client does it) | ✅ transport-level tests + the factory attaches and starts on a live loop |
| Slack, Matrix, Mattermost, Google Chat, Feishu, DingTalk, Buzz | the adapter's `send` / `edit_message` / `delete_message` | ⚠️ best effort — the transport is tested against a fake adapter, but not against a live workspace; the plugin tries, and reposts when a platform cannot edit |

Platforms with no edit API at all (Signal, iMessage, email, SMS, LINE, ntfy)
cannot host a sticky; the plugin does not pretend otherwise. WhatsApp is
deliberately not wired — it is not worth a message per interval there.

## Operational notes

- **Rate limits are the design constraint.** Telegram counts a send and an edit
  against the *same* per-chat allowance the gateway's own streaming previews
  draw from; Discord allows roughly five edits per five seconds per channel.
  That is why the interval is floored per platform, why an unchanged board costs
  **zero** API calls, and why a chat that keeps failing backs off exponentially
  (up to five minutes) instead of retrying every tick.
- **Settings changes land without a restart.** A running publisher re-reads its
  own settings (at most every 30 seconds) and adopts them in place, and the loop
  re-reads the interval every tick — so switching to `mode: bump`, changing
  `interval_seconds`, or switching the plugin off lands within half a minute.
  This exists because a platform factory runs once per native client: a hot
  plugin reload never re-runs it, and without the re-read a change would wait for
  a gateway restart.
- **The message is never lost.** The anchor id is stored in
  `$HERMES_HOME/plugin-data/sticky-statusbar/anchors.json`. If an edit fails —
  the message was deleted by hand, the record is stale after a restart, or the
  platform has no edit API — the publisher posts a fresh message and only then
  deletes the old one, so the chat never shows a gap.
- **Nothing is posted into a chat nobody asked for.** Tracking is per chat and
  can be stopped with `/sticky stop`.
- **Observer-only.** No prompt rewriting, no tool overrides, no core files
  touched, no monkey-patching. Every platform call goes through the client the
  adapter hands over at connect time.

## Disclosure

- **It redacts what a normal reply would have redacted.** An agent's own message
  passes through Hermes's redactor on its way to the platform; a message this
  plugin posts goes out through the platform client and past that pass, while the
  board echoes the first characters of a tool's command. So `redact.py` scrubs
  the rendered text first — vendor credential prefixes, credential-shaped
  assignments and headers, URL userinfo, and long opaque blobs. It is
  intentionally self-contained (Hermes's own redactor is a private core module)
  and deliberately biased toward masking: a masked word on a status line is
  cosmetic, a leaked token is not. A 40-character hex string in a command hint is
  masked for the same reason.
- **Network calls:** none of its own. Every request is one API call to the
  gateway platform you already run (Telegram's Bot API, Discord's API, …) through
  the client that platform's adapter provides. No telemetry, no third parties.
- **Local files:** one JSON file, `$HERMES_HOME/plugin-data/sticky-statusbar/anchors.json`,
  holding `platform:chat_id → message_id`. Nothing else is read or written.
- **Shell commands:** none.
- **Background process:** one asyncio task per connected platform, on that
  platform's existing event loop. It stops with the gateway.

## Development

```bash
pip install "pytest>=8,<9" "pyyaml>=6,<7"
python -m pytest tests/ -q          # 203 unit tests, no SDKs needed
ruff check sticky-statusbar/ tests/
hermes plugins validate sticky-statusbar
hermes plugins doctor sticky-statusbar
```

`tests/test_e2e.py` goes further: it installs the plugin into an isolated
`HERMES_HOME`, lets Hermes discover and load it exactly as the gateway does,
fires the real `pre_api_request` / `post_api_request` / `pre_tool_call` /
`post_tool_call` / `post_gateway_admission` / `on_session_end` payloads through
the live hook registry, calls the registered platform factory the way an adapter
does at connect time, and asserts on what a chat member would see. It skips
itself automatically when the Hermes source is not importable.

Both suites refuse to touch a live profile: `HERMES_HOME` is forced to a fresh
scratch directory before Hermes is imported.

## License

MIT
