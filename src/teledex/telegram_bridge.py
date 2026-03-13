from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Union

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
from .config import BotConfig, Settings
from .interactions import InteractionPrompt
from .media import MediaPipeline
from .opencode_session import OpencodeSession
from .state import BridgeState, StateStore, clear_pairing, issue_pair_code, pair_chat, unpair_user
from .util import chunk_text, normalize_telegram_slash_command


logger = logging.getLogger(__name__)

# Commands registered for every bot regardless of agent type
TELEDEX_COMMANDS = [
    BotCommand("help", "Show teledex help"),
    BotCommand("bridge_status", "Show teledex daemon and session status"),
    BotCommand("start_session", "Ensure the agent session is ready"),
    BotCommand("interrupt", "Interrupt the current agent request"),
    BotCommand("reset", "Reset the current agent thread"),
    BotCommand("unpair_chat", "Remove this chat from authorized list"),
]

CODEX_COMMANDS = [
    BotCommand("permissions", "Set what Codex can do without asking first"),
    BotCommand("sandbox_add_read_dir", "Grant extra sandbox read access"),
    BotCommand("agent", "Switch the active agent thread"),
    BotCommand("apps", "Browse apps and insert them into your prompt"),
    BotCommand("clear", "Clear the terminal and start a fresh chat"),
    BotCommand("compact", "Summarize the visible conversation"),
    BotCommand("copy", "Copy the latest completed output"),
    BotCommand("diff", "Show the current Git diff"),
    BotCommand("exit", "Exit the CLI"),
    BotCommand("experimental", "Toggle experimental features"),
    BotCommand("feedback", "Send logs to the maintainers"),
    BotCommand("init", "Generate an AGENTS.md scaffold"),
    BotCommand("logout", "Sign out"),
    BotCommand("mcp", "List configured MCP tools"),
    BotCommand("mention", "Attach a file to the conversation"),
    BotCommand("model", "Choose the active model"),
    BotCommand("plan", "Switch to plan mode"),
    BotCommand("personality", "Choose a communication style"),
    BotCommand("ps", "Show background terminals and output"),
    BotCommand("fork", "Fork the current conversation"),
    BotCommand("resume", "Resume a saved conversation"),
    BotCommand("new", "Start a new conversation"),
    BotCommand("quit", "Exit the CLI"),
    BotCommand("review", "Ask the agent to review your working tree"),
    BotCommand("status", "Display session configuration and token usage"),
    BotCommand("debug_config", "Print config and requirements diagnostics"),
    BotCommand("statusline", "Configure TUI status-line fields"),
]

OPENCODE_COMMANDS = [
    BotCommand("model", "Choose the active model"),
    BotCommand("new", "Start a new conversation"),
    BotCommand("resume", "Resume a saved conversation"),
    BotCommand("logout", "Sign out"),
    BotCommand("status", "Display session configuration"),
    BotCommand("mcp", "List configured MCP tools"),
]


AgentSession = Union[CodexSession, OpencodeSession]


@dataclass(slots=True)
class PendingFreeformReply:
    interaction_kind: str
    prompt: str


def _make_session(bot_cfg: BotConfig, on_text, on_interaction) -> AgentSession:
    """Factory: create the right session type based on agent_type."""
    if bot_cfg.agent_type == "opencode":
        return OpencodeSession(
            opencode_cmd=bot_cfg.agent_cmd,
            root_dir=bot_cfg.session_storage_dir,
            workspace_root=bot_cfg.workspace_root,
            memory_file=bot_cfg.memory_file,
            on_text=on_text,
            on_interaction=on_interaction,
            model=bot_cfg.model,
        )
    return CodexSession(
        codex_cmd=bot_cfg.agent_cmd,
        root_dir=bot_cfg.session_storage_dir,
        workspace_root=bot_cfg.workspace_root,
        memory_file=bot_cfg.memory_file,
        on_text=on_text,
        on_interaction=on_interaction,
    )


class BotInstance:
    """A single Telegram bot with per-chat isolated agent sessions.
    
    Each chat (private or group) gets its own independent session with
    isolated conversation history and context.
    """

    def __init__(self, bot_cfg: BotConfig, global_settings: Settings, store: StateStore, state: BridgeState):
        self.bot_cfg = bot_cfg
        self.global_settings = global_settings
        self.store = store
        self.state = state
        
        # Per-chat sessions: {chat_id: AgentSession}
        self.sessions: dict[int, AgentSession] = {}
        # Per-chat pending freeform replies
        self.pending_freeforms: dict[int, PendingFreeformReply] = {}
        # Per-chat typing tasks
        self.typing_tasks: dict[int, asyncio.Task] = {}
        # Cache bot username for mention detection
        self.bot_username: str | None = None

        # Configure per-bot logging
        self._setup_logging()

        self.media = MediaPipeline(
            global_settings.transcriber_backend,
            global_settings.whisper_model,
        )
        request = HTTPXRequest(httpx_kwargs={"trust_env": False})
        self.application = (
            Application.builder()
            .token(bot_cfg.telegram_bot_token)
            .request(request)
            .get_updates_request(request)
            .concurrent_updates(False)
            .build()
        )
        self._wire_handlers()

    def _setup_logging(self) -> None:
        """Add a file handler for this bot's log file."""
        bot_cfg = self.bot_cfg
        bot_cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(bot_cfg.log_file, encoding="utf-8")
        formatter = logging.Formatter(
            f"%(asctime)s [{bot_cfg.name}] %(levelname)s %(name)s: %(message)s"
        )
        handler.setFormatter(formatter)
        # Add handler to root logger so all modules' logs go to bot's file
        logging.getLogger().addHandler(handler)

    def _commands(self) -> list[BotCommand]:
        if self.bot_cfg.agent_type == "opencode":
            return TELEDEX_COMMANDS + OPENCODE_COMMANDS
        return TELEDEX_COMMANDS + CODEX_COMMANDS

    def _get_or_create_session(self, chat_id: int) -> AgentSession:
        """Get existing session for this chat, or create a new isolated one."""
        if chat_id in self.sessions:
            return self.sessions[chat_id]
        
        # Create per-chat session directory: ~/.teledex/sessions/<botname>/<chat_id>/
        session_root = self.bot_cfg.session_storage_dir / str(chat_id)
        session_root.mkdir(parents=True, exist_ok=True)
        
        # Factory creates the session with chat-specific callbacks
        def on_text(text: str):
            return self._send_agent_text(chat_id, text)
        
        def on_interaction(interaction: InteractionPrompt, raw_text: str):
            return self._send_interaction(chat_id, interaction, raw_text)
        
        if self.bot_cfg.agent_type == "opencode":
            session = OpencodeSession(
                opencode_cmd=self.bot_cfg.agent_cmd,
                root_dir=session_root,
                workspace_root=self.bot_cfg.workspace_root,
                memory_file=self.bot_cfg.memory_file,
                on_text=on_text,
                on_interaction=on_interaction,
                model=self.bot_cfg.model,
            )
        else:
            session = CodexSession(
                codex_cmd=self.bot_cfg.agent_cmd,
                root_dir=session_root,
                workspace_root=self.bot_cfg.workspace_root,
                memory_file=self.bot_cfg.memory_file,
                on_text=on_text,
                on_interaction=on_interaction,
            )
        
        self.sessions[chat_id] = session
        logger.info("[%s] Created new session for chat %s", self.bot_cfg.name, chat_id)
        return session

    async def run(self) -> None:
        if not shutil.which(self.bot_cfg.agent_cmd):
            raise RuntimeError(
                f"Agent command not found for bot '{self.bot_cfg.name}': {self.bot_cfg.agent_cmd}"
            )
        if not self.state.pairing.is_paired() and not self.state.pairing.code_valid():
            code = issue_pair_code(
                self.state,
                self.global_settings.pair_code_length,
                self.global_settings.pair_code_ttl_seconds,
            )
            self.store.save(self.state)
            print(f"[{self.bot_cfg.name}] Pairing code: {code}")
            print(f"[{self.bot_cfg.name}] Send this code to the Telegram bot from your private chat.")
        await self.application.initialize()
        me = await self.application.bot.get_me()
        self.bot_username = me.username
        await self.application.bot.set_my_commands(self._commands())
        await self.application.start()
        assert self.application.updater is not None
        await self.application.updater.start_polling(
            poll_interval=self.global_settings.poll_interval_seconds
        )
        logger.info("Bot '%s' (%s) started", self.bot_cfg.name, self.bot_cfg.agent_type)
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()
            # Stop all active sessions
            for session in self.sessions.values():
                await session.stop()

    def _wire_handlers(self) -> None:
        self.application.add_handler(CommandHandler("help", self._handle_help))
        self.application.add_handler(CommandHandler("bridge_status", self._handle_status))
        self.application.add_handler(CommandHandler("start_session", self._handle_start_session))
        self.application.add_handler(CommandHandler("interrupt", self._handle_interrupt))
        self.application.add_handler(CommandHandler("reset", self._handle_reset))
        self.application.add_handler(CommandHandler("unpair_chat", self._handle_unpair_chat))
        if self.bot_cfg.agent_type == "opencode":
            self.application.add_handler(CommandHandler("model", self._handle_model))
        self.application.add_handler(CallbackQueryHandler(self._handle_callback))
        self.application.add_handler(MessageHandler(filters.PHOTO, self._handle_photo))
        self.application.add_handler(MessageHandler(filters.VOICE, self._handle_voice))
        self.application.add_handler(MessageHandler(filters.Document.IMAGE, self._handle_image_document))
        self.application.add_handler(MessageHandler(filters.COMMAND, self._handle_agent_command))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))
        self.application.add_error_handler(self._handle_error)

    def _authorized(self, update: Update) -> bool:
        """Check if the user who sent this update is authorized.
        
        Works in both private chats and groups — only checks user_id.
        """
        user = update.effective_user
        if user is None:
            return False
        return self.state.pairing.is_authorized(user.id)

    async def _send_to_chat(self, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        """Send message to a specific chat (private or group)."""
        for chunk in chunk_text(text):
            try:
                await self.application.bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    reply_markup=reply_markup,
                )
            except NetworkError as exc:
                logger.error("[%s] Telegram network error: %s", self.bot_cfg.name, exc)
                return
            except TelegramError as exc:
                logger.error("[%s] Telegram API error: %s", self.bot_cfg.name, exc)
                return
            reply_markup = None

    async def _send_agent_text(self, chat_id: int, text: str) -> None:
        """Callback from agent session — reply to the specific chat."""
        await self._send_to_chat(chat_id, text)

    async def _send_interaction(self, chat_id: int, interaction: InteractionPrompt, raw_text: str) -> None:
        """Callback from agent session — send interaction prompt to specific chat."""
        buttons = [
            [InlineKeyboardButton(option.label[:64], callback_data=f"choice:{option.id}")]
            for option in interaction.options
        ]
        if interaction.allow_custom:
            buttons.append([InlineKeyboardButton("Custom Reply", callback_data="choice:custom")])
        markup = InlineKeyboardMarkup(buttons)
        await self._send_to_chat(chat_id, raw_text, reply_markup=markup)

    def _option_by_id(self, chat_id: int, option_id: str) -> str | None:
        session = self.sessions.get(chat_id)
        if not session:
            return None
        interaction = session.pending_interaction
        if not interaction:
            return None
        for option in interaction.options:
            if option.id == option_id:
                return option.value
        return None

    async def _ensure_paired(self, message: Message) -> bool:
        """Handle pairing via private chat only.
        
        Must be called from a private chat. Pairs the user (not the chat),
        so after pairing in private chat, the user can use bot in groups.
        """
        # Always reload from disk so that codes issued by `teledex pair`
        # after the daemon started are visible to the running process.
        self.state = self.store.load()
        
        # Private chat: message.chat.id == message.from_user.id
        # Group chat: message.chat.id != message.from_user.id
        from_user = message.from_user
        if from_user is None:
            return False
        
        # Only allow pairing in private chats
        if message.chat.type != "private":
            await self._safe_reply(message, "Pairing must be done in a private chat with the bot.")
            return False
        
        user_id = from_user.id
        if self.state.pairing.is_authorized(user_id):
            return True
        
        success, status = pair_chat(
            self.state,
            message.text or "",
            user_id,
            self.global_settings.pair_max_attempts,
            self.global_settings.pair_cooldown_seconds,
        )
        self.store.save(self.state)
        if success:
            await self._safe_reply(message, "Pairing complete. You can now use this bot in private chats and groups.")
            return True
        if status == "cooldown":
            await self._safe_reply(message, "Pairing is temporarily locked. Try again later.")
        elif status == "expired":
            await self._safe_reply(message, "Pairing code expired. Regenerate it locally and retry.")
        elif status == "invalid":
            await self._safe_reply(message, "Invalid pairing code.")
        else:
            await self._safe_reply(message, "You are not authorized. Send the pairing code in a private chat.")
        return False

    async def _handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        agent_label = self.bot_cfg.agent_type.capitalize()
        await self._safe_reply(
            update.effective_message,
            f"Bot: {self.bot_cfg.name} ({agent_label})\n\n"
            "Commands:\n"
            "/help /bridge_status /start_session /interrupt /reset /unpair_chat\n\n"
            f"{agent_label} slash commands are passed through to the agent.\n"
            "Send plain text to the agent.\n"
            "Send an image to pass a local file path to the agent.\n"
            "Send a voice message to transcribe and forward to the agent.\n\n"
            "Note: After pairing in a private chat, you can use this bot in any Telegram group.",
        )

    async def _handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        message = update.effective_message
        if message is None:
            return
        session = self._get_or_create_session(message.chat_id)
        status = session.status()
        pending = status.pending_interaction.kind if status.pending_interaction else "none"
        users = ", ".join(str(uid) for uid in self.state.pairing.authorized_user_ids) or "(none)"
        await self._safe_reply(
            message,
            f"bot: {self.bot_cfg.name}\n"
            f"agent: {self.bot_cfg.agent_type}\n"
            f"chat_id: {message.chat_id}\n"
            f"authorized_users: {users}\n"
            f"active_sessions: {len(self.sessions)}\n"
            f"state: {status.state}\npid: {status.pid}\n"
            f"session_dir: {status.session_dir}\n"
            f"pending_interaction: {pending}\nlast_output_at: {status.last_output_at}\n"
            f"memory_file: {self.bot_cfg.memory_file}",
        )

    async def _handle_start_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        message = update.effective_message
        if message is None:
            return
        session = self._get_or_create_session(message.chat_id)
        await session.start()
        await self._safe_reply(message, f"{self.bot_cfg.agent_type.capitalize()} session is ready.")

    async def _handle_interrupt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        message = update.effective_message
        if message is None:
            return
        session = self.sessions.get(message.chat_id)
        if session:
            await session.interrupt()
            await self._safe_reply(message, f"Sent interrupt to {self.bot_cfg.agent_type}.")
        else:
            await self._safe_reply(message, "No active session for this chat.")

    async def _handle_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        message = update.effective_message
        if message is None:
            return
        session = self.sessions.get(message.chat_id)
        if session:
            self.pending_freeforms.pop(message.chat_id, None)
            await session.reset()
            await self._safe_reply(message, f"{self.bot_cfg.agent_type.capitalize()} session reset.")
        else:
            await self._safe_reply(message, "No active session for this chat.")

    async def _handle_unpair_chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Remove the current user from authorized list."""
        if not self._authorized(update):
            return
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        removed = unpair_user(self.state, user.id)
        self.store.save(self.state)
        if removed:
            await self._safe_reply(message, f"You have been removed from authorized users. You will no longer be able to use this bot.")
        else:
            await self._safe_reply(message, "You were not in the authorized list.")

    async def _handle_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/model [provider/model] — show or set the active model (opencode bots only).

        Usage:
          /model                         — show current model
          /model openrouter/openai/gpt-5   — switch model (takes effect on next message)
          /model reset                   — revert to opencode default
        """
        if not self._authorized(update):
            return
        message = update.effective_message
        if message is None:
            return
        session = self._get_or_create_session(message.chat_id)
        if not isinstance(session, OpencodeSession):
            await self._safe_reply(message, "This command is only available for opencode bots.")
            return
        args = (context.args or [])
        if not args:
            current = session.model or "(opencode default)"
            await self._safe_reply(message, f"Current model: {current}\nUsage: /model provider/model")
            return
        target = args[0].strip()
        if target.lower() == "reset":
            session.model = None
            await self._safe_reply(message, "Model reset to opencode default.")
        else:
            session.model = target
            await self._safe_reply(message, f"Model set to: {target}\n(takes effect on next message)")

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None:
            return
        if not self.state.pairing.is_paired():
            await self._ensure_paired(message)
            return
        if not self._authorized(update):
            return
        # In groups, only respond if bot is mentioned or replied to
        text_to_send = message.text or ""
        if message.chat.type in ("group", "supergroup"):
            bot_mentioned = False
            # Check if message is a reply to bot
            if message.reply_to_message and message.reply_to_message.from_user:
                if message.reply_to_message.from_user.id == self.application.bot.id:
                    bot_mentioned = True
            # Check if bot is mentioned in entities
            if not bot_mentioned and message.entities and self.bot_username:
                for entity in message.entities:
                    if entity.type == "mention" and message.text:
                        mention_text = message.text[entity.offset:entity.offset + entity.length]
                        if mention_text.lstrip("@") == self.bot_username:
                            bot_mentioned = True
                            # Remove @mention from text sent to agent
                            text_to_send = text_to_send.replace(mention_text, "").strip()
                    elif entity.type == "text_mention":
                        if entity.user and entity.user.id == self.application.bot.id:
                            bot_mentioned = True
            if not bot_mentioned:
                return
        
        session = self._get_or_create_session(message.chat_id)
        if self.pending_freeforms.get(message.chat_id):
            await self._run_with_typing(message.chat_id, session.send_text(text_to_send))
            session.pending_interaction = None
            self.pending_freeforms.pop(message.chat_id, None)
            await self._safe_reply(message, "Custom reply forwarded.")
            return
        await self._run_with_typing(message.chat_id, session.send_text(text_to_send))

    async def _handle_agent_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or not message.text:
            return
        if not self.state.pairing.is_paired():
            await self._safe_reply(message, "Not paired. Send the pairing code as a plain text message.")
            return
        if not self._authorized(update):
            return
        session = self._get_or_create_session(message.chat_id)
        command_text = normalize_telegram_slash_command(message.text)
        await self._run_with_typing(message.chat_id, session.send_text(command_text))

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if not self._authorized(update):
            return
        query_message = query.message
        chat_id = getattr(query_message, "chat_id", None) if query_message else None
        if chat_id is None:
            return
        session = self.sessions.get(chat_id)
        if not session or not session.pending_interaction:
            await query.edit_message_reply_markup(reply_markup=None)
            return
        choice = (query.data or "").split(":", 1)[-1]
        if choice == "custom":
            self.pending_freeforms[chat_id] = PendingFreeformReply(
                interaction_kind=session.pending_interaction.kind,
                prompt=session.pending_interaction.prompt,
            )
            if query_message and hasattr(query_message, "reply_text"):
                await self._safe_reply(query_message, "Send the next text message as your custom reply.")  # type: ignore[arg-type]
            return
        value = self._option_by_id(chat_id, choice)
        if not value:
            if query_message and hasattr(query_message, "reply_text"):
                await self._safe_reply(query_message, "That option is no longer available.")  # type: ignore[arg-type]
            return
        await self._run_with_typing(chat_id, session.send_text(value))
        session.pending_interaction = None
        self.pending_freeforms.pop(chat_id, None)
        if query_message and hasattr(query_message, "reply_text"):
            await self._safe_reply(query_message, f"Selected: {value}")  # type: ignore[arg-type]

    async def _handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        message = update.effective_message
        if message is None or not message.photo:
            return
        session = self._get_or_create_session(message.chat_id)
        photo = message.photo[-1]
        tg_file = await photo.get_file()
        session_dir = session.status().session_dir
        if session_dir is None:
            await session.start()
            session_dir = session.status().session_dir
        assert session_dir is not None
        target = Path(session_dir) / "media" / f"photo-{int(time.time())}.jpg"
        path = await self.media.save_telegram_file(tg_file, target)
        prompt = f"User uploaded an image.\nLocal file path: {path}\nCaption: {message.caption or '(none)'}"
        await self._run_with_typing(message.chat_id, session.send_text(prompt))
        await self._safe_reply(message, f"Image saved and forwarded: {path.name}")

    async def _handle_image_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        message = update.effective_message
        if message is None or message.document is None:
            return
        session = self._get_or_create_session(message.chat_id)
        tg_file = await message.document.get_file()
        session_dir = session.status().session_dir
        if session_dir is None:
            await session.start()
            session_dir = session.status().session_dir
        assert session_dir is not None
        filename = message.document.file_name or f"image-{int(time.time())}"
        target = Path(session_dir) / "media" / filename
        path = await self.media.save_telegram_file(tg_file, target)
        prompt = f"User uploaded an image document.\nLocal file path: {path}\nCaption: {message.caption or '(none)'}"
        await self._run_with_typing(message.chat_id, session.send_text(prompt))
        await self._safe_reply(message, f"Image saved and forwarded: {path.name}")

    async def _handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        message = update.effective_message
        if message is None or message.voice is None:
            return
        session = self._get_or_create_session(message.chat_id)
        tg_file = await message.voice.get_file()
        session_dir = session.status().session_dir
        if session_dir is None:
            await session.start()
            session_dir = session.status().session_dir
        assert session_dir is not None
        target = Path(session_dir) / "media" / f"voice-{int(time.time())}.ogg"
        path = await self.media.save_telegram_file(tg_file, target)
        try:
            transcript = await self.media.transcribe(path)
            payload = f"User uploaded a voice message.\nLocal file path: {path}\nTranscript:\n{transcript}"
            await self._run_with_typing(message.chat_id, session.send_text(payload))
            await self._safe_reply(message, "Voice transcribed and forwarded.")
        except Exception as exc:
            logger.exception("[%s] Voice transcription failed", self.bot_cfg.name)
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
            logger.error("[%s] Telegram network error while replying: %s", self.bot_cfg.name, exc)
        except TelegramError as exc:
            logger.error("[%s] Telegram API error while replying: %s", self.bot_cfg.name, exc)

    async def _handle_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.exception("[%s] Unhandled telegram application error", self.bot_cfg.name, exc_info=context.error)

    async def _run_with_typing(self, chat_id: int | None, operation) -> None:
        if chat_id is None:
            await operation
            return
        self._stop_typing_sync(chat_id)
        self.typing_tasks[chat_id] = asyncio.create_task(self._typing_loop(chat_id))
        try:
            await operation
        finally:
            await self._stop_typing(chat_id)

    async def _typing_loop(self, chat_id: int) -> None:
        try:
            while True:
                await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            raise
        except NetworkError as exc:
            logger.error("[%s] Network error during typing: %s", self.bot_cfg.name, exc)
        except TelegramError as exc:
            logger.error("[%s] Telegram error during typing: %s", self.bot_cfg.name, exc)

    def _stop_typing_sync(self, chat_id: int) -> None:
        task = self.typing_tasks.get(chat_id)
        if task is not None:
            task.cancel()
            self.typing_tasks.pop(chat_id, None)

    async def _stop_typing(self, chat_id: int) -> None:
        task = self.typing_tasks.get(chat_id)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self.typing_tasks.pop(chat_id, None)


class MultiAgentRunner:
    """Runs all configured bots concurrently in a single asyncio event loop."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.instances: list[BotInstance] = []
        for bot_cfg in settings.bots:
            store = StateStore(bot_cfg.state_file)
            state = store.load()
            self.instances.append(BotInstance(
                bot_cfg=bot_cfg,
                global_settings=settings,
                store=store,
                state=state,
            ))

    async def run(self) -> None:
        if not self.instances:
            raise RuntimeError("No bots configured. Add at least one bot to config.json.")
        tasks = [asyncio.create_task(inst.run(), name=inst.bot_cfg.name) for inst in self.instances]
        logger.info("Starting %d bot(s)", len(tasks))
        try:
            await asyncio.gather(*tasks)
        except Exception:
            for task in tasks:
                task.cancel()
            raise


# ---------------------------------------------------------------------------
# Backward-compat alias: TelegramBridge wraps a single BotInstance
# ---------------------------------------------------------------------------

class TelegramBridge:
    """Backward-compatible wrapper: single bot, single agent session.

    Accepts the legacy (settings, store, state) signature so existing code
    in cli.py that builds TelegramBridge directly continues to work.
    """

    def __init__(self, settings: Settings, store: StateStore, state: BridgeState):
        if not settings.bots:
            raise ValueError("No bots configured in settings")
        bot_cfg = settings.bots[0]
        self._instance = BotInstance(
            bot_cfg=bot_cfg,
            global_settings=settings,
            store=store,
            state=state,
        )

    async def run(self) -> None:
        await self._instance.run()
