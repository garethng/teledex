from pathlib import Path

from teledex.state import BridgeState, clear_pairing, issue_pair_code, pair_chat
from teledex.state import StateStore


def test_pairing_flow():
    state = BridgeState.empty()
    code = issue_pair_code(state, length=6, ttl_seconds=600)
    success, status = pair_chat(
        state=state,
        code=code,
        chat_id=123,
        max_attempts=5,
        cooldown_seconds=60,
    )
    assert success is True
    assert status == "paired"
    assert state.pairing.authorized_chat_id == 123


def test_clear_pairing():
    state = BridgeState.empty()
    code = issue_pair_code(state, length=6, ttl_seconds=600)
    pair_chat(state, code, 1, 5, 60)
    clear_pairing(state)
    assert state.pairing.authorized_chat_id is None


def test_load_empty_state_file(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text("", encoding="utf-8")
    state = StateStore(path).load()
    assert state.pairing.authorized_chat_id is None
