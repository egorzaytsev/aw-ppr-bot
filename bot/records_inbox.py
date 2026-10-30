"""Записи Егора (встречи, звонки) из Dropbox /ecoCRM/records/inbox — без Mac.

Раз в 15 минут: аудио из inbox режется на куски по 10 минут, расшифровывается (Groq),
текст кладётся рядом в processed (.txt), аудио переносится туда же, в Буфер — запись «Аудио».
Не вышло трижды — запись в Буфер с причиной, файл — в /ecoCRM/records/failed.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .airtable_ids import Buffer
from .slots import VALENCIA
from .audio_split import split_audio

log = logging.getLogger(__name__)

INBOX = "/ecoCRM/records/inbox"
PROCESSED = "/ecoCRM/records/processed"
FAILED = "/ecoCRM/records/failed"
EVERY = 15 * 60
MAX_TRIES = 3
AUDIO_EXT = (".m4a", ".mp3", ".wav", ".ogg", ".oga", ".opus", ".aac", ".flac", ".webm", ".mp4", ".mov", ".amr")


def record_fields(name: str, path: str, when: datetime, transcript: str, error: str = "") -> dict:
    first = " ".join(transcript.split()[:8])[:60] if transcript else name
    essence = transcript or f"🎙 Расшифровка не удалась: {error}; файл: Dropbox {path}"
    return {
        Buffer.WHAT: f"Аудио · {when.astimezone(VALENCIA):%d.%m %H:%M} · {first}",
        Buffer.SOURCE: "Аудио",
        Buffer.RECEIVED: when.isoformat(),
        Buffer.ESSENCE: essence,
        Buffer.SOURCE_REF: f"Dropbox {path}",
        Buffer.STATUS: "Новое",
        Buffer.KEY: f"dbx:{name}:{when:%Y%m%d%H%M%S}",
    }


class RecordsInbox:
    def __init__(self, dropbox, transcriber, repo) -> None:
        self._dbx = dropbox
        self._transcriber = transcriber
        self._repo = repo
        self._tries: dict[str, int] = {}

    async def _transcribe(self, content: bytes, name: str) -> str:
        suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ".m4a"
        texts = []
        for part_name, part in await split_audio(content, suffix):
            texts.append(await self._transcriber.transcribe(part, part_name, language=None))
        return "\n".join(t for t in texts if t).strip()

    async def _one(self, entry: dict) -> None:
        name, path = entry["name"], entry["path_display"]
        when = datetime.fromisoformat(entry.get("client_modified", "").replace("Z", "+00:00")) \
            if entry.get("client_modified") else datetime.now(timezone.utc)
        fields = record_fields(name, path, when, "")
        if await asyncio.to_thread(self._repo.buffer_has, fields[Buffer.KEY]):
            await self._dbx.move(path, f"{PROCESSED}/{name}")  # уже записано раньше — только убрать из inbox
            return
        try:
            content = await self._dbx.download(path)
            transcript = await self._transcribe(content, name)
            if not transcript:
                raise RuntimeError("пустая расшифровка")
        except Exception as exc:
            n = self._tries[path] = self._tries.get(path, 0) + 1
            if n < MAX_TRIES:
                log.warning("Записи Dropbox: %s — попытка %s не удалась: %s", name, n, exc)
                return
            moved = await self._dbx.move(path, f"{FAILED}/{name}")
            reason = str(exc).split("\n")[0][:200]
            await asyncio.to_thread(self._repo.buffer_add, record_fields(name, moved, when, "", reason))
            log.error("Записи Dropbox: %s не расшифрована: %s", name, reason)
            self._tries.pop(path, None)
            return
        moved = await self._dbx.move(path, f"{PROCESSED}/{name}")
        base = moved.rsplit(".", 1)[0]
        await self._dbx.upload(f"{base}.txt", transcript.encode("utf-8"))
        await asyncio.to_thread(self._repo.buffer_add, record_fields(name, moved, when, transcript))
        self._tries.pop(path, None)

    async def check_once(self) -> int:
        files = [e for e in await self._dbx.list_files(INBOX) if e["name"].lower().endswith(AUDIO_EXT)]
        for entry in files:
            await self._one(entry)
        return len(files)

    async def run(self) -> None:
        log.info("Записи Dropbox (%s) → Буфер запущены", INBOX)
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Записи Dropbox: ошибка проверки папки")
            await asyncio.sleep(EVERY)
