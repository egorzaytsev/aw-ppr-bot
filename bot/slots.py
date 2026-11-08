"""Слоты для звонка: ближайшие рабочие дни × окна по времени Валенсии.

Окна заданы по Валенсии (рабочее время AW). Дилеру показываем их сразу в местном
времени по стране объекта; Валенсию бот пересчитывает сам и пишет в заявку.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

VALENCIA = ZoneInfo("Europe/Madrid")
DAYS_AHEAD = 4  # рабочих дней, начиная с завтра
# Окна по времени Валенсии
WINDOWS = ((time(10), time(12)), (time(12), time(14)), (time(15), time(17)), (time(17), time(19)))

WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

# Страна объекта (значение Country) → (часовой пояс, подпись)
LOCAL_TZ = {
    "Russia": ("Europe/Moscow", "Мск"),
    "Kazakhstan": ("Asia/Almaty", "Алматы"),
    "Uzbekistan": ("Asia/Tashkent", "Ташкент"),
    "Georgia": ("Asia/Tbilisi", "Тбилиси"),
    "Armenia": ("Asia/Yerevan", "Ереван"),
    "Kyrgyzstan": ("Asia/Bishkek", "Бишкек"),
    "Belarus": ("Europe/Minsk", "Минск"),
}


def next_workdays(today: date | None = None, n: int = DAYS_AHEAD) -> list[date]:
    d = today or datetime.now(VALENCIA).date()
    out = []
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


def day_label(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m}"


def _local(d: date, t: time, country: str | None) -> str | None:
    if country not in LOCAL_TZ:
        return None
    tz, _ = LOCAL_TZ[country]
    return datetime.combine(d, t, VALENCIA).astimezone(ZoneInfo(tz)).strftime("%H:%M")


def zone_name(country: str | None) -> str:
    return LOCAL_TZ[country][1] if country in LOCAL_TZ else "Валенсия"


def zone_label(country: str | None) -> str:
    """Для текста дилеру: «время Мск» или «время по Валенсии (Испания)», если страна не из списка."""
    return f"время {LOCAL_TZ[country][1]}" if country in LOCAL_TZ else "время по Валенсии (Испания)"


def valencia_window(idx: int) -> str:
    start, end = WINDOWS[idx]
    return f"{start:%H:%M}–{end:%H:%M}"


def window_label(d: date, idx: int, country: str | None) -> str:
    """Кнопка для дилера: окно в его местном времени (или по Валенсии, если страна не из списка)."""
    start, end = WINDOWS[idx]
    local_start = _local(d, start, country)
    if not local_start:
        return valencia_window(idx)
    return f"{local_start}–{_local(d, end, country)}"


def slot_value(d: date, idx: int, country: str | None) -> str:
    """Строка для Payload / Project Log: местное время дилера и пересчёт на Валенсию."""
    head = f"{d:%d.%m.%Y} {day_label(d)[:2]}, {window_label(d, idx, country)} {zone_name(country)}"
    if country in LOCAL_TZ:
        head += f" (Валенсия {valencia_window(idx)})"
    return head


_SLOT_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})[^\n]*?(?:Валенсия (\d{2}):(\d{2})|(\d{2}):(\d{2})–\d{2}:\d{2} Валенсия)")


def valencia_start(value: str) -> datetime | None:
    """Начало звонка по Валенсии из строки слота («05.11.2026 Чт, 14:00–16:00 Мск (Валенсия 12:00–14:00)»).
    Свободный текст партнёра — None."""
    m = _SLOT_RE.search(str(value or ""))
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh, mm = (m.group(4), m.group(5)) if m.group(4) else (m.group(6), m.group(7))
    try:
        return datetime(y, mo, d, int(hh), int(mm), tzinfo=VALENCIA)
    except ValueError:
        return None
