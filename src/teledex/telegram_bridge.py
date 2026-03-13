from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import NetworkError, TelegramError
from telegram.request import HTTPXRequest

from .codex_session import CodexSession
from .config import Settings
from .interactions import InteractionPrompt
from .media import MediaPipeline
from .state import BridgeState, StateStore, clear_pairing, issue_pair_code, pair_chat
from .util import chunk_text, normalize_telegram_slash_command


logger = logging.getLogger(__name__)
TELEDEX_COMMANDS = [
    BotCommand("help", "Show teledex help"),
    BotCommand("bridge_status", "Show teledex daemon and session status"),
    BotCommand("start_session", "Ensure the Codex session is ready"),
    BotCommand("interrupt", "Interrupt the current Codex request"),
    BotCommand("reset", "Reset the current Codex thread"),
]

CODEX_COMMANDS = [
    BotCommand("permissions", "Set what Codex can do without asking first"),
    BotCommand("sandbox_add_read_dir", "Grant extra sandbox read access"),
    BotCommand("agent", "Switch the active agent thread"),
    BotCommand("apps", "Browse apps and insert them into your prompt"),
    BotCommand("clear", "Clear the terminal and start a fresh chat"),
    BotCommand("compact", "Summarize the visible conversation"),
    BotCommand("copy", "Copy the latest completed Codex output"),
    BotCommand("diff", "Show the current Git diff"),
    BotCommand("exit", "Exit the Codex CLI"),
    BotCommand("experimental", "Toggle experimental features"),
    BotCommand("feedback", "Send logs to the Codex maintainers"),
    BotCommand("init", "Generate an AGENTS.md scaffold"),
    BotCommand("logout", "Sign out of Codex"),
    BotCommand("mcp", "List configured MCP tools"),
    BotCommand("mention", "Attach a file to the conversation"),
    BotCommand("model", "Choose the active model"),
    BotCommand("plan", "Switch to plan mode"),
    BotCommand("personality", "Choose a communication style"),
    BotCommand("ps", "Show background terminals and output"),
    BotCommand("fork", "Fork the current conversation"),
    BotCommand("resume", "Resume a saved conversation"),
    BotCommand("new", "Start a new conversation"),
    BotCommand("quit", "Exit the Codex CLI"),
    BotCommand("review", "Ask Codex to review your working tree"),
    BotCommand("status", "Display session configuration and token usage"),
    BotCommand("debug_config", "Print config and requirements diagnostics"),
    BotCommand("statusline", "Configure TUI status-line fields"),
]

TELEGRAM_COMMANDS = TELEDEX_COMMANDS + CODEX_COMMANDS


@dataclass(slots=True)
class PendingFreeformReply:
    interaction_kind: str
    prompt: str


class TelegramBridge:
    def __init__(self, settings: Settings, store: StateStore, state: BridgeState):
        self.settings = settings
        self.store = store
        self.state = state
        self.pending_freeform: PendingFreeformReply | None = None
        self.typing_task: asyncio.Task | None = None
        self.session = CodexSession(
            codex_cmd=settings.codex_cmd,
            root_dir=settings.session_storage_dir,
            workspace_root=settings.workspace_root,
            memory_file=settings.memory_file,
            on_text=self._send_codex_text,
            on_interaction=self._send_interaction,
        )
        self.media = MediaPipeline(settings.transcriber_backend, settings.whisper_model)
        request = HTTPXRequest(httpx_kwargs={"trust_env": False})
        self.application = (
            Application.builder()
            .token(settings.telegram_bot_token)
            .request(request)
            .get_updates_request(request)
            .concurrent_updates(False)
            .build()
        )
        self._wire_handlers()

    async def run(self) -> None:
        if not shutil.which(self.settings.codex_cmd):
            raise RuntimeError(f"Codex command not found: {self.settings.codex_cmd}")
        if not self.state.pairing.is_paired() and not self.state.pairing.code_valid():
            code = issue_pair_code(
                self.state,
                self.settings.pair_code_length,
                self.settings.pair_code_ttl_seconds,
            )
            self.store.save(self.state)
            print(f"Pairing code: {code}")
            print("Send this code to the Telegram bot from your private chat.")
        await self.session.start()
        await self.application.initialize()
        await self.application.bot.set_my_commands(TELEGRAM_COMMANDS)
        await self.application.start()
        await self.application.updater.start_polling(poll_interval=self.settings.poll_interval_seconds)
        logger.info("Telegram bridge started")
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()
            await self.session.stop()

    def _wire_handlers(self) -> None:
        self.application.add_handler(CommandHandler("help", self._handle_help))
        self.application.add_handler(CommandHandler("bridge_status", self._handle_status))
        self.application.add_handler(CommandHandler("start_session", self._handle_start_session))
        self.application.add_handler(CommandHandler("interrupt", self._handle_interrupt))
        self.application.add_handler(CommandHandler("reset", self._handle_reset))
        self.application.add_handler(CallbackQueryHandler(self._handle_callback))
        self.application.add_handler(MessageHandler(filters.PHOTO, self._handle_photo))
        self.application.add_handler(MessageHandler(filters.VOICE, self._handle_voice))
        self.application.add_handler(MessageHandler(filters.Document.IMAGE, self._handle_image_document))
        self.application.add_handler(MessageHandler(filters.COMMAND, self._handle_codex_command))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))
        self.application.add_error_handler(self._handle_error)

    def _authorized(self, update: Update) -> bool:
        message = update.effective_message
        chat_id = message.chat_id if message else (update.effective_chat.id if update.effective_chat else None)
        return bool(chat_id and self.state.pairing.authorized_chat_id == chat_id)

    async def _send_to_authorized_chat(self, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        chat_id = self.state.pairing.authorized_chat_id
        if chat_id is None:
            return
        for chunk in chunk_text(text):
            try:
                await self.application.bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    reply_markup=reply_markup,
                )
            except NetworkError as exc:
                logger.error("Telegram network error while sending message: %s", exc)
                return
            except TelegramError as exc:
                logger.error("Telegram API error while sending message: %s", exc)
                return
            reply_markup = None

    async def _send_codex_text(self, text: str) -> None:
        await self._send_to_authorized_chat(text)

    async def _send_interaction(self, interaction: InteractionPrompt, raw_text: str) -> None:
        buttons = [
            [InlineKeyboardButton(option.label[:64], callback_data=f"choice:{option.id}")]
            for option in interaction.options
        ]
        if interaction.allow_custom:
            buttons.append([InlineKeyboardButton("Custom Reply", callback_data="choice:custom")])
        markup = InlineKeyboardMarkup(buttons)
        await self._send_to_authorized_chat(raw_text, reply_markup=markup)

    def _option_by_id(self, option_id: str) -> str | None:
        interaction = self.session.pending_interaction
        if not interaction:
            return None
        for option in interaction.options:
            if option.id == option_id:
                return option.value
        return None

    async def _ensure_paired(self, message: Message) -> bool:
        if self.state.pairing.is_paired():
            if self.state.pairing.authorized_chat_id == message.chat_id:
                return True
            return False
        success, status = pair_chat(
            self.state,
            message.text or "",
            message.chat_id,
            self.settings.pair_max_attempts,
            self.settings.pair_cooldown_seconds,
        )
        self.store.save(self.state)
        if success:
            await self._safe_reply(message, "Pairing complete. This chat is now authorized.")
            return True
        if status == "cooldown":
            await self._safe_reply(message, "Pairing is temporarily locked. Try again later.")
        elif status == "expired":
            await self._safe_reply(message, "Pairing code expired. Regenerate it locally and retry.")
        elif status == "invalid":
            await self._safe_reply(message, "Invalid pairing code.")
        else:
            await self._safe_reply(message, "Bridge is already paired with another chat.")
        return False

    async def _handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await self._safe_reply(
            update.effective_message,
            "/help /bridge_status /start_session /interrupt /reset\n"
            "Telegram slash suggestions include the built-in Codex commands.\n"
            "Codex-native commands such as /status, /model, /review, /plan, /resume and /quit are passed through to Codex.\n"
            "Send plain text to Codex.\n"
            "Send an image to pass a local file path to Codex.\n"
            "Send a voice message to transcribe and forward to Codex.",
        )

    async def _handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        status = self.session.status()
        pending = status.pending_interaction.kind if status.pending_interaction else "none"
        await self._safe_reply(
            update.effective_message,
            f"paired: yes\nstate: {status.state}\npid: {status.pid}\nsession_dir: {status.session_dir}\n"
            f"pending_interaction: {pending}\nlast_output_at: {status.last_output_at}\n"
            f"memory_file: {self.settings.memory_file}",
        )

    async def _handle_start_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await self.session.start()
        await self._safe_reply(update.effective_message, "Codex session is ready.")

    async def _handle_interrupt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await self.session.interrupt()
        await self._safe_reply(update.effective_message, "Sent interrupt to Codex.")

    async def _handle_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        self.pending_freeform = None
        await self.session.reset()
        await self._safe_reply(update.effective_message, "Codex session reset.")

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None:
            return
        if not self.state.pairing.is_paired():
            await self._ensure_paired(message)
            return
        if not self._authorized(update):
            return
        if self.pending_freeform:
            await self._run_with_typing(message.chat_id, self.session.send_text(message.text or ""))
            self.session.pending_interaction = None
            self.pending_freeform = None
            await self._safe_reply(message, "Custom reply forwarded.")
            return
        await self._run_with_typing(message.chat_id, self.session.send_text(message.text or ""))

    async def _handle_codex_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or not message.text:
            return
        if not self.state.pairing.is_paired():
            await self._ensure_paired(message)
            return
        if not self._authorized(update):
            return
        command_text = normalize_telegram_slash_command(message.text)
        await self._run_with_typing(message.chat_id, self.session.send_text(command_text))

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if not self._authorized(update):
            return
        if not self.session.pending_interaction:
            await query.edit_message_reply_markup(reply_markup=None)
            return
        choice = (query.data or "").split(":", 1)[-1]
        if choice == "custom":
            self.pending_freeform = PendingFreeformReply(
                interaction_kind=self.session.pending_interaction.kind,
                prompt=self.session.pending_interaction.prompt,
            )
            await self._safe_reply(query.message, "Send the next text message as your custom reply.")
            return
        value = self._option_by_id(choice)
        if not value:
            await self._safe_reply(query.message, "That option is no longer available.")
            return
        chat_id = query.message.chat_id if query.message else None
        await self._run_with_typing(chat_id, self.session.send_text(value))
        self.session.pending_interaction = None
        self.pending_freeform = None
        await self._safe_reply(query.message, f"Selected: {value}")

    async def _handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        message = update.effective_message
        if message is None or not message.photo:
            return
        photo = message.photo[-1]
        tg_file = await photo.get_file()
        session_dir = self.session.status().session_dir
        if session_dir is None:
            await self.session.start()
            session_dir = self.session.status().session_dir
        target = Path(session_dir) / "media" / f"photo-{int(time.time())}.jpg"
        path = await self.media.save_telegram_file(tg_file, target)
        prompt = f"User uploaded an image.\nLocal file path: {path}\nCaption: {message.caption or '(none)'}"
        await self._run_with_typing(message.chat_id, self.session.send_text(prompt))
        await self._safe_reply(message, f"Image saved and forwarded: {path.name}")

    async def _handle_image_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        message = update.effective_message
        if message is None or message.document is None:
            return
        tg_file = await message.document.get_file()
        session_dir = self.session.status().session_dir
        if session_dir is None:
            await self.session.start()
            session_dir = self.session.status().session_dir
        filename = message.document.file_name or f"image-{int(time.time())}"
        target = Path(session_dir) / "media" / filename
        path = await self.media.save_telegram_file(tg_file, target)
        prompt = f"User uploaded an image document.\nLocal file path: {path}\nCaption: {message.caption or '(none)'}"
        await self._run_with_typing(message.chat_id, self.session.send_text(prompt))
        await self._safe_reply(message, f"Image saved and forwarded: {path.name}")

    async def _handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        message = update.effective_message
        if message is None or message.voice is None:
            return
        tg_file = await message.voice.get_file()
        session_dir = self.session.status().session_dir
        if session_dir is None:
            await self.session.start()
            session_dir = self.session.status().session_dir
        target = Path(session_dir) / "media" / f"voice-{int(time.time())}.ogg"
        path = await self.media.save_telegram_file(tg_file, target)
        try:
            transcript = await self.media.transcribe(path)
            payload = f"User uploaded a voice message.\nLocal file path: {path}\nTranscript:\n{transcript}"
            await self._run_with_typing(message.chat_id, self.session.send_text(payload))
            await self._safe_reply(message, "Voice transcribed and forwarded.")
        except Exception as exc:
            logger.exception("Voice transcription failed")
            await self._safe_reply(message, f"Voice saved but transcription failed: {exc}")

    async def unpair(self) -> None:
        clear_pairing(self.state)
        self.store.save(self.state)

    async def _safe_reply(self, message: Message | None, text: str) -> None:
        if message is None:
            return
        try:
            await message.reply_text(text)
        except NetworkError as exc:
            logger.error("Telegram network error while replying: %s", exc)
        except TelegramError as exc:
            logger.error("Telegram API error while replying: %s", exc)

    async def _handle_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.exception("Unhandled telegram application error", exc_info=context.error)

    async def _run_with_typing(self, chat_id: int | None, operation) -> None:
        if chat_id is None:
            await operation
            return
        self._stop_typing()
        self.typing_task = asyncio.create_task(self._typing_loop(chat_id))
        try:
            await operation
        finally:
            await self._stop_typing()

    async def _typing_loop(self, chat_id: int) -> None:
        try:
            while True:
                await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            raise
        except NetworkError as exc:
            logger.error("Telegram network error while sending typing action: %s", exc)
        except TelegramError as exc:
            logger.error("Telegram API error while sending typing action: %s", exc)

    async def _stop_typing(self) -> None:
        if self.typing_task is None:
            return
        self.typing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.typing_task
        self.typing_task = None
