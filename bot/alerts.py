"""Ошибки бота — Егору сразу (пачкой раз в минуту), плюс короткая сводка каждое утро."""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta
from html import escape

from .slots import VALENCIA

SUMMARY_HOUR = 9        # сводка — в 9 утра по Валенсии
MAX_LINES = 5           # в одном сообщении об ошибках
REPEAT_AFTER = timedelta(hours=1)  # одинаковую ошибку присылаем не чаще раза в час
_IGNORED = ("telegram.ext.Updater",)  # сетевые сбои опроса Telegram — не ошибки бота


class AlertHandler(logging.Handler):
    """Копит ERROR-записи логов; отправляет их цикл в main."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.queue: deque = deque(maxlen=200)
        self.day_count = 0
        self._sent: dict[str, datetime] = {}  # текст ошибки → когда последний раз присылали

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(_IGNORED):
            return
        try:
            text = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                text += f" — {type(exc).__name__}: {exc}"
            self.queue.append((datetime.now(VALENCIA), text[:300]))
            self.day_count += 1
        except Exception:
            pass

    def drain(self, now: datetime | None = None) -> str | None:
        """Сообщение о новых ошибках (очередь очищается); одинаковые — одной строкой и не чаще раза в час.
        None — присылать нечего."""
        if not self.queue:
            return None
        now = now or datetime.now(VALENCIA)
        items = list(self.queue)
        self.queue.clear()
        grouped: dict[str, list] = {}
        for at, text in items:
            grouped.setdefault(text, []).append(at)
        fresh = [(times[0], text, len(times)) for text, times in grouped.items()
                 if not (text in self._sent and now - self._sent[text] < REPEAT_AFTER)]
        if not fresh:
            return None
        for _, text, _ in fresh:
            self._sent[text] = now
        total = sum(n for *_, n in fresh)
        lines = [f"🛠 ⚠️ <b>Пепе: ошибки ({total})</b>"]
        lines += [f"• {at:%H:%M} {escape(text)}" + (f" (×{n})" if n > 1 else "") for at, text, n in fresh[:MAX_LINES]]
        if len(fresh) > MAX_LINES:
            lines.append(f"• и ещё {len(fresh) - MAX_LINES} разных — подробности в логах Railway")
        return "\n".join(lines)


def summary_text(day: datetime, errors: int, stats: dict, overdue: int, unknown: int) -> str:
    lines = [f"🛠 <b>Сводка Пепе · {day:%d.%m}</b>",
             f"• Заявок без решения: {stats['no_decision']}" + (f" (дольше 72 ч: {overdue})" if overdue else ""),
             f"• Ждут ответа дилера: {stats['need_info']}"]
    if stats["blocked"]:
        lines.append(f"• Застряли решения: {len(stats['blocked'])} — " + escape(", ".join(stats["blocked"][:3])))
    if unknown:
        lines.append(f"• Новые люди ждут добавления в People: {unknown}")
    lines.append(f"• Ошибок за сутки: {errors}" if errors else "• Ошибок за сутки не было")
    return "\n".join(lines)


def summary_due(now: datetime, last_day: str | None) -> bool:
    # Только в течение часа сводки — перезапуск днём не присылает её второй раз
    return now.hour == SUMMARY_HOUR and last_day != now.date().isoformat()
