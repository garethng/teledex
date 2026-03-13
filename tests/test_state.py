from pathlib import Path

from teledex.state import BridgeState, clear_pairing, issue_pair_code, pair_chat, unpair_user
from teledex.state import StateStore


def test_pairing_flow():
    state = BridgeState.empty()
    code = issue_pair_code(state, length=6, ttl_seconds=600)
    success, status = pair_chat(
        state=state,
        code=code,
        user_id=123,
        max_attempts=5,
        cooldown_seconds=60,
    )
    assert success is True
    assert status == "paired"
    assert 123 in state.pairing.authorized_user_ids
    assert state.pairing.is_authorized(123)


def test_clear_pairing():
    state = BridgeState.empty()
    code = issue_pair_code(state, length=6, ttl_seconds=600)
    pair_chat(state, code, 1, 5, 60)
    clear_pairing(state)
    assert len(state.pairing.authorized_user_ids) == 0


def test_load_empty_state_file(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text("", encoding="utf-8")
    state = StateStore(path).load()
    assert len(state.pairing.authorized_user_ids) == 0


def test_multi_user_pairing():
    """Test pairing multiple users."""
    state = BridgeState.empty()
    code1 = issue_pair_code(state, length=6, ttl_seconds=600)
    pair_chat(state, code1, 123, 5, 60)
    assert state.pairing.is_authorized(123)
    
    # Same bot can pair another user with new code
    code2 = issue_pair_code(state, length=6, ttl_seconds=600)
    pair_chat(state, code2, 456, 5, 60)
    assert state.pairing.is_authorized(456)
    assert len(state.pairing.authorized_user_ids) == 2
    
    # Unpair specific user
    unpair_user(state, 123)
    assert not state.pairing.is_authorized(123)
    assert state.pairing.is_authorized(456)
    assert len(state.pairing.authorized_user_ids) == 1
