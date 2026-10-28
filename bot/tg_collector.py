"""Сбор Telegram для сводки CRM: всё, что получает @hareworldbot, — сразу в Буфер Airtable.

Работает на Railway всегда, поэтому сообщения не пропадают, пока Mac выключен
(Telegram хранит недоставленные боту сообщения только сутки). Без анализа:
проект и выводы дописывает вечерняя сводка Claude при разборе Буфера.
Голосовые расшифровываются через Groq, файлы до 5 МБ кладутся вложением.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from .airtable_ids import Buffer
from .voice import Transcriber

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
POLL_TIMEOUT = 50           # секунд long polling
RETRY_PAUSE = 30
MAX_ATTEMPTS = 3            # сообщение, которое не удаётся записать трижды, пропускаем с ошибкой
MAX_FILE_BYTES = 5 * 1024 * 1024
# Сообщения бизнес-аккаунта (Telegram Business) — тоже в Буфер
ALLOWED = ["message", "edited_message", "channel_post", "business_message", "edited_business_message"]
_MESSAGE_KEYS = ("message", "edited_message", "channel_post", "business_message", "edited_business_message")


def _who(user: dict | None) -> str:
    if not user:
        return ""
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p)
    return f"{name} (@{user['username']})" if user.get("username") else name or str(user.get("id", ""))


def _chat_name(chat: dict) -> str:
    return chat.get("title") or _who(chat) or str(chat.get("id"))


def _forward_note(msg: dict) -> str:
    origin = msg.get("forward_origin") or {}
    sender = origin.get("sender_user") or {}
    name = _who(sender) or origin.get("sender_user_name") or (origin.get("chat") or {}).get("title") \
        or (origin.get("sender_chat") or {}).get("title")
    return f"переслано от: {name}" if name else ""


def message_record(update_kind: str, msg: dict, transcript: str = "") -> dict:
    """Поля записи Буфера для одного сообщения (без файлов)."""
    chat = msg.get("chat") or {}
    text = (msg.get("text") or msg.get("caption") or "").strip()
    sender = _who(msg.get("from")) or _chat_name(chat)
    parts = [text] if text else []
    if transcript:
        parts.append(f"🎙 Расшифровка голосового: {transcript}")
    attachments = [k for k in ("document", "photo", "voice", "audio", "video", "video_note") if msg.get(k)]
    if attachments and not text and not transcript:
        parts.append(f"[{', '.join(attachments)}]")
    essence = "\n".join(parts) or "[пустое сообщение]"
    edited = update_kind.startswith("edited")
    ref = [f"чат: {_chat_name(chat)} (id {chat.get('id')})", f"от: {sender}", f"сообщение: {msg.get('message_id')}"]
    note = _forward_note(msg)
    if note:
        ref.append(note)
    if msg.get("business_connection_id"):
        ref.append("через Telegram Business")
    if edited:
        ref.append("исправленное сообщение")
    date = datetime.fromtimestamp(msg.get("edit_date") or msg.get("date") or 0, timezone.utc)
    title = f"Telegram · {sender}: {essence.splitlines()[0][:60]}"
    return {
        Buffer.WHAT: title,
        Buffer.SOURCE: "Telegram",
        Buffer.RECEIVED: date.isoformat(),
        Buffer.ESSENCE: essence,
        Buffer.SOURCE_REF: "\n".join(ref),
        Buffer.STATUS: "Новое",
        Buffer.KEY: f"tg:{chat.get('id')}:{msg.get('message_id')}" + (f":e{msg.get('edit_date')}" if edited else ""),
    }


class TelegramCollector:
    def __init__(self, token: str, repo, transcriber: Transcriber | None) -> None:
        self._token = token
        self._repo = repo
        self._transcriber = transcriber
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(POLL_TIMEOUT + 20))
        self._offset: int | None = None
        self._attempts: dict[int, int] = {}

    async def _call(self, method: str, **params) -> dict:
        resp = await self._client.post(f"{API}/bot{self._token}/{method}", json=params)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('error_code')} {data.get('description')}")
        return data["result"]

    async def _download(self, file_id: str) -> tuple[bytes, str] | None:
        info = await self._call("getFile", file_id=file_id)
        if (info.get("file_size") or 0) > MAX_FILE_BYTES or not info.get("file_path"):
            return None
        resp = await self._client.get(f"{API}/file/bot{self._token}/{info['file_path']}")
        resp.raise_for_status()
        return resp.content, info["file_path"].rsplit("/", 1)[-1]

    async def _handle(self, kind: str, msg: dict) -> None:
        chat = msg.get("chat") or {}
        key_base = f"tg:{chat.get('id')}:{msg.get('message_id')}"
        record_key = key_base + (f":e{msg.get('edit_date')}" if kind.startswith("edited") else "")
        if await asyncio.to_thread(self._repo.buffer_has, record_key):
            return  # уже записано (перезапуск до подтверждения offset)
        transcript = ""
        voice = msg.get("voice") or msg.get("audio")
        if voice and self._transcriber:
            try:
                got = await self._download(voice["file_id"])
                if got:
                    transcript = await self._transcriber.transcribe(got[0], got[1], language=None)
            except Exception:
                log.exception("Сбор Telegram: не удалось расшифровать голосовое %s", key_base)
        record_id = await asyncio.to_thread(self._repo.buffer_add, message_record(kind, msg, transcript))
        # Документы и фото (самое крупное из размеров) — вложением, если до 5 МБ
        files = []
        if msg.get("document"):
            files.append((msg["document"]["file_id"], msg["document"].get("file_name"), msg["document"].get("mime_type")))
        if msg.get("photo"):
            files.append((msg["photo"][-1]["file_id"], None, "image/jpeg"))
        for file_id, name, mime in files:
            try:
                got = await self._download(file_id)
                if got:
                    await asyncio.to_thread(self._repo.buffer_attach, record_id, name or got[1], got[0], mime)
            except Exception:
                log.exception("Сбор Telegram: не удалось приложить файл к %s", key_base)

    async def poll_once(self) -> int:
        params = {"timeout": POLL_TIMEOUT, "allowed_updates": ALLOWED}
        if self._offset is not None:
            params["offset"] = self._offset  # подтверждает всё до offset — после записи в Буфер
        updates = await self._call("getUpdates", **params)
        for upd in updates:
            kind = next((k for k in _MESSAGE_KEYS if k in upd), None)
            if kind:
                try:
                    await self._handle(kind, upd[kind])
                except Exception:
                    n = self._attempts[upd["update_id"]] = self._attempts.get(upd["update_id"], 0) + 1
                    if n < MAX_ATTEMPTS:
                        raise  # offset не двигаем — попробуем снова в следующем цикле
                    log.exception("Сбор Telegram: сообщение %s не записано в Буфер — пропущено", upd["update_id"])
            self._attempts.pop(upd["update_id"], None)
            self._offset = upd["update_id"] + 1
        return len(updates)

    async def run(self) -> None:
        log.info("Сбор Telegram (@hareworldbot) → Буфер запущен")
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 409 — кто-то ещё забирает сообщения этого бота (коннектор AnythingMCP); 401 — токен
                if "409" in str(exc):
                    log.warning("Сбор Telegram: %s — пауза %s с", exc, RETRY_PAUSE)
                else:
                    log.error("Сбор Telegram: %s — пауза %s с", exc, RETRY_PAUSE)  # ошибка — Егору
                await asyncio.sleep(RETRY_PAUSE)

    async def aclose(self) -> None:
        await self._client.aclose()
