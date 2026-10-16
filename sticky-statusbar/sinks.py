"""Per-platform transports for the sticky message.

A sink knows how to create, rewrite, delete and (where the platform has one)
pin one message in one chat. Three implementations cover every gateway:

* :class:`TelegramSink` — the PTB ``Bot`` handed over by
  ``register_platform_handler("telegram", ...)``. Sends with
  ``disable_notification`` (a status message must never ping anyone) and edits
  with ``parse_mode=None``, so a ``·`` or an ``_`` in the text cannot turn into
  a MarkdownV2 parse error. Telegram is the one platform where pinning helps:
  a pinned message stays visible above the composer when the chat scrolls.
* :class:`DiscordSink` — the ``discord.ext.commands.Bot`` from
  ``register_platform_handler("discord", ...)``. Sends silently and edits the
  channel's partial message. Discord's adapter exposes no ``delete_message``, so
  deletes go through the native client (needed for ``mode: bump``).
* :class:`AdapterSink` — the documented ``adapter.send`` / ``adapter.edit_message``
  / ``adapter.delete_message`` surface, for every other platform. Editing is
  optional in that interface, so the publisher falls back to delete-and-repost
  when an edit reports failure.

No sink ever raises out to the publisher: every call is wrapped, because a
status message must never take a gateway down.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class Sink:
    """Base transport. Unsupported operations report failure, never raise."""

    platform = "unknown"
    supports_pin = False

    def available(self) -> bool:
        """Whether this sink can talk to its platform at all."""
        return True

    def _chat(self, chat_id: str) -> Any:
        """Normalize a chat id for the platform's API."""
        return chat_id

    def _message(self, message_id: str) -> Any:
        """Normalize a message id for the platform's API."""
        text = str(message_id).strip()
        try:
            return int(text)
        except (TypeError, ValueError):
            return text

    async def send(self, chat_id: str, text: str, thread_id: str = "") -> str | None:
        """Post the message; returns its id, or None on failure."""
        raise NotImplementedError

    async def edit(self, chat_id: str, message_id: str, text: str) -> bool:
        """Rewrite the message in place. False = caller should repost."""
        return False

    async def delete(self, chat_id: str, message_id: str) -> bool:
        """Delete the message. False = it stays where it is."""
        return False

    async def pin(self, chat_id: str, message_id: str) -> bool:
        """Pin the message (silently). False = unsupported or refused."""
        return False

    def describe(self) -> str:
        return self.platform


# ── Telegram ─────────────────────────────────────────────────────────────────


class TelegramSink(Sink):
    """PTB ``Bot`` transport: send / edit / delete / pin, all notification-free."""

    platform = "telegram"
    supports_pin = True

    def __init__(self, native: Any, adapter: Any = None) -> None:
        self._bot = getattr(native, "bot", None)
        self._adapter = adapter

    def available(self) -> bool:
        return self._bot is not None

    def _chat(self, chat_id: str) -> Any:
        # Supergroup ids arrive as "-100…" strings; PTB takes them as ints.
        # A "@channel" handle is left alone.
        text = str(chat_id).strip()
        try:
            return int(text)
        except (TypeError, ValueError):
            return text

    async def send(self, chat_id: str, text: str, thread_id: str = "") -> str | None:
        if self._bot is None:
            return None
        kwargs: dict[str, Any] = {
            "chat_id": self._chat(chat_id),
            "text": text,
            # A status message that pings the whole channel on creation and
            # again on every repost is a status message nobody keeps.
            "disable_notification": True,
        }
        if thread_id:
            try:
                kwargs["message_thread_id"] = int(thread_id)
            except (TypeError, ValueError):
                pass
        try:
            message = await self._bot.send_message(**kwargs)
        except Exception as exc:  # noqa: BLE001 - a status message is never fatal
            logger.debug("[telegram] sticky send failed in %s: %s", chat_id, exc)
            return None
        message_id = getattr(message, "message_id", None)
        return str(message_id) if message_id is not None else None

    async def edit(self, chat_id: str, message_id: str, text: str) -> bool:
        if self._bot is None:
            return False
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat(chat_id),
                message_id=self._message(message_id),
                text=text,
            )
        except Exception as exc:  # noqa: BLE001
            if "not modified" in str(exc).lower():
                return True          # identical text: Telegram's own dedupe, not a failure
            logger.debug("[telegram] sticky edit failed in %s: %s", chat_id, exc)
            return False
        return True

    async def delete(self, chat_id: str, message_id: str) -> bool:
        if self._bot is None:
            return False
        try:
            await self._bot.delete_message(
                chat_id=self._chat(chat_id), message_id=self._message(message_id)
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[telegram] sticky delete failed in %s: %s", chat_id, exc)
            return False
        return True

    async def pin(self, chat_id: str, message_id: str) -> bool:
        if self._bot is None:
            return False
        try:
            await self._bot.pin_chat_message(
                chat_id=self._chat(chat_id),
                message_id=self._message(message_id),
                disable_notification=True,
            )
        except Exception as exc:  # noqa: BLE001 - "not enough rights" is normal in groups
            logger.debug("[telegram] sticky pin refused in %s: %s", chat_id, exc)
            return False
        return True


# ── Discord ──────────────────────────────────────────────────────────────────


class DiscordSink(Sink):
    """``discord.py`` transport. Pinning does not help on Discord (pins are a
    separate list, not a position), so the anchor is edited in place and
    ``mode: bump`` deletes and reposts it at turn end."""

    platform = "discord"
    supports_pin = False

    def __init__(self, native: Any, adapter: Any = None) -> None:
        self._bot = native
        self._adapter = adapter

    def available(self) -> bool:
        return self._bot is not None

    def _chat(self, chat_id: str) -> Any:
        text = str(chat_id).strip()
        try:
            return int(text)
        except (TypeError, ValueError):
            return text

    async def _channel(self, chat_id: str) -> Any:
        target = self._chat(chat_id)
        channel = None
        getter = getattr(self._bot, "get_channel", None)
        if callable(getter) and isinstance(target, int):
            channel = getter(target)
        if channel is None:
            fetcher = getattr(self._bot, "fetch_channel", None)
            if callable(fetcher):
                channel = await fetcher(target)
        return channel

    async def send(self, chat_id: str, text: str, thread_id: str = "") -> str | None:
        try:
            channel = await self._channel(chat_id)
            if channel is None:
                return None
            try:
                message = await channel.send(content=text, silent=True)
            except TypeError:
                # Older discord.py without the ``silent`` kwarg.
                message = await channel.send(content=text)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[discord] sticky send failed in %s: %s", chat_id, exc)
            return None
        message_id = getattr(message, "id", None)
        return str(message_id) if message_id is not None else None

    async def edit(self, chat_id: str, message_id: str, text: str) -> bool:
        try:
            channel = await self._channel(chat_id)
            if channel is None:
                return False
            partial = channel.get_partial_message(self._message(message_id))
            await partial.edit(content=text)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[discord] sticky edit failed in %s: %s", chat_id, exc)
            return False
        return True

    async def delete(self, chat_id: str, message_id: str) -> bool:
        """Discord's adapter exposes no delete; the native client does."""
        try:
            channel = await self._channel(chat_id)
            if channel is None:
                return False
            partial = channel.get_partial_message(self._message(message_id))
            await partial.delete()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[discord] sticky delete failed in %s: %s", chat_id, exc)
            return False
        return True


# ── everything else ──────────────────────────────────────────────────────────


class AdapterSink(Sink):
    """Transport for every platform whose native client we do not special-case.

    Uses only the documented adapter surface (``send`` / ``edit_message`` /
    ``delete_message``), so it works wherever those are implemented — Slack,
    Matrix, Mattermost, Google Chat, Feishu, DingTalk, Buzz — and degrades to
    "no edit support" where they are not, which makes the publisher repost
    instead of rewriting.
    """

    supports_pin = False

    def __init__(self, adapter: Any, platform: str = "unknown") -> None:
        self._adapter = adapter
        self.platform = platform or getattr(getattr(adapter, "platform", None), "value", "unknown")

    def available(self) -> bool:
        return self._adapter is not None

    def _chat(self, chat_id: str) -> Any:
        normalizer = getattr(self._adapter, "normalize_chat_id", None)
        if callable(normalizer):
            try:
                return normalizer(chat_id)
            except Exception:  # noqa: BLE001
                return chat_id
        return chat_id

    async def send(self, chat_id: str, text: str, thread_id: str = "") -> str | None:
        try:
            result = await self._adapter.send(self._chat(chat_id), text)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[%s] sticky send failed in %s: %s", self.platform, chat_id, exc)
            return None
        if not getattr(result, "success", False):
            logger.debug(
                "[%s] sticky send refused in %s: %s",
                self.platform, chat_id, getattr(result, "error", ""),
            )
            return None
        message_id = getattr(result, "message_id", None)
        return str(message_id) if message_id else None

    async def edit(self, chat_id: str, message_id: str, text: str) -> bool:
        try:
            result = await self._adapter.edit_message(self._chat(chat_id), str(message_id), text)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[%s] sticky edit failed in %s: %s", self.platform, chat_id, exc)
            return False
        return bool(getattr(result, "success", False))

    async def delete(self, chat_id: str, message_id: str) -> bool:
        try:
            return bool(
                await self._adapter.delete_message(self._chat(chat_id), str(message_id))
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[%s] sticky delete failed in %s: %s", self.platform, chat_id, exc)
            return False


def build_sink(platform: str, native: Any, adapter: Any) -> Sink:
    """Pick the transport for *platform* from the objects the adapter handed us."""
    name = (platform or "").lower()
    if name == "telegram" and getattr(native, "bot", None) is not None:
        return TelegramSink(native, adapter)
    if name == "discord" and native is not None:
        return DiscordSink(native, adapter)
    return AdapterSink(adapter, name)
