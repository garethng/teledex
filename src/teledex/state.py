from __future__ import annotations

import hashlib
import json
import secrets
import string
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def _now() -> float:
    return time.time()


@dataclass(slots=True)
class PairingState:
    authorized_user_ids: list[int]
    code_hash: str | None = None
    code_expires_at: float | None = None
    failed_attempts: int = 0
    cooldown_until: float | None = None
    last_code_issued_at: float | None = None

    def __init__(
        self,
        authorized_user_ids: list[int] | None = None,
        code_hash: str | None = None,
        code_expires_at: float | None = None,
        failed_attempts: int = 0,
        cooldown_until: float | None = None,
        last_code_issued_at: float | None = None,
        # Legacy fields for backward compatibility
        authorized_chat_id: int | None = None,
        authorized_chat_ids: list[int] | None = None,
    ):
        # Handle backward compatibility: chat_id(s) -> user_id(s)
        # Old single-chat format used private chat_id which equals user_id
        if authorized_user_ids is None:
            if authorized_chat_ids:
                authorized_user_ids = authorized_chat_ids
            elif authorized_chat_id is not None:
                authorized_user_ids = [authorized_chat_id]
            else:
                authorized_user_ids = []
        self.authorized_user_ids = authorized_user_ids
        self.code_hash = code_hash
        self.code_expires_at = code_expires_at
        self.failed_attempts = failed_attempts
        self.cooldown_until = cooldown_until
        self.last_code_issued_at = last_code_issued_at

    def is_paired(self) -> bool:
        return len(self.authorized_user_ids) > 0

    def is_authorized(self, user_id: int) -> bool:
        return user_id in self.authorized_user_ids

    def on_cooldown(self) -> bool:
        return bool(self.cooldown_until and self.cooldown_until > _now())

    def code_valid(self) -> bool:
        return bool(self.code_hash and self.code_expires_at and self.code_expires_at > _now())


@dataclass(slots=True)
class BridgeState:
    pairing: PairingState

    @classmethod
    def empty(cls) -> "BridgeState":
        return cls(pairing=PairingState())


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> BridgeState:
        if not self.path.exists():
            return BridgeState.empty()
        raw = self.path.read_text(encoding="utf-8").strip()
        if not raw:
            return BridgeState.empty()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return BridgeState.empty()
        pairing = PairingState(**payload.get("pairing", {}))
        return BridgeState(pairing=pairing)

    def save(self, state: BridgeState) -> None:
        self.path.write_text(
            json.dumps({"pairing": asdict(state.pairing)}, indent=2, sort_keys=True),
            encoding="utf-8",
        )


def generate_pair_code(length: int) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def issue_pair_code(state: BridgeState, length: int, ttl_seconds: int) -> str:
    code = generate_pair_code(length)
    state.pairing.code_hash = hash_code(code)
    state.pairing.code_expires_at = _now() + ttl_seconds
    state.pairing.failed_attempts = 0
    state.pairing.cooldown_until = None
    state.pairing.last_code_issued_at = _now()
    return code


def clear_pairing(state: BridgeState) -> None:
    state.pairing = PairingState()


def pair_chat(
    state: BridgeState,
    code: str,
    user_id: int,
    max_attempts: int,
    cooldown_seconds: int,
) -> tuple[bool, str]:
    """Pair a user (identified by user_id from private chat).
    
    After pairing, the user can use the bot in private chats and any group.
    """
    if state.pairing.is_authorized(user_id):
        return False, "already_paired"
    if state.pairing.on_cooldown():
        return False, "cooldown"
    if not state.pairing.code_valid():
        return False, "expired"
    if hash_code(code.strip().upper()) != state.pairing.code_hash:
        state.pairing.failed_attempts += 1
        if state.pairing.failed_attempts >= max_attempts:
            state.pairing.cooldown_until = _now() + cooldown_seconds
        return False, "invalid"
    # Add this user to authorized list
    state.pairing.authorized_user_ids.append(user_id)
    state.pairing.code_hash = None
    state.pairing.code_expires_at = None
    state.pairing.failed_attempts = 0
    state.pairing.cooldown_until = None
    return True, "paired"


def unpair_user(state: BridgeState, user_id: int) -> bool:
    """Remove a specific user from authorized list. Returns True if removed."""
    if user_id in state.pairing.authorized_user_ids:
        state.pairing.authorized_user_ids.remove(user_id)
        return True
    return False
