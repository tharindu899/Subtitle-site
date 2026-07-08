from __future__ import annotations

import asyncio
import logging
from importlib.metadata import PackageNotFoundError, version as package_version
from io import BytesIO
from typing import Any, Awaitable, Callable

import httpx

# PyroFork deliberately exports the compatible ``pyrogram`` import namespace.
# Existing handlers and type imports stay stable while the installed library is
# PyroFork, not the legacy package.
try:
    import tgcrypto
except ImportError as error:  # pragma: no cover - Docker prevents this path
    raise RuntimeError("TgCrypto is required. Rebuild the project so requirements.txt is installed.") from error

try:
    PYROFORK_VERSION = package_version("pyrofork")
except PackageNotFoundError as error:  # pragma: no cover - Docker prevents this path
    raise RuntimeError("PyroFork is required. Rebuild the project so requirements.txt is installed.") from error

TGCRYPTO_VERSION = getattr(tgcrypto, "__version__", "installed")

from pyrogram import Client, filters
from pyrogram.enums import ChatType, ParseMode
from pyrogram.errors import MessageNotModified, PeerIdInvalid
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import BotCommand, CallbackQuery, InlineKeyboardMarkup, InputMediaPhoto, Message

from .config import settings
from .database import get_db, utcnow

logger = logging.getLogger("tharinduhub.subtitles.telegram")

ChannelCallback = Callable[[Message], Awaitable[None]]
PrivateDocumentCallback = Callable[[Message], Awaitable[None]]
PrivateCommandCallback = Callable[[Message], Awaitable[None]]
PrivateTextCallback = Callable[[Message], Awaitable[None]]
CallbackHandler = Callable[[CallbackQuery], Awaitable[None]]


class TelegramStorageError(RuntimeError):
    """Raised when Telegram cannot store or fetch a subtitle document."""


class TelegramService:
    def __init__(self) -> None:
        self.client: Client | None = None
        self.online = False
        self.bot_username = ""
        self._channel_ref: int | str = settings.channel_ref
        self._channel_ready = False
        self._on_channel: ChannelCallback | None = None
        self._on_private_document: PrivateDocumentCallback | None = None
        self._on_private_command: PrivateCommandCallback | None = None
        self._on_private_text: PrivateTextCallback | None = None
        self._on_callback: CallbackHandler | None = None

    @staticmethod
    def _channel_matches(chat: object | None) -> bool:
        chat_id = getattr(chat, "id", None)
        username = str(getattr(chat, "username", "") or "").lstrip("@").lower()
        expected = str(settings.auth_channel or "").strip()
        return bool(
            chat_id is not None
            and (
                str(chat_id) == expected
                or (expected.startswith("@") and username == expected.lstrip("@").lower())
            )
        )

    def _remember_channel(self, message: Message) -> None:
        if self._channel_matches(getattr(message, "chat", None)):
            self._channel_ref = int(message.chat.id)
            self._channel_ready = True

    async def _persist_channel_peer(self) -> bool:
        """Persist the private channel access hash in MongoDB.

        Telegram bot accounts cannot list dialogs after a restart, so a numeric
        private channel ID alone is not enough. Once the bot sees a channel post
        (or an owner forwards one to the bot), PyroFork has the access hash in its
        peer cache. Persisting that peer lets MTProto publishing work again
        after a Space rebuild without calling the HTTP Bot API.
        """
        if not self._channel_ready or not isinstance(self._channel_ref, int):
            return False
        try:
            peer = await self.require().resolve_peer(int(self._channel_ref))
            access_hash = getattr(peer, "access_hash", None)
            if access_hash is None:
                return False
            await get_db().settings.update_one(
                {"_id": "telegram_channel_peer"},
                {
                    "$set": {
                        "channel_id": str(int(self._channel_ref)),
                        "access_hash": str(int(access_hash)),
                        "peer_type": "channel",
                        "updated_at": utcnow(),
                    }
                },
                upsert=True,
            )
            logger.info("Saved subtitle channel peer for MTProto delivery: %s", self._channel_ref)
            return True
        except Exception as error:
            logger.debug("Could not persist subtitle channel peer: %s", error)
            return False

    async def _restore_channel_peer(self) -> bool:
        """Restore a previously learned private channel peer from MongoDB."""
        if self._channel_ready:
            return True
        try:
            saved = await get_db().settings.find_one({"_id": "telegram_channel_peer"})
            if not saved or str(saved.get("channel_id") or "") != str(settings.auth_channel):
                return False
            peer_id = int(saved["channel_id"])
            access_hash = int(saved["access_hash"])
            await self.require().storage.update_peers([(peer_id, access_hash, "channel", None, None)])
            await self.require().resolve_peer(peer_id)
            self._channel_ref = peer_id
            self._channel_ready = True
            logger.info("Restored subtitle channel peer from MongoDB: %s", peer_id)
            return True
        except Exception as error:
            logger.debug("Could not restore subtitle channel peer: %s", error)
            return False

    async def link_channel_from_forward(self, message: Message) -> tuple[bool, str]:
        """Link a private storage channel from a post forwarded by the owner.

        Forwarding an existing post gives PyroFork the channel access hash in
        its update payload. This is the safe bootstrap path when a bot cannot
        resolve a private channel from an ID after a fresh restart.
        """
        origin = getattr(message, "forward_from_chat", None)
        if not origin:
            return False, "Forward a post from the subtitle storage channel; do not copy or re-send it."
        if not self._channel_matches(origin):
            return False, "That forwarded post is not from AUTH_CHANNEL. Forward a post from the configured subtitle storage channel."
        self._channel_ref = int(origin.id)
        self._channel_ready = True
        try:
            await self.require().resolve_peer(int(origin.id))
        except Exception as error:
            self._channel_ready = False
            return False, f"Telegram could not learn that channel peer yet: {error}"
        if not await self._persist_channel_peer():
            return False, "The channel was seen, but its peer could not be saved. Send one new test message in the channel and try again."
        return True, "Storage channel linked. Future subtitle publishing will use MTProto directly."

    async def _learn_channel_from_message(self, message: Message) -> None:
        self._remember_channel(message)
        if self._channel_ready:
            await self._persist_channel_peer()

    async def _warm_channel_peer(self, force: bool = False) -> bool:
        """Resolve a channel only when PyroFork already knows that peer.

        Telegram bots are not allowed to call ``messages.GetDialogs``. For a
        private numeric channel, PyroFork learns the peer from a channel post
        or the owner-only forwarded-post linking flow, then restores it from
        MongoDB after later restarts.
        """
        if self._channel_ready and not force:
            return True
        if await self._restore_channel_peer():
            return True
        client = self.require()
        # A public @channel_username can always be resolved. A private numeric
        # channel resolves here only after an update from that channel has been
        # received and saved in PyroFork's local peer cache.
        try:
            chat = await client.get_chat(self._channel_ref)
            if self._channel_matches(chat):
                self._channel_ref = int(chat.id)
                self._channel_ready = True
                logger.info("Subtitle channel peer verified for MTProto delivery: %s", self._channel_ref)
                return True
        except Exception as error:
            logger.debug("MTProto channel peer is not cached yet: %s", error)
        return False

    async def _channel_target(self) -> int | str:
        await self._warm_channel_peer()
        return self._channel_ref

    async def start(
        self,
        on_channel: ChannelCallback,
        on_private_document: PrivateDocumentCallback,
        on_private_command: PrivateCommandCallback,
        on_private_text: PrivateTextCallback,
        on_callback: CallbackHandler,
    ) -> None:
        self._on_channel = on_channel
        self._on_private_document = on_private_document
        self._on_private_command = on_private_command
        self._on_private_text = on_private_text
        self._on_callback = on_callback
        if not (settings.api_id and settings.api_hash and settings.bot_token):
            return

        logger.info(
            "PyroFork MTProto enabled (v%s) with TgCrypto acceleration (%s)",
            PYROFORK_VERSION,
            TGCRYPTO_VERSION,
        )

        # Keep a normal session database rather than an in-memory session. It
        # preserves the channel peer/access-hash during process lifetime and
        # avoids fresh PEER_ID_INVALID errors after a bot restart.
        self.client = Client(
            "tharinduhub_subtitles_bot",
            api_id=settings.api_id,
            api_hash=settings.api_hash,
            bot_token=settings.bot_token,
            no_updates=False,
            workdir="/tmp",
            # PyroFork defaults to 1 concurrent upload/download transmission.
            # Subtitle files are small but members can submit several in a
            # row; raising this lets those transfers overlap instead of
            # queueing strictly one-at-a-time, which is the main source of a
            # "slow" feeling bot under normal (non-abusive) use.
            max_concurrent_transmissions=4,
        )
        commands = ["start", "help", "menu", "submit", "myfiles", "library", "members", "reports", "ads", "connectchannel", "cancel"]
        # Remember every message from the configured channel. This binds a
        # private channel to the current PyroFork peer cache even when the post
        # is not a subtitle document.
        self.client.add_handler(MessageHandler(self._remember_channel_message, filters.channel), group=-1)
        self.client.add_handler(MessageHandler(self._handle_channel, filters.channel & filters.document))
        self.client.add_handler(MessageHandler(self._handle_private_document, filters.private & filters.document))
        self.client.add_handler(MessageHandler(self._handle_private_command, filters.private & filters.command(commands)))
        self.client.add_handler(MessageHandler(self._handle_private_text, filters.private & filters.text & ~filters.command(commands)))
        # CallbackQuery is not a Message and has no `.chat` attribute.
        self.client.add_handler(CallbackQueryHandler(self._handle_callback))

        await self.client.start()
        # The client is usable immediately after start(); set this before
        # restoring the MongoDB peer because _restore_channel_peer calls
        # require() to update PyroFork's local peer store.
        self.online = True
        await self._restore_channel_peer()
        me = await self.client.get_me()
        self.bot_username = me.username or ""
        if isinstance(self._channel_ref, str) and self._channel_ref.startswith("@"):
            await self._warm_channel_peer()
        else:
            logger.info("Private numeric AUTH_CHANNEL must be linked once with /connectchannel before publishing.")
        try:
            await self.client.set_bot_commands(
                [
                    BotCommand("start", "Open subtitle workspace"),
                    BotCommand("menu", "Open the current menu"),
                    BotCommand("submit", "Send a subtitle file"),
                    BotCommand("myfiles", "Manage your uploaded files"),
                    BotCommand("library", "Browse team library"),
                    BotCommand("members", "Manage member access (owners)"),
                    BotCommand("reports", "Review viewer reports"),
                    BotCommand("ads", "Manage website ads (owners)"),
                    BotCommand("connectchannel", "Link private subtitle channel (owners)"),
                    BotCommand("cancel", "Cancel current action"),
                ]
            )
        except Exception:
            pass

    async def stop(self) -> None:
        self.online = False
        if self.client:
            await self.client.stop()
        self.client = None
        self._channel_ready = False
        self._channel_ref = settings.channel_ref

    async def _remember_channel_message(self, _client: Client, message: Message) -> None:
        await self._learn_channel_from_message(message)

    async def _handle_channel(self, _client: Client, message: Message) -> None:
        await self._learn_channel_from_message(message)
        if self._channel_matches(getattr(message, "chat", None)) and self._on_channel:
            await self._on_channel(message)

    async def _handle_private_document(self, _client: Client, message: Message) -> None:
        if self._on_private_document:
            await self._on_private_document(message)

    async def _handle_private_command(self, _client: Client, message: Message) -> None:
        if self._on_private_command:
            await self._on_private_command(message)

    async def _handle_private_text(self, _client: Client, message: Message) -> None:
        if self._on_private_text:
            await self._on_private_text(message)

    async def _handle_callback(self, _client: Client, callback: CallbackQuery) -> None:
        message = callback.message
        chat = getattr(message, "chat", None)
        if not chat or chat.type != ChatType.PRIVATE:
            try:
                await callback.answer("Open this menu in a private chat with the bot.", show_alert=True)
            except Exception:
                pass
            return
        if self._on_callback:
            await self._on_callback(callback)

    def require(self) -> Client:
        if not self.client or not self.online:
            raise TelegramStorageError("Telegram is offline. Check API_ID, API_HASH and BOT_TOKEN.")
        return self.client

    async def send_text(self, chat_id: int, text: str, keyboard: InlineKeyboardMarkup | None = None) -> Message:
        return await self.require().send_message(
            chat_id,
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    async def _remote_poster(self, poster_url: str) -> BytesIO | None:
        if not poster_url:
            return None
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(9.0, connect=4.0), follow_redirects=True) as client:
                response = await client.get(poster_url)
                response.raise_for_status()
            if not response.headers.get("content-type", "").startswith("image/"):
                return None
            image = BytesIO(response.content)
            image.name = "poster.jpg"
            return image
        except (httpx.HTTPError, OSError):
            return None

    async def replace_card(
        self,
        chat_id: int,
        text: str,
        keyboard: InlineKeyboardMarkup | None = None,
        poster_url: str = "",
        previous_message_id: int | None = None,
    ) -> Message:
        """Update one persistent menu card in place instead of delete + resend.

        Telegram lets a bot edit its own messages (text, caption, photo and
        keyboard) with no practical time limit, so every menu screen reuses
        the same message box. A delete-and-send-new only happens as a last
        resort, when Telegram genuinely cannot apply an in-place edit (the
        card type must flip between text-only and photo, or the stored
        message was deleted/too old for Telegram to touch).
        """
        client = self.require()
        poster = await self._remote_poster(poster_url) if poster_url else None

        if previous_message_id:
            try:
                if poster:
                    return await client.edit_message_media(
                        chat_id,
                        previous_message_id,
                        InputMediaPhoto(poster, caption=text, parse_mode=ParseMode.HTML),
                        reply_markup=keyboard,
                    )
                return await client.edit_message_text(
                    chat_id,
                    previous_message_id,
                    text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception as primary_error:
                # The stored message may be the "other shape" (e.g. a plain
                # text card when a poster card was requested). Editing just
                # the caption keeps the same message alive either way, even
                # though the photo itself cannot change without media edit.
                try:
                    return await client.edit_message_caption(
                        chat_id,
                        previous_message_id,
                        caption=text,
                        reply_markup=keyboard,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    logger.debug("Could not edit menu card %s in place: %s", previous_message_id, primary_error)

        # Fallback: no previous message, or it could not be edited at all
        # (deleted by the user, wrong chat, or otherwise unreachable).
        if previous_message_id:
            try:
                await client.delete_messages(chat_id, previous_message_id)
            except Exception:
                pass
        if poster:
            try:
                return await client.send_photo(chat_id, poster, caption=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            except Exception:
                pass
        return await client.send_message(chat_id, text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    async def answer(self, callback: CallbackQuery, text: str = "", alert: bool = False) -> None:
        try:
            await callback.answer(text=text[:190], show_alert=alert)
        except Exception:
            pass

    def _channel_peer_help(self) -> str:
        return (
            "The storage channel is not linked to this bot session yet. An owner must run /connectchannel, "
            "then forward any existing post from AUTH_CHANNEL to this bot. The bot will save the private channel peer automatically."
        )

    async def _copy_once(
        self,
        target: int | str,
        source_chat_id: int,
        source_message_id: int,
        file_id: str,
        caption: str,
        keyboard: InlineKeyboardMarkup | None = None,
    ) -> Message:
        client = self.require()
        try:
            return await client.copy_message(
                target,
                source_chat_id,
                source_message_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except Exception as first_error:
            try:
                return await client.send_document(
                    target,
                    file_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            except Exception as second_error:
                if isinstance(first_error, PeerIdInvalid) or isinstance(second_error, PeerIdInvalid) or "PEER_ID_INVALID" in str(second_error).upper():
                    raise TelegramStorageError(self._channel_peer_help()) from second_error
                raise TelegramStorageError(f"Telegram could not store this file: {second_error}") from first_error

    async def copy_document_to_channel(
        self,
        source_chat_id: int,
        source_message_id: int,
        file_id: str,
        caption: str,
        keyboard: InlineKeyboardMarkup | None = None,
    ) -> Any:
        """Copy a private member upload into the storage channel via MTProto.

        The channel peer is learned once through the owner-only /connectchannel
        flow and saved in MongoDB. This avoids HTTP Bot API connection failures
        from hosted environments.
        """
        if not await self._warm_channel_peer():
            raise TelegramStorageError(self._channel_peer_help())
        return await self._copy_once(await self._channel_target(), source_chat_id, source_message_id, file_id, caption, keyboard)

    async def edit_channel_caption(
        self,
        message_id: int,
        caption: str,
        keyboard: InlineKeyboardMarkup | None = None,
    ) -> None:
        if not await self._warm_channel_peer():
            raise TelegramStorageError(self._channel_peer_help())
        try:
            await self.require().edit_message_caption(
                await self._channel_target(),
                int(message_id),
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except MessageNotModified:
            # Telegram already has this exact caption + keyboard. That is the
            # desired end state, not a failure, so treat it as a silent success
            # instead of surfacing a "channel update failed" error to the user.
            logger.debug("Channel caption %s already matched the requested content; nothing to change.", message_id)
        except Exception as error:
            raise TelegramStorageError(f"Telegram could not update the channel caption: {error}") from error

    async def delete_channel_message(self, message_id: int) -> None:
        """Permanently remove one subtitle post from the configured storage channel."""
        if not await self._warm_channel_peer():
            raise TelegramStorageError(self._channel_peer_help())
        try:
            await self.require().delete_messages(await self._channel_target(), int(message_id))
        except Exception as error:
            raise TelegramStorageError(f"Telegram could not delete the stored subtitle: {error}") from error

    @staticmethod
    def _payload(value: object) -> BytesIO | None:
        if isinstance(value, BytesIO):
            value.seek(0)
            return value
        return None

    async def _download_with_retry(self, source: object, label: str) -> BytesIO:
        client = self.require()
        errors: list[str] = []
        for attempt in range(2):
            try:
                payload = self._payload(await client.download_media(source, in_memory=True))
                if payload is not None:
                    return payload
                errors.append("Telegram returned an empty file payload")
            except Exception as error:
                errors.append(str(error))
            if attempt == 0:
                await asyncio.sleep(0.7)
        raise TelegramStorageError(f"Could not download subtitle by {label}: {errors[-1] if errors else 'unknown error'}")

    async def download_document(self, *, file_id: str = "", message_id: int | None = None) -> BytesIO:
        errors: list[str] = []
        if file_id:
            try:
                return await self._download_with_retry(file_id, "file ID")
            except TelegramStorageError as error:
                errors.append(str(error))
        if message_id:
            try:
                target = await self._channel_target()
                message = await self.require().get_messages(target, int(message_id))
                if not message or not message.document:
                    raise TelegramStorageError("The saved channel message no longer contains a document.")
                return await self._download_with_retry(message, "channel message")
            except Exception as error:
                errors.append(str(error))
        raise TelegramStorageError(" | ".join(errors) or "The stored file reference is missing.")
