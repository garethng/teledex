from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import File


logger = logging.getLogger(__name__)


class MediaPipeline:
    def __init__(self, backend: str, whisper_model: str):
        self.backend = backend
        self.whisper_model = whisper_model
        self._model = None

    async def save_telegram_file(self, tg_file: File, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        await tg_file.download_to_drive(custom_path=str(target))
        return target

    async def transcribe(self, audio_path: Path) -> str:
        if self.backend == "disabled":
            raise RuntimeError("Voice transcription is disabled in teledex config")
        if self.backend != "faster-whisper":
            raise RuntimeError(f"Unsupported transcriber backend: {self.backend}")
        return await asyncio.to_thread(self._transcribe_faster_whisper, audio_path)

    def _transcribe_faster_whisper(self, audio_path: Path) -> str:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed. Install with `pip install -e .[faster-whisper]`."
            ) from exc

        if self._model is None:
            self._model = WhisperModel(self.whisper_model)

        segments, _ = self._model.transcribe(str(audio_path))
        text = " ".join(segment.text.strip() for segment in segments if segment.text.strip())
        if not text:
            raise RuntimeError("Voice transcription returned empty text")
        return text
