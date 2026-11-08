"""Ошибки бота — проверяющему по-русски (пачкой раз в минуту), плюс сводка каждое утро.

Правило 1.9 задания от 08.11:
- сбои, которые бот пережил сам (тайм-аут сети, повтор удался, пауза сбора), не присылаем — только счётчик
  в утренней сводке;
- остальное — по-русски: что случилось, кого касается, что сделать; технические подробности — только в логах
  Railway. Текст для проверяющего передаётся в лог через extra={"alert": "…", "reviewer": "lola"};
- одинаковая ошибка не теряется: раз в час — «Ошибка повторяется: …, N раз за час».
"""
from __future__ import annotations

import logging
import re
from collections import deque
from datetime import datetime, timedelta
from html import escape

from .slots import VALENCIA

SUMMARY_HOUR = 9        # сводка — в 9 утра по Валенсии
MAX_LINES = 5           # в одном сообщении об ошибках
REPEAT_AFTER = timedelta(hours=1)  # одинаковую ошибку присылаем не чаще раза в час
_IGNORED = ("telegram.ext.Updater",)  # сетевые сбои опроса Telegram — не ошибки бота
# Модули, которые сами повторяют работу (сбор Telegram, записи Dropbox, карточки испанского)
_SELF_HEALING = ("bot.tg_collector", "bot.records_inbox", "bot.spanish", "bot.dropbox_store")
# Временные сетевые сбои: бот повторит сам
_TRANSIENT = ("TimedOut", "NetworkError", "ReadTimeout", "ConnectTimeout", "ConnectError", "RemoteProtocolError",
              "ReadError", "WriteTimeout", "PoolTimeout", "RetryAfter", "ConnectionError", "Timeout", "TimeoutError")
_IDS = re.compile(r"\b(rec|tbl|fld|app)[A-Za-z0-9]{14}\b|\b-?\d{6,}\b")
DEFAULT_HINT = "Если повторится — передай Claude, подробности в логах Railway."


def plain(text: str) -> str:
    """Без ID записей и чатов — их проверяющему не показываем."""
    return re.sub(r"\s{2,}", " ", _IDS.sub("", text)).strip(" :—-")


def survived(record: logging.LogRecord) -> bool:
    if getattr(record, "alert", None):
        return False
    if record.name.startswith(_SELF_HEALING):
        return True
    exc = record.exc_info[1] if record.exc_info else None
    return exc is not None and type(exc).__name__ in _TRANSIENT


class AlertHandler(logging.Handler):
    """Копит ERROR-записи логов; отправляет их цикл в main."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.queue: deque = deque(maxlen=200)
        self.day_count = 0
        self.survived_count = 0
        self._sent: dict[tuple, datetime] = {}      # (кому, текст) → когда последний раз присылали
        self._repeats: dict[tuple, int] = {}        # (кому, текст) → сколько раз повторилась после отправки

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(_IGNORED):
            return
        try:
            if survived(record):
                self.survived_count += 1
                return
            text = getattr(record, "alert", None)
            if not text:
                text = f"Сбой: {plain(record.getMessage())}. {DEFAULT_HINT}"
            who = getattr(record, "reviewer", None) or "egor"
            self.queue.append((datetime.now(VALENCIA), who, text[:400]))
            self.day_count += 1
        except Exception:
            pass

    def drain(self, now: datetime | None = None) -> dict:
        """{кому: сообщение} о новых ошибках (очередь очищается); пустой dict — присылать нечего."""
        now = now or datetime.now(VALENCIA)
        items = list(self.queue)
        self.queue.clear()
        grouped: dict[tuple, list] = {}
        for at, who, text in items:
            grouped.setdefault((who, text), []).append(at)
        lines: dict[str, list[str]] = {}
        for key, times in grouped.items():
            if key in self._sent and now - self._sent[key] < REPEAT_AFTER:
                self._repeats[key] = self._repeats.get(key, 0) + len(times)
                continue
            self._sent[key] = now
            n = len(times) + self._repeats.pop(key, 0)
            lines.setdefault(key[0], []).append(
                f"• {times[0]:%H:%M} {escape(key[1])}" + (f" (×{n})" if n > 1 else ""))
        # Повторы, которые за час так и не прислали
        for key, n in list(self._repeats.items()):
            if now - self._sent.get(key, now) >= REPEAT_AFTER:
                self._repeats.pop(key)
                self._sent[key] = now
                lines.setdefault(key[0], []).append(f"• Ошибка повторяется: {escape(key[1])} — {n} раз за час")
        out = {}
        for who, rows in lines.items():
            text = [f"🛠 ⚠️ <b>Пепе: ошибки ({len(rows)})</b>", *rows[:MAX_LINES]]
            if len(rows) > MAX_LINES:
                text.append(f"• и ещё {len(rows) - MAX_LINES} — подробности в логах Railway")
            out[who] = "\n".join(text)
        return out


def summary_text(day: datetime, errors: int, stats: dict, overdue: int, unknown: int,
                 survived_count: int = 0) -> str:
    lines = [f"🛠 <b>Сводка Пепе · {day:%d.%m.%Y}</b>",
             f"• Заявок без решения: {stats['no_decision']}" + (f" (дольше 72 ч: {overdue})" if overdue else ""),
             f"• Ждём ответа дилера на твой запрос: {stats['need_info']}"]
    if stats["blocked"]:
        lines.append(f"• Решения не прошли: {len(stats['blocked'])} — " + escape(", ".join(stats["blocked"][:5]))
                     + ". Кнопка «Показать» ниже пришлёт их с вариантами")
    if unknown:
        lines.append(f"• Написали боту, но их нет в базе: {unknown} — подключить через Claude")
    lines.append(f"• Ошибок за сутки: {errors}" if errors else "• Ошибок за сутки не было")
    if survived_count:
        lines.append(f"• Сбоев, которые бот пережил сам: {survived_count}")
    return "\n".join(lines)


def summary_due(now: datetime, last_day: str | None) -> bool:
    # Только в течение часа сводки — перезапуск днём не присылает её второй раз
    return now.hour == SUMMARY_HOUR and last_day != now.date().isoformat()
