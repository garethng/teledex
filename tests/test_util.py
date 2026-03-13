from teledex.util import normalize_codex_output, normalize_telegram_slash_command


def test_normalize_codex_output_removes_tui_chrome():
    raw = """
╭────────────────────╮
│ >_ OpenAI Codex    │
│ model: gpt-5.4     │
╰────────────────────╯
› hi
当前目录
  gpt-5.4 medium · 100% left · ~/Documents/code/own/teledex
"""
    assert normalize_codex_output(raw, "hi") == "当前目录"


def test_normalize_codex_output_keeps_meaningful_text():
    raw = "这是正常回答\n第二行"
    assert normalize_codex_output(raw, "hi") == "这是正常回答\n第二行"


def test_normalize_codex_output_removes_inline_prompt_and_status():
    raw = "›hi当前目录  gpt-5.4 medium · 100% left · ~/Documents/code/own/teledex"
    assert normalize_codex_output(raw, "hi") == "当前目录"


def test_normalize_telegram_slash_command_strips_bot_mention():
    assert normalize_telegram_slash_command("/model@MiniClaw_bot") == "/model"


def test_normalize_telegram_slash_command_keeps_args():
    assert normalize_telegram_slash_command("/model@MiniClaw_bot gpt-5.4") == "/model gpt-5.4"


def test_normalize_telegram_slash_command_maps_telegram_aliases():
    assert normalize_telegram_slash_command("/debug_config") == "/debug-config"
    assert normalize_telegram_slash_command("/sandbox_add_read_dir C:/tmp") == "/sandbox-add-read-dir C:/tmp"
