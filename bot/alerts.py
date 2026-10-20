"""Ошибки бота — Егору сразу (пачкой раз в минуту), плюс короткая сводка каждое утро."""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from html import escape

from .slots import VALENCIA

SUMMARY_HOUR = 9        # сводка — в 9 утра по Валенсии
MAX_LINES = 5           # в одном сообщении об ошибках
_IGNORED = ("telegram.ext.Updater",)  # сетевые сбои опроса Telegram — не ошибки бота


class AlertHandler(logging.Handler):
    """Копит ERROR-записи логов; отправляет их цикл в main."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.queue: deque = deque(maxlen=50)
        self.day_count = 0

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

    def drain(self) -> str | None:
        """Текст сообщения о накопленных ошибках (и очистка очереди); None — ошибок не было."""
        if not self.queue:
            return None
        items = list(self.queue)
        self.queue.clear()
        lines = [f"🛠 ⚠️ <b>Пепе: ошибки ({len(items)})</b>"]
        lines += [f"• {at:%H:%M} {escape(text)}" for at, text in items[:MAX_LINES]]
        if len(items) > MAX_LINES:
            lines.append(f"• и ещё {len(items) - MAX_LINES} — подробности в логах Railway")
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
