"""«📅 В календарь» для подтверждённых звонков: ссылка в Outlook (Microsoft 365 AW) и файл .ics для любого календаря.

Время — из строки слота («15.11.2026 Пн, 13:00–15:00 Мск (Валенсия 12:00–14:00)»), по Валенсии.
Свободный текст партнёра («в среду после обеда») не разбираем — кнопки нет, бот так и пишет.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from .slots import valencia_start

OUTLOOK = "https://outlook.office.com/calendar/0/deeplink/compose"
DEFAULT_MINUTES = 30


@dataclass
class CallEvent:
    start: datetime
    minutes: int
    title: str
    body: str

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.minutes)

    def outlook_url(self) -> str:
        """Outlook в браузере / приложении: встреча заполнена, остаётся «Сохранить»."""
        query = urlencode({
            "path": "/calendar/action/compose", "rru": "addevent", "subject": self.title, "body": self.body,
            "startdt": self.start.isoformat(timespec="seconds"), "enddt": self.end.isoformat(timespec="seconds"),
        })
        return f"{OUTLOOK}?{query}"

    def ics(self) -> bytes:
        """Файл встречи: открывается календарём телефона (Apple, Outlook, Google)."""
        def utc(dt: datetime) -> str:
            return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        def esc(text: str) -> str:
            return text.replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace("\n", r"\n")

        lines = [
            "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Andreu World//Pepe bot//RU", "CALSCALE:GREGORIAN",
            "BEGIN:VEVENT", f"UID:{uuid.uuid4()}@pepe", f"DTSTAMP:{utc(datetime.now(timezone.utc))}",
            f"DTSTART:{utc(self.start)}", f"DTEND:{utc(self.end)}",
            f"SUMMARY:{esc(self.title)}", f"DESCRIPTION:{esc(self.body)}",
            "BEGIN:VALARM", "TRIGGER:-PT15M", "ACTION:DISPLAY", "DESCRIPTION:Звонок", "END:VALARM",
            "END:VEVENT", "END:VCALENDAR",
        ]
        return ("\r\n".join(lines) + "\r\n").encode("utf-8")

    def filename(self) -> str:
        return f"zvonok_{self.start:%Y-%m-%d_%H%M}.ics"


def minutes_of(label: str) -> int:
    """«Звонок 15 минут» → 15, «Звонок на 30 минут перед регистрацией» → 30."""
    m = re.search(r"(\d+)\s*мин", label or "")
    return int(m.group(1)) if m else DEFAULT_MINUTES


def call_event(label: str, value: str, who: str, project: str, phone: str = "", company: str = "") -> CallEvent | None:
    start = valencia_start(value)
    if not start:
        return None
    person = f"{who} ({company})" if company else who
    body = "\n".join(x for x in (f"Проект: {project}", f"С кем: {person}", f"Телефон: {phone}" if phone else "",
                                 f"Слот: {value}", "Добавлено ботом Пепе") if x)
    return CallEvent(start, minutes_of(label), f"Звонок · {who} · {project}", body)
