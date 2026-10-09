"""Распознавание голосовых через Groq (OpenAI-совместимый endpoint)."""
from __future__ import annotations

import httpx

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


class Transcriber:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(60.0),
        )
        self._model = model

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        resp = await self._client.post(
            GROQ_URL,
            files={"file": (filename, audio, "audio/ogg")},
            data={"model": self._model, "language": "ru", "response_format": "json"},
        )
        resp.raise_for_status()
        return (resp.json().get("text") or "").strip()

    async def aclose(self) -> None:
        await self._client.aclose()
