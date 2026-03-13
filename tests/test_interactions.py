from teledex.interactions import detect_interaction_prompt


def test_detects_approval_prompt():
    prompt = """Approval required
- Read-only
- Workspace write
- Danger full access
"""
    result = detect_interaction_prompt(prompt)
    assert result is not None
    assert result.kind == "approval"
    assert [option.value for option in result.options] == [
        "Read-only",
        "Workspace write",
        "Danger full access",
    ]


def test_detects_plan_question():
    prompt = """Question: choose a path
1. Fast path (Recommended)
2. Safe path
"""
    result = detect_interaction_prompt(prompt)
    assert result is not None
    assert result.kind == "plan_question"
