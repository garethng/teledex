# teledex

Bridge a local interactive `codex` session to Telegram.

## Features

- Pair a single Telegram chat with a one-time code printed in the local terminal
- Proxy a local Codex PTY session to Telegram
- Stream Codex output back to Telegram
- Support inline keyboard replies for approvals and plan questions
- Download Telegram images and voice messages into a per-session directory
- Transcribe voice messages with `faster-whisper` when installed

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

Optional voice transcription:

```bash
pip install -e .[faster-whisper]
```

## First Run Setup

`teledex` no longer expects the Telegram token from environment variables.

On first run, it opens a terminal setup flow and asks for:

- Telegram bot token
- Codex command
- Workspace root
- Global memory file
- State file path
- Session storage directory
- Voice transcription backend
- Whisper model

The answers are saved to `~/.teledex/config.json`.

`Memory.md` is injected into every Codex request as persistent context. By default, it lives at
`~/.teledex/Memory.md` and is shared globally across workspaces.

## Run

```bash
teledex run
```

You can also rerun the setup wizard explicitly:

```bash
teledex init
```

When unpaired, `teledex` prints a pairing code locally. Send that code to your Telegram bot in a private chat to bind the chat.

Useful commands:

```bash
teledex unpair
teledex status
```
