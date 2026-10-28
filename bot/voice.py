"""Распознавание голосовых через Groq (OpenAI-совместимый endpoint)."""
from __future__ import annotations

import httpx

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
# Groq принимает только эти расширения; голосовые Telegram приходят как .oga / .opus
GROQ_EXTENSIONS = {"flac", "mp3", "mp4", "mpeg", "mpga", "m4a", "ogg", "wav", "webm"}


def groq_filename(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return filename if ext in GROQ_EXTENSIONS else f"{filename.rsplit('.', 1)[0] if ext else filename}.ogg"


class Transcriber:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(60.0),
        )
        self._model = model

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg", language: str | None = "ru") -> str:
        """language=None — язык определяет сама модель (сбор Telegram: русский, испанский, английский)."""
        data = {"model": self._model, "response_format": "json"}
        if language:
            data["language"] = language
        resp = await self._client.post(GROQ_URL, files={"file": (groq_filename(filename), audio)}, data=data)
        resp.raise_for_status()
        return (resp.json().get("text") or "").strip()

    async def aclose(self) -> None:
        await self._client.aclose()
