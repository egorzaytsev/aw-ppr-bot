"""Карточки испанского — отдельный личный бот Егора (не рабочий), без Mac и без Claude.

Каждый день в 10:00 (Валенсия) — следующие 6 карточек: испанское и перевод под спойлером.
Новые карточки — сами из Google Doc с уроками (раз в 3 часа): документ делится на разделы по датам
(«28.10»), из строк, которых бот ещё не видел, LLM (Groq) делает пары «испанское — перевод»;
перевода нет — переводит сам. Упражнения, таблицы спряжений, ссылки и коды Zoom не берутся.
Строки помечаются виденными только после успешного разбора, поэтому урок, который дописывают
во время занятия, догоняется при следующей проверке.
Можно и вручную: текст боту — тот же разбор; старая очередь (queue.json) — файлом.
Команды: /more — ещё порция сейчас, /left — сколько осталось, /doc — проверить документ сейчас.

Очередь — один JSON в Dropbox /Spanish/cards.json: переживает перезапуски Railway, её можно открыть руками.
Пишет и принимает команды только от Егора; остальным не отвечает.
"""
from __future__ import annotations

import asyncio
import hashlib
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
DOC_URL = "https://docs.google.com/document/d/12cJa9dqg5_vOm_FF1KL5hg6Iw0KfP0koAZ3OIxhL1QA/export?format=txt"
DOC_EVERY = 3 * 3600
FIRST_RUN_SECTIONS = 2   # первый запуск: берём два последних урока, старые считаем отправленными раньше
LLM_URL = "https://api.groq.com/openai/v1/chat/completions"
LLM_CHUNK = 120

_SECTION = re.compile(r"^\s*#?\s*(\d{1,2}\.\d{2})\s*$")
_NOISE = re.compile(r"https?://|zoom|meet -|идентификатор|код доступа", re.I)

LLM_PROMPT = """Ты делаешь карточки для изучения испанского из конспекта урока (носитель — русский).
Карточка — пара: испанское слово, фраза, выражение или пример употребления — и короткий русский перевод.
Бери ВСЁ лексическое: слова, фразы, разговорные выражения, примеры. Если перевода в тексте нет — переведи сам.
Если строка — испанское определение термина («El inmovilismo es…»), карточка: термин — короткий русский смысл.
Варианты через «/» объедини в одну карточку. Испанскую часть сохраняй как в тексте, исправляя только явные опечатки.
НЕ бери: таблицы спряжений, нумерованные упражнения и вопросы к ним, строки только на русском, ссылки, коды, имена.
Ответ — только JSON: {"cards": [{"es": "...", "ru": "..."}]}. Нечего брать — {"cards": []}."""


def split_sections(text: str) -> list[tuple[str, list[str]]]:
    """[(ключ раздела, строки)]; ключ — дата и номер её повтора («28.10#2» на следующий год)."""
    sections: list[tuple[str, list[str]]] = []
    seen: dict[str, int] = {}
    for raw in text.replace("\r", "").split("\n"):
        m = _SECTION.match(raw.strip("\ufeff"))
        if m:
            n = seen[m.group(1)] = seen.get(m.group(1), 0) + 1
            sections.append((m.group(1) if n == 1 else f"{m.group(1)}#{n}", []))
        elif sections:
            line = raw.strip()
            if line and not _NOISE.search(line):
                sections[-1][1].append(line)
    return sections


def line_id(line: str) -> str:
    return hashlib.sha1(" ".join(line.lower().split()).encode()).hexdigest()[:10]

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
    def __init__(self, token: str, dropbox, owner_id: int, groq_key: str = "",
                 llm_model: str = "llama-3.3-70b-versatile") -> None:
        self._token = token
        self._groq_key = groq_key
        self._llm_model = llm_model
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

    # ---------- разбор текста ----------

    async def extract(self, lines: list[str]) -> list[dict]:
        """Карточки из строк конспекта через LLM; без ключа — простой разбор «испанское — перевод»."""
        if not self._groq_key:
            return parse_cards("\n".join(lines))[0]
        cards: list[dict] = []
        for i in range(0, len(lines), LLM_CHUNK):
            resp = await self._client.post(LLM_URL, headers={"Authorization": f"Bearer {self._groq_key}"}, json={
                "model": self._llm_model, "temperature": 0.1, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": LLM_PROMPT},
                             {"role": "user", "content": "\n".join(lines[i:i + LLM_CHUNK])}]})
            if resp.status_code != 200:
                raise RuntimeError(f"Groq {resp.status_code}: {resp.text[:200]}")
            data = json.loads(resp.json()["choices"][0]["message"]["content"])
            for c in data.get("cards", []):
                es, ru = str(c.get("es", "")).strip(), str(c.get("ru", "")).strip()
                if es and ru:
                    cards.append({"es": es, "ru": ru})
        return cards

    async def _add(self, cards: list[dict]) -> int:
        """Добавить в конец очереди без повторов; вызывать под self._lock."""
        st = await self._load()
        known = {c["es"].lower() for c in st["cards"]}
        new = []
        for c in cards:
            if c["es"].lower() not in known:
                known.add(c["es"].lower())
                new.append(c)
        st["cards"] += new
        return len(new)

    async def check_doc(self, report: bool = False) -> int:
        resp = await self._client.get(DOC_URL, follow_redirects=True)
        if resp.status_code != 200 or "text/plain" not in resp.headers.get("content-type", ""):
            raise RuntimeError(f"Google Doc недоступен ({resp.status_code}) — проверь доступ «по ссылке»")
        sections = split_sections(resp.content.decode("utf-8-sig", errors="replace"))
        added, notes = 0, []
        async with self._lock:
            st = await self._load()
            if "seen" not in st:   # первый запуск — старые уроки уже были в прежней очереди
                st["seen"] = {k: [line_id(x) for x in lines] for k, lines in sections[:-FIRST_RUN_SECTIONS]}
            for key, lines in sections:
                seen = set(st["seen"].get(key, []))
                fresh = [x for x in lines if line_id(x) not in seen]
                if not fresh:
                    continue
                n = await self._add(await self.extract(fresh))
                st["seen"][key] = sorted(seen | {line_id(x) for x in fresh})
                await self._save()   # после каждого раздела: сбой дальше не повторит уже разобранное
                added += n
                if n:
                    notes.append(f"{key.split('#')[0]} — {n}")
            left = len(st["cards"]) - st["sent"]
        if notes or report:
            what = ", ".join(notes) if notes else "новых строк нет"
            await self._say(f"📒 Из документа: {what}. В очереди {left}.")
        return added

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
                            "Новые слова беру сам из документа с уроками (раз в 3 часа).\n"
                            "/more — ещё порция сейчас, /left — сколько осталось, /doc — проверить документ сейчас.")
        elif cmd == "/more":
            await self.send_portion()
        elif cmd == "/doc":
            try:
                await self.check_doc(report=True)
            except Exception as exc:
                await self._say(f"⚠️ Документ не разобран: {escape(_short_error(exc))}")
        elif cmd == "/left":
            async with self._lock:
                st = await self._load()
            await self._say(f"Отправлено {st['sent']} из {len(st['cards'])}, "
                            f"в очереди {len(st['cards']) - st['sent']}.")
        elif text and not cmd:
            await self._add_text(text)

    async def _add_text(self, text: str) -> None:
        cards = await self.extract([x.strip() for x in text.splitlines() if x.strip()])
        async with self._lock:
            new = await self._add(cards)
            if new:
                await self._save()
            st = await self._load()
        reply = f"➕ Добавил {new} карточек, в очереди {len(st['cards']) - st['sent']}."
        if len(cards) > new:
            reply += f"\nУже были: {len(cards) - new}."
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

    async def _doc_loop(self) -> None:
        last_error = ""
        while True:
            try:
                await self.check_doc()
                last_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                err = _short_error(exc)
                log.warning("Карточки: документ не разобран: %s", err)
                if err != last_error:   # одна и та же ошибка — одно сообщение, не каждые 3 часа
                    last_error = err
                    try:
                        await self._say(f"⚠️ Документ с уроками не разобран: {escape(err)}")
                    except Exception:
                        pass
            await asyncio.sleep(DOC_EVERY)

    async def run(self) -> None:
        log.info("Карточки испанского запущены")
        daily = asyncio.create_task(self._daily())
        doc = asyncio.create_task(self._doc_loop())
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
            doc.cancel()

    async def aclose(self) -> None:
        await self._client.aclose()
