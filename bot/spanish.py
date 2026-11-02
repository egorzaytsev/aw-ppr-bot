"""Карточки испанского — отдельный личный бот Егора (не рабочий), без Mac и без Claude.

Каждый день в 10:00 (Валенсия) — следующие 6 карточек: испанское и перевод под спойлером.
Новые карточки: Егор присылает боту текст урока — строки «испанское — перевод»
(разделитель: таб, два пробела, « — », « - », «=»). Заголовки «# дата», нумерация упражнений и строки
без перевода не берутся — бот перечисляет пропущенные. Старую очередь (queue.json) можно прислать файлом.
Команды: /more — ещё порция сейчас, /left — сколько осталось.

Очередь — один JSON в Dropbox /Spanish/cards.json: переживает перезапуски Railway, её можно открыть руками.
Пишет и принимает команды только от Егора; остальным не отвечает.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, time, timedelta
from html import escape

import httpx

from .slots import VALENCIA
from .tg_collector import _short_error

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
STORE = "/Spanish/cards.json"
DAILY_N = 6
SEND_AT = time(10, 0)
POLL_TIMEOUT = 50
RETRY_PAUSE = 30

_SEP = re.compile(r"\t+|\s{2,}|\s+[—–-]\s+|\s*=\s*")
_EXERCISE = re.compile(r"^\s*(\d+[.)]|[a-zа-я][.)])\s")
_HEADER = re.compile(r"^\s*#")


def parse_cards(text: str) -> tuple[list[dict], list[str]]:
    """Карточки из текста урока и строки, которые не похожи на пару «испанское — перевод»."""
    cards, skipped = [], []
    for raw in text.splitlines():
        line = raw.strip().strip("•·*").strip()
        if not line or _HEADER.match(line):
            continue
        if _EXERCISE.match(line):
            skipped.append(line)
            continue
        parts = [p.strip() for p in _SEP.split(line, maxsplit=1)]
        if len(parts) == 2 and all(parts):
            cards.append({"es": parts[0], "ru": parts[1]})
        else:
            skipped.append(line)
    return cards, skipped


def import_queue(data) -> tuple[list[dict], int]:
    """Старая очередь из GitHub (queue.json): карточки и сколько уже отправлено. Формат угадываем мягко."""
    sent = 0
    items = data
    if isinstance(data, dict):
        sent = int(data.get("sent_count") or data.get("sent") or 0)
        items = next((v for k, v in data.items() if isinstance(v, list) and k != "processed_sections"), [])
    cards = []
    for it in items:
        if isinstance(it, dict):
            es = it.get("es") or it.get("spanish") or it.get("front") or it.get("word") or ""
            ru = it.get("ru") or it.get("russian") or it.get("back") or it.get("translation") or ""
        elif isinstance(it, (list, tuple)) and len(it) >= 2:
            es, ru = it[0], it[1]
        else:
            continue
        if str(es).strip() and str(ru).strip():
            cards.append({"es": str(es).strip(), "ru": str(ru).strip()})
    return cards, min(sent, len(cards))


def card_text(cards: list[dict], start: int, total: int) -> str:
    lines = [f"🇪🇸 <b>Карточки {start + 1}–{start + len(cards)}</b> из {total}", ""]
    for c in cards:
        lines.append(f"<b>{escape(c['es'])}</b> — <tg-spoiler>{escape(c['ru'])}</tg-spoiler>")
    return "\n".join(lines)


def next_send(now: datetime) -> datetime:
    at = datetime.combine(now.date(), SEND_AT, VALENCIA)
    return at if now < at else at + timedelta(days=1)


class SpanishCards:
    def __init__(self, token: str, dropbox, owner_id: int) -> None:
        self._token = token
        self._dbx = dropbox
        self._owner = owner_id
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(POLL_TIMEOUT + 20))
        self._offset: int | None = None
        self._state: dict | None = None
        self._lock = asyncio.Lock()

    async def _call(self, method: str, **params) -> dict:
        resp = await self._client.post(f"{API}/bot{self._token}/{method}", json=params)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('error_code')} {data.get('description')}")
        return data["result"]

    async def _say(self, text: str) -> None:
        await self._call("sendMessage", chat_id=self._owner, text=text, parse_mode="HTML")

    # ---------- очередь ----------

    async def _load(self) -> dict:
        if self._state is None:
            try:
                self._state = json.loads(await self._dbx.download(STORE))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 409:   # 409 — файла ещё нет
                    raise
                self._state = {"cards": [], "sent": 0, "last_day": ""}
        return self._state

    async def _save(self) -> None:
        body = json.dumps(self._state, ensure_ascii=False, indent=1).encode("utf-8")
        await self._dbx.upload_overwrite(STORE, body)

    async def send_portion(self, day: str = "") -> int:
        async with self._lock:
            st = await self._load()
            start = st["sent"]
            portion = st["cards"][start:start + DAILY_N]
            if not portion:
                await self._say("🇪🇸 Карточки закончились — пришли новый урок.")
                if day:
                    st["last_day"] = day
                    await self._save()
                return 0
            await self._say(card_text(portion, start, len(st["cards"])))
            st["sent"] = start + len(portion)
            if day:
                st["last_day"] = day
            await self._save()
            return len(portion)

    # ---------- сообщения Егора ----------

    async def _on_message(self, msg: dict) -> None:
        if (msg.get("from") or {}).get("id") != self._owner or msg.get("chat", {}).get("type") != "private":
            return   # бот личный: чужим не отвечает
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if msg.get("document"):
            await self._import_file(msg["document"])
            return
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if cmd in ("/start", "/help"):
            await self._say("🇪🇸 Каждый день в 10:00 — 6 карточек, перевод под спойлером.\n"
                            "Пришли текст урока строками «испанское — перевод», я добавлю их в очередь.\n"
                            "/more — ещё порция сейчас, /left — сколько осталось.")
        elif cmd == "/more":
            await self.send_portion()
        elif cmd == "/left":
            async with self._lock:
                st = await self._load()
            await self._say(f"Отправлено {st['sent']} из {len(st['cards'])}, "
                            f"в очереди {len(st['cards']) - st['sent']}.")
        elif text and not cmd:
            await self._add_text(text)

    async def _add_text(self, text: str) -> None:
        cards, skipped = parse_cards(text)
        async with self._lock:
            st = await self._load()
            known = {c["es"].lower() for c in st["cards"]}
            new = [c for c in cards if c["es"].lower() not in known]
            st["cards"] += new
            if new:
                await self._save()
        reply = f"➕ Добавил {len(new)} карточек, в очереди {len(st['cards']) - st['sent']}."
        if len(cards) > len(new):
            reply += f"\nУже были: {len(cards) - len(new)}."
        if skipped:
            shown = "\n".join(f"• {escape(s[:80])}" for s in skipped[:10])
            more = f"\n…и ещё {len(skipped) - 10}" if len(skipped) > 10 else ""
            reply += f"\nНе понял (нет перевода или это упражнение):\n{shown}{more}"
        await self._say(reply)

    async def _import_file(self, doc: dict) -> None:
        if not (doc.get("file_name") or "").lower().endswith(".json"):
            await self._say("Файлом принимаю только старую очередь — queue.json.")
            return
        info = await self._call("getFile", file_id=doc["file_id"])
        resp = await self._client.get(f"{API}/file/bot{self._token}/{info['file_path']}")
        resp.raise_for_status()
        cards, sent = import_queue(json.loads(resp.content))
        if not cards:
            await self._say("В файле не нашёл карточек.")
            return
        async with self._lock:
            st = await self._load()
            had = len(st["cards"])
            known = {c["es"].lower() for c in cards}
            rest = [c for c in st["cards"] if c["es"].lower() not in known]
            st["cards"], st["sent"] = cards + rest, sent
            await self._save()
        await self._say(f"📥 Импорт: {len(cards)} карточек, из них уже отправлено {sent}. "
                        f"Своих сверху осталось {len(rest)} (было {had}).")

    # ---------- цикл ----------

    async def _poll_once(self) -> None:
        params = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message"]}
        if self._offset is not None:
            params["offset"] = self._offset
        for upd in await self._call("getUpdates", **params):
            self._offset = upd["update_id"] + 1
            if "message" in upd:
                try:
                    await self._on_message(upd["message"])
                except Exception as exc:
                    log.error("Карточки: сообщение не обработано: %s", _short_error(exc))  # без ссылок с токеном

    async def _daily(self) -> None:
        while True:
            now = datetime.now(VALENCIA)
            try:
                st = await self._load()
                today = now.date().isoformat()
                if now.time() >= SEND_AT and st.get("last_day") != today:
                    await self.send_portion(today)   # в том числе после перезапуска позже 10:00
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Карточки: ежедневная отправка не удалась")
                await asyncio.sleep(RETRY_PAUSE * 10)
                continue
            await asyncio.sleep(max(30.0, (next_send(datetime.now(VALENCIA)) - datetime.now(VALENCIA)).total_seconds()))

    async def run(self) -> None:
        log.info("Карточки испанского запущены")
        daily = asyncio.create_task(self._daily())
        try:
            while True:
                try:
                    await self._poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("Карточки: %s — пауза %s с", _short_error(exc), RETRY_PAUSE)
                    await asyncio.sleep(RETRY_PAUSE)
        finally:
            daily.cancel()

    async def aclose(self) -> None:
        await self._client.aclose()
