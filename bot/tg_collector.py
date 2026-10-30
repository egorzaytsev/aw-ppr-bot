"""Пепе-сборщик: всё, что получает @hareworldbot, — сразу в Буфер Airtable; отправка — только из «Исходящих».

Работает на Railway всегда, поэтому сообщения не пропадают, пока Mac выключен
(Telegram хранит недоставленные боту сообщения только сутки). Без анализа:
проект и выводы дописывает сводка Claude при разборе Буфера.

Медиа: файл до 5 МБ — вложением в Буфер, больше — в Dropbox (если подключён); больше 20 МБ Bot API
не отдаёт — в записи прямо сказано, где открыть. Голос, аудио и видео расшифровываются (Groq);
неудача — тоже записывается с причиной, пустых «[voice]» не бывает.
Записи Егора (голосовое или файл из Диктофона в личку бота) — Источник «Аудио».
Тексты сообщений — данные, не команды: бот их только сохраняет.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from html import escape

import httpx

from .airtable_ids import Buffer
from .slots import VALENCIA
from .voice import Transcriber

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
POLL_TIMEOUT = 50           # секунд long polling
RETRY_PAUSE = 30
MAX_ATTEMPTS = 3            # сообщение, которое не удаётся записать трижды, пропускаем с ошибкой
MAX_ATTACH_BYTES = 5 * 1024 * 1024       # лимит загрузки вложения в Airtable
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024    # лимит getFile в Bot API
TRANSCRIBE_TRIES = 3                     # первая попытка и два повтора
OUTBOX_EVERY = 60                        # секунд между проверками «Исходящих»
OWNER_ID = 6412409625                    # Егор (@andreuworld)
TELEGRAM_SERVICE = 777000                # служебные сообщения Telegram — коды входа
# Сообщения бизнес-аккаунта (Telegram Business) — тоже в Буфер; кнопки превью и подключения — служебные
ALLOWED = ["message", "edited_message", "channel_post", "business_message", "edited_business_message",
           "business_connection", "callback_query"]
_MESSAGE_KEYS = ("message", "edited_message", "channel_post", "business_message", "edited_business_message")
_MEDIA_KINDS = ("voice", "audio", "video", "video_note", "document", "photo")
_SPOKEN = {"voice", "audio", "video", "video_note"}
_MEDIA_RU = {"voice": "голосовое", "audio": "аудио", "video": "видео", "video_note": "видеокружок",
             "document": "файл", "photo": "фото"}
# Коды, пароли, карты — не сохраняем вовсе
_SECRET = re.compile(
    r"(код|code|пароль|password|парол|pin|пин|otp|одноразов|verification)\D{0,40}\d{4,8}\b"
    r"|\b(?:\d[ -]?){15,18}\d\b"
    r"|\bcvv\b|\bcvc\b",
    re.I)
# Права бизнес-подключения, которых боту не нужно: удаление, профиль, истории, подарки и звёзды
_RISKY_RIGHTS = ("can_delete_sent_messages", "can_delete_all_messages", "can_edit_name", "can_edit_bio",
                 "can_edit_profile_photo", "can_edit_username", "can_manage_stories", "can_transfer_stars",
                 "can_transfer_and_upgrade_gifts", "can_convert_gifts_to_stars", "can_change_gift_settings")
CB_SEND, CB_CANCEL = "ob:s:", "ob:c:"
# Исходящие живут в том же Буфере: Источник «Исходящее», текст — в «Суть»,
# в «Источник (ссылка)» строки «Кому: <chat_id>», «От кого: Бот | Егор (business)», «parse_mode: HTML»
OUT_SOURCE = "Исходящее"
OUT_DRAFT, OUT_WAITING, OUT_SENT, OUT_CANCELLED, OUT_ERROR = (
    "Черновик", "Ждёт Егора", "Отправлено", "Отменено", "Ошибка")
FROM_OWNER = "Егор (business)"


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


def _mb(size: int | None) -> str:
    return f"{(size or 0) / 1024 / 1024:.1f} МБ".replace(".", ",")


def is_secret(msg: dict) -> bool:
    """Код входа, пароль, номер карты — такое сообщение не сохраняется."""
    if (msg.get("from") or {}).get("id") == TELEGRAM_SERVICE:
        return True
    return bool(_SECRET.search(msg.get("text") or msg.get("caption") or ""))


def media_of(msg: dict) -> dict | None:
    """Медиа сообщения: вид, file_id, размер, имя, длительность (у фото — самый крупный размер)."""
    for kind in _MEDIA_KINDS:
        item = msg.get(kind)
        if not item:
            continue
        if kind == "photo":
            item = item[-1]
        mime = item.get("mime_type") or ("image/jpeg" if kind == "photo" else "")
        spoken = kind in _SPOKEN or (kind == "document" and mime.startswith(("audio/", "video/")))
        return {"kind": kind, "file_id": item.get("file_id"), "unique": item.get("file_unique_id"),
                "size": item.get("file_size") or 0, "name": item.get("file_name"), "mime": mime,
                "duration": item.get("duration"), "spoken": spoken}
    return None


def content_key(kind: str, msg: dict) -> str:
    """Ключ по содержанию: переписка Егора с Лолой приходит через оба бизнес-подключения — запись одна."""
    sender = (msg.get("from") or {}).get("id") or (msg.get("chat") or {}).get("id")
    media = media_of(msg) or {}
    body = (msg.get("text") or msg.get("caption") or "") + "|" + (media.get("unique") or "")
    digest = hashlib.sha1(body.encode()).hexdigest()[:12]
    key = f"tg:{sender}:{msg.get('date')}:{digest}"
    return key + (f":e{msg.get('edit_date')}" if kind.startswith("edited") else "")


def is_owner_recording(msg: dict, owner_id: int) -> bool:
    """Егор прислал боту в личку голосовое или запись (Диктофон → Поделиться)."""
    chat, media = msg.get("chat") or {}, media_of(msg)
    return (chat.get("type") == "private" and not msg.get("business_connection_id")
            and (msg.get("from") or {}).get("id") == owner_id and bool(media) and media["spoken"])


def message_record(update_kind: str, msg: dict, transcript: str = "", notes: list[str] | None = None,
                   owner_recording: bool = False) -> dict:
    """Поля записи Буфера для одного сообщения (файл прикладывается отдельно)."""
    chat = msg.get("chat") or {}
    text = (msg.get("text") or msg.get("caption") or "").strip()
    sender = _who(msg.get("from")) or _chat_name(chat)
    media = media_of(msg)
    parts = [text] if text else []            # подпись Егора к записи — в начало
    if media and media["kind"] in ("document", "audio") and media.get("name"):
        parts.append(f"📎 {media['name']} · {_mb(media['size'])}")
    elif media and not transcript and not notes:
        parts.append(f"📎 {_MEDIA_RU[media['kind']]} · {_mb(media['size'])}")
    if transcript:
        parts.append(f"🎙 Расшифровка: {transcript}")
    parts += list(notes or [])
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
    if media:
        ref.append(f"file_id: {media['file_id']}")
        ref.append(f"file_unique_id: {media['unique']}")
    date = datetime.fromtimestamp(msg.get("edit_date") or msg.get("date") or 0, timezone.utc)
    if owner_recording:
        first = (transcript or text or media.get("name") or "запись").split("\n")[0]
        title = f"Аудио · {date.astimezone(VALENCIA):%d.%m %H:%M} · {' '.join(first.split()[:8])[:60]}"
    else:
        title = f"Telegram · {sender}: {essence.splitlines()[0][:60]}"
    return {
        Buffer.WHAT: title,
        Buffer.SOURCE: "Аудио" if owner_recording else "Telegram",
        Buffer.RECEIVED: date.isoformat(),
        Buffer.ESSENCE: essence,
        Buffer.SOURCE_REF: "\n".join(ref),
        Buffer.STATUS: "Новое",
        Buffer.KEY: content_key(update_kind, msg),
    }


def outbox_target(fields: dict) -> dict:
    """Кому, от кого и разметка — из строк «Источник (ссылка)» исходящего."""
    out = {"to": "", "from": "Бот", "html": False}
    for line in str(fields.get(Buffer.SOURCE_REF) or "").splitlines():
        name, _, value = line.partition(":")
        name, value = name.strip().lower(), value.strip()
        if name == "кому":
            out["to"] = value.split()[0] if value else ""
        elif name == "от кого":
            out["from"] = value
        elif name == "parse_mode":
            out["html"] = value.upper() == "HTML"
    return out


def outbox_preview(rec: dict) -> str:
    f = rec["fields"]
    t = outbox_target(f)
    return (f"📤 <b>{escape(f.get(Buffer.WHAT) or 'Черновик сообщения')}</b>\n"
            f"Кому: {escape(t['to'] or '—')} · от кого: {escape(t['from'])}\n\n{escape(f.get(Buffer.ESSENCE) or '')}")


class TelegramCollector:
    def __init__(self, token: str, repo, transcriber: Transcriber | None, dropbox=None,
                 owner_id: int = OWNER_ID) -> None:
        self._token = token
        self._repo = repo
        self._transcriber = transcriber
        self._dropbox = dropbox
        self._owner = owner_id
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(POLL_TIMEOUT + 20))
        self._offset: int | None = None
        self._attempts: dict[int, int] = {}
        self._connections: dict[str, dict] = {}   # business_connection_id → {user_id, risky}
        self._owner_connection: str | None = None
        self._last_outbox = 0.0
        self._pause = asyncio.sleep   # пауза между попытками расшифровки (в тестах — без ожидания)

    async def _call(self, method: str, **params) -> dict:
        resp = await self._client.post(f"{API}/bot{self._token}/{method}", json=params)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('error_code')} {data.get('description')}")
        return data["result"]

    async def _notify_owner(self, text: str, markup: dict | None = None) -> dict | None:
        params = {"chat_id": self._owner, "text": text, "parse_mode": "HTML"}
        if markup:
            params["reply_markup"] = markup
        try:
            return await self._call("sendMessage", **params)
        except Exception:
            log.exception("Сбор Telegram: не удалось написать Егору")
            return None

    # ---------- бизнес-подключения ----------

    async def _learn_connection(self, conn: dict) -> None:
        cid = conn.get("id")
        if not cid or cid in self._connections:
            return
        user_id = (conn.get("user") or {}).get("id")
        rights = conn.get("rights") or {}
        risky = [r for r in _RISKY_RIGHTS if rights.get(r)]
        self._connections[cid] = {"user": user_id, "risky": risky}
        if user_id == self._owner:
            self._owner_connection = cid
        if risky:
            await self._notify_owner(
                "⚠️ У бота в Telegram Business есть лишние права: " + ", ".join(risky)
                + f" (подключение {escape(_who(conn.get('user')))}). Боту нужно только читать и отвечать — "
                "сними эти права в Настройки → Telegram для бизнеса → Чат-боты.")

    async def _connection_of(self, msg: dict) -> None:
        cid = msg.get("business_connection_id")
        if cid and cid not in self._connections:
            try:
                await self._learn_connection(await self._call("getBusinessConnection", business_connection_id=cid))
            except Exception:
                log.warning("Сбор Telegram: не удалось прочитать подключение %s", cid)

    # ---------- медиа ----------

    async def _download(self, file_id: str) -> tuple[bytes, str]:
        info = await self._call("getFile", file_id=file_id)
        if not info.get("file_path"):
            raise RuntimeError("Telegram не дал путь к файлу")
        resp = await self._client.get(f"{API}/file/bot{self._token}/{info['file_path']}")
        resp.raise_for_status()
        return resp.content, info["file_path"].rsplit("/", 1)[-1]

    async def _transcribe(self, content: bytes, filename: str) -> tuple[str, str]:
        """(текст, причина неудачи) — три попытки."""
        if not self._transcriber:
            return "", "распознавание не настроено"
        reason = ""
        for attempt in range(TRANSCRIBE_TRIES):
            try:
                text = await self._transcriber.transcribe(content, filename, language=None)
                if text:
                    return text, ""
                reason = "пустой ответ распознавания"
            except Exception as exc:
                reason = _short_error(exc)
            if attempt < TRANSCRIBE_TRIES - 1:
                await self._pause(2 * (attempt + 1))
        return "", reason

    async def _store_big(self, content: bytes, name: str, date: datetime) -> str:
        """Файл > 5 МБ — в Dropbox; путь или причина, почему не сохранён."""
        if not self._dropbox:
            return f"не сохранён: {_mb(len(content))}, больше 5 МБ, а Dropbox не подключён"
        try:
            path = await self._dropbox.upload(f"/ecoCRM/telegram/{date:%Y-%m-%d}/{name}", content)
            return f"Dropbox {path}"
        except Exception as exc:
            return f"не сохранён в Dropbox: {_short_error(exc)}"

    # ---------- сообщения ----------

    async def _handle(self, kind: str, msg: dict) -> None:
        if is_secret(msg):
            log.info("Сбор Telegram: сообщение с кодом или реквизитами не сохранено")
            return
        await self._connection_of(msg)
        key = content_key(kind, msg)
        if await asyncio.to_thread(self._repo.buffer_has, key):
            return  # уже записано: перезапуск или та же переписка через второе подключение
        chat = msg.get("chat") or {}
        where_tg = f"чат {chat.get('id')}, сообщение {msg.get('message_id')}"
        media = media_of(msg)
        notes: list[str] = []
        transcript = ""
        content = None
        attach_name = ""
        where = ""
        if media:
            if media["size"] > MAX_DOWNLOAD_BYTES:
                notes.append(f"⚠ Файл > 20 МБ ({_mb(media['size'])}), не скачан — открыть в Telegram: {where_tg}")
            else:
                try:
                    content, tg_name = await self._download(media["file_id"])
                    attach_name = media.get("name") or tg_name
                except Exception as exc:
                    notes.append(f"⚠ Файл не скачан: {_short_error(exc)} — открыть в Telegram: {where_tg}")
            if content is not None:
                date = datetime.fromtimestamp(msg.get("date") or 0, timezone.utc)
                if len(content) <= MAX_ATTACH_BYTES:
                    where = "во вложениях"
                else:
                    where = await self._store_big(content, attach_name, date)
                    if where.startswith("не сохранён"):
                        notes.append(f"⚠ Файл {where}. Открыть в Telegram: {where_tg}")
                    else:
                        notes.append(f"📁 {where}")
                if media["spoken"]:
                    transcript, reason = await self._transcribe(content, attach_name)
                    if not transcript:
                        notes.append(f"🎙 Расшифровка не удалась: {reason}; длительность "
                                     f"{media.get('duration') or '?'} с; файл: {where}")
            elif media["spoken"] and media["size"] <= MAX_DOWNLOAD_BYTES:
                notes.append(f"🎙 Расшифровка не удалась: файл не скачан; длительность {media.get('duration') or '?'} с")
        owner_rec = is_owner_recording(msg, self._owner)
        record_id = await asyncio.to_thread(
            self._repo.buffer_add, message_record(kind, msg, transcript, notes, owner_rec))
        if content is not None and len(content) <= MAX_ATTACH_BYTES:
            try:
                await asyncio.to_thread(self._repo.buffer_attach, record_id, attach_name, content,
                                        media.get("mime") or None)
            except Exception as exc:
                log.exception("Сбор Telegram: не удалось приложить файл к %s", key)
                fields = message_record(kind, msg, transcript,
                                        notes + [f"⚠ Файл не приложен: {_short_error(exc)} — открыть в Telegram: "
                                                 f"{where_tg}"], owner_rec)
                await asyncio.to_thread(self._repo.buffer_update, record_id, {Buffer.ESSENCE: fields[Buffer.ESSENCE]})

    # ---------- исходящие ----------

    async def check_outbox(self) -> None:
        """Черновики исходящих в Буфере → превью Егору с кнопками; статус «Ждёт Егора»."""
        for rec in await asyncio.to_thread(self._repo.outbox_drafts):
            markup = {"inline_keyboard": [[{"text": "✅ Отправить", "callback_data": f"{CB_SEND}{rec['id']}"},
                                           {"text": "✖️ Отменить", "callback_data": f"{CB_CANCEL}{rec['id']}"}]]}
            if await self._notify_owner(outbox_preview(rec), markup):
                await asyncio.to_thread(self._repo.buffer_update, rec["id"], {Buffer.STATUS: OUT_WAITING})

    async def _on_callback(self, cq: dict) -> None:
        data = cq.get("data") or ""
        try:
            await self._call("answerCallbackQuery", callback_query_id=cq["id"])
        except Exception:
            pass
        if (cq.get("from") or {}).get("id") != self._owner or not data.startswith((CB_SEND, CB_CANCEL)):
            return  # кнопки отправки — только Егору
        rec_id = data[len(CB_SEND):]
        rec = await asyncio.to_thread(self._repo.buffer_get, rec_id)
        f = rec.get("fields", {}) if rec else {}
        if f.get(Buffer.SOURCE) != OUT_SOURCE or f.get(Buffer.STATUS) != OUT_WAITING:
            await self._notify_owner(f"ℹ️ Уже обработано: {escape(f.get(Buffer.STATUS) or 'запись не найдена')}.")
            return
        if data.startswith(CB_CANCEL):
            await asyncio.to_thread(self._repo.buffer_update, rec_id, {Buffer.STATUS: OUT_CANCELLED})
            await self._notify_owner("✖️ Отменено, не отправлял.")
            return
        await self._send_outbox(rec_id, f)

    async def _send_outbox(self, rec_id: str, f: dict) -> None:
        t = outbox_target(f)
        text = f.get(Buffer.ESSENCE) or ""
        params = {"chat_id": t["to"], "text": text}
        if t["html"]:
            params["parse_mode"] = "HTML"
        error = ""
        if t["from"] == FROM_OWNER:
            if self._owner_connection:
                params["business_connection_id"] = self._owner_connection
            else:
                error = "бот ещё не видел бизнес-подключение Егора — после первого сообщения в бизнес-чате повтори"
        if not t["to"]:
            error = "не указан chat_id получателя (строка «Кому: …»)"
        sent = None
        if not error:
            try:
                sent = await self._call("sendMessage", **params)
            except Exception as exc:
                error = _short_error(exc)
                if any(w in error.lower() for w in ("business", "peer", "chat not found", "forbidden")):
                    error += " (чат вне 24-часового окна Telegram Business или бот его не видел)"
        now = datetime.now(timezone.utc)
        if sent:
            await asyncio.to_thread(self._repo.buffer_update, rec_id, {
                Buffer.STATUS: OUT_SENT,
                Buffer.ANALYSIS: f"Отправлено {now.astimezone(VALENCIA):%d.%m.%Y %H:%M}, message_id {sent.get('message_id')}"})
            await self._notify_owner(f"✅ Отправлено: {escape(f.get(Buffer.WHAT) or t['to'])}.")
        else:
            await asyncio.to_thread(self._repo.buffer_update, rec_id,
                                    {Buffer.STATUS: OUT_ERROR, Buffer.ANALYSIS: f"Ошибка отправки: {error}"})
            await self._notify_owner(f"⚠️ Не отправилось: {escape(error)}\nТекст для ручной отправки:")
            await self._notify_owner(escape(text))

    # ---------- цикл ----------

    async def poll_once(self) -> int:
        params = {"timeout": POLL_TIMEOUT, "allowed_updates": ALLOWED}
        if self._offset is not None:
            params["offset"] = self._offset  # подтверждает всё до offset — после записи в Буфер
        updates = await self._call("getUpdates", **params)
        for upd in updates:
            kind = next((k for k in _MESSAGE_KEYS if k in upd), None)
            try:
                if kind:
                    await self._handle(kind, upd[kind])
                elif "business_connection" in upd:
                    self._connections.pop(upd["business_connection"].get("id"), None)  # права могли смениться
                    await self._learn_connection(upd["business_connection"])
                elif "callback_query" in upd:
                    await self._on_callback(upd["callback_query"])
            except Exception:
                n = self._attempts[upd["update_id"]] = self._attempts.get(upd["update_id"], 0) + 1
                if n < MAX_ATTEMPTS:
                    raise  # offset не двигаем — попробуем снова в следующем цикле
                log.exception("Сбор Telegram: обновление %s не обработано — пропущено", upd["update_id"])
            self._attempts.pop(upd["update_id"], None)
            self._offset = upd["update_id"] + 1
        return len(updates)

    async def run(self) -> None:
        log.info("Сбор Telegram (@hareworldbot) → Буфер запущен")
        loop = asyncio.get_running_loop()
        while True:
            try:
                await self.poll_once()
                if loop.time() - self._last_outbox >= OUTBOX_EVERY:
                    self._last_outbox = loop.time()
                    await self.check_outbox()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 409 — кто-то ещё забирает сообщения этого бота; 401 — токен
                if "409" in str(exc):
                    log.warning("Сбор Telegram: %s — пауза %s с", exc, RETRY_PAUSE)
                else:
                    log.error("Сбор Telegram: %s — пауза %s с", exc, RETRY_PAUSE)  # ошибка — Егору
                await asyncio.sleep(RETRY_PAUSE)

    async def aclose(self) -> None:
        await self._client.aclose()


def _short_error(exc: Exception) -> str:
    """Причина без ссылок с токеном (в httpx-ошибках бывает адрес api.telegram.org/file/bot…)."""
    text = str(exc).split("\n")[0]
    text = re.sub(r"https?://\S+", "", text)
    return (f"{type(exc).__name__}: {text}".strip(": "))[:200]
