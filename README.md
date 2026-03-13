# teledex

`teledex` is a macOS CLI bridge that lets you operate a local `codex` workflow from Telegram.

It runs as a local process on your Mac, binds to a single Telegram chat with a one-time pairing
code, forwards messages to `codex`, and sends the replies back to Telegram.

## What It Does

- Pair one Telegram chat to one local bridge instance with a one-time code
- Forward Telegram text messages to `codex`
- Keep multi-turn context by resuming the same Codex thread between messages
- Inject a global `Memory.md` file into every Codex request
- Support Telegram inline buttons for approval-like and plan-question interactions
- Accept Telegram image uploads and pass local file paths into Codex
- Accept Telegram voice messages and transcribe them with `faster-whisper`
- Show Telegram `typing...` while Codex is still working

## How It Works

`teledex` does not drive the interactive Codex TUI anymore.

Instead, it uses:

- `codex exec --json` for the first request
- `codex exec resume <thread_id> --json` for later requests

This avoids TUI rendering issues and keeps the conversation state inside a Codex thread.

## Requirements

- macOS
- Python 3.11+
- A working local `codex` CLI installation
- A Telegram bot token
- Optional: `faster-whisper` if you want voice transcription

## Install

Install from PyPI:

```bash
pip install teledex-cli
```

```bash
uv tool install teledex-cli
```

Install from local source for development:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

```bash
uv venv
source .venv/bin/activate
uv pip install -e '.[dev]'
```

Optional voice transcription support:

```bash
pip install -e .[faster-whisper]
```

```bash
uv pip install -e '.[faster-whisper]'
```

## First-Time Setup

Run:

```bash
teledex init
```

The setup wizard asks for:

- Telegram bot token
- Codex command
- Workspace root
- Global memory file
- State file path
- Session storage directory
- Voice transcription backend
- Whisper model

Configuration is stored in:

```text
~/.teledex/config.json
```

## Global Memory

Every Codex request includes the contents of a global memory file before the user message.

Default path:

```text
~/.teledex/Memory.md
```

Use it for durable context such as:

- who the user is
- preferred response style
- project conventions
- persistent reminders for the bridge

## Run

Start the bridge:

```bash
teledex run
```

`teledex run` starts the bridge as a background process and returns immediately.

If the bridge is not paired yet, it prints a one-time pairing code locally before daemonizing. Send
that code to the Telegram bot from the chat you want to authorize.

Useful lifecycle commands:

```bash
teledex status
teledex stop
teledex restart
```

## Telegram Usage

After pairing, you can:

- send plain text to talk to Codex
- upload images
- upload voice messages
- tap inline buttons for structured replies

Built-in bot commands:

```bash
/help
/status
/start_session
/interrupt
/reset
```

## CLI Commands

```bash
teledex init
teledex run
teledex restart
teledex stop
teledex status
teledex unpair
```

## Notes

- The bridge authorizes exactly one Telegram chat at a time.
- `Memory.md` is global, not per-project.
- The bridge strips proxy and conflicting `CODEX_*` environment variables before invoking Codex.
- Voice transcription is optional; if disabled, voice files are still saved locally but not
  transcribed.

## Publishing

PyPI publishing is handled by the GitHub Actions workflow at
[` .github/workflows/publish.yml `](.github/workflows/publish.yml).

Before the first release, configure PyPI Trusted Publishing so this repository is allowed to publish
without a long-lived API token.

Release process:

1. Update the version in `pyproject.toml`.
2. Commit the version change to `main`.
3. Create a Git tag that matches the package version exactly, for example `0.1.0`.
4. Push the tag and create a GitHub Release from that tag.
5. When the GitHub Release is published, the workflow builds the package and uploads it to PyPI.

Versioning convention:

- `pyproject.toml` uses the canonical package version, for example `0.1.0`.
- Git tags use the exact same version string, for example `0.1.0`.
- The GitHub Release tag and `pyproject.toml` version should always match exactly.

Example:

```text
pyproject.toml -> version = "0.1.0"
git tag        -> 0.1.0
GitHub Release -> 0.1.0
PyPI release   -> 0.1.0
```

## Development

Run tests:

```bash
.venv/bin/pytest
```

Current test suite covers config, state, prompt composition, output cleaning, and interaction
parsing.
