"""Напоминания дилерам по их плейсментам — раз в неделю, в выбранное время.

Поводы (по каждому проекту):
- до конца срока предрегистрации/регистрации или квотации 7 дней или меньше;
- не хватает данных для следующего этапа (и пропуски уведомления);
- по проекту нет новостей от дилера 21 день и больше (или не было вовсе).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from html import escape
from zoneinfo import ZoneInfo

from .scenarios.new_project import QUESTIONS
from .scenarios.update_project import LOG_GAP_KEYS, MATERIAL_KEYS, gaps_for

EXPIRY_DAYS = 7  # до конца статуса или квотации — напоминаем за неделю
STALE_DAYS = 21
REMIND_EVERY_DAYS = 7
MAX_GAPS = 3

# Страна человека (People.Country) → часовой пояс; по умолчанию — Москва
PERSON_TZ = {
    "Russia": "Europe/Moscow", "Kazakhstan": "Asia/Almaty", "Uzbekistan": "Asia/Tashkent",
    "Georgia": "Asia/Tbilisi", "Armenia": "Asia/Yerevan", "Kyrgyzstan": "Asia/Bishkek",
    "Belarus": "Europe/Minsk", "Spain": "Europe/Madrid", "Italy": "Europe/Rome", "Poland": "Europe/Warsaw",
}
_TITLES = {q.key: q.title for q in QUESTIONS}
# Необязательное и ответы из Project Log в напоминаниях не требуем
_SKIP_IN_REMINDERS = {"Object Part", "AW Origin", *LOG_GAP_KEYS}
_PHASE1 = {q.key for q in QUESTIONS if q.phase == 1}


def person_now(country: str | None) -> datetime:
    return datetime.now(ZoneInfo(PERSON_TZ.get(country or "", "Europe/Moscow")))


def in_slot(now: datetime, slot: str | None) -> bool:
    """Слот вида «11:00–14:00» по местному времени человека."""
    if not slot or "–" not in slot:
        return False
    start, end = (time.fromisoformat(t.strip()) for t in slot.split("–"))
    return start <= now.time() < end


def due(last_reminder: str | None, today: date) -> bool:
    if today.weekday() >= 5:  # только будни
        return False
    if not last_reminder:
        return True
    return (today - date.fromisoformat(last_reminder[:10])).days >= REMIND_EVERY_DAYS


# Причины напоминания — по ним группируется сообщение
EXPIRY, GAPS, STALE = "expiry", "gaps", "stale"
SECTIONS = (
    (EXPIRY, "⏳ <b>Срок подходит к концу</b>",
     "Если есть прогресс — «Сообщить новость», чтобы запросить продление."),
    (GAPS, "📝 <b>Не хватает данных для следующего этапа</b>", "Дополнить — «Дополнить материалы»."),
    (STALE, f"💤 <b>Нет новостей больше {STALE_DAYS} дней</b>", "Расскажи, что нового, — «Сообщить новость»."),
)
STATUS_NAME = {"registered": "регистрация", "pre_registered": "предрегистрация"}


@dataclass
class Item:
    project_id: str
    name: str
    # причина → подробность («до 20.10.2026, осталось 8 дн.»)
    reasons: dict


def _gap_titles(keys: list[str]) -> list[str]:
    out: list[str] = []
    for k in keys:
        if k in _SKIP_IN_REMINDERS:
            continue
        title = "Материалы" if k in MATERIAL_KEYS else _TITLES.get(k, k)
        if title not in out:
            out.append(title)
    return out


def project_item(project, today: date, last_news: date | None, quote_valid: str | None = None) -> Item | None:
    """Что напомнить по одному проекту (DealerProject); None — напоминать нечего."""
    reasons: dict = {}
    if quote_valid:
        until = date.fromisoformat(quote_valid[:10])
        days = (until - today).days
        if days <= EXPIRY_DAYS:
            left = f"осталось {days} дн." if days >= 0 else "срок истёк"
            reasons[EXPIRY] = f"квотация до {until:%d.%m.%Y}, {left}"
    if project.ppr_status in STATUS_NAME and project.status_expiry:
        expiry = date.fromisoformat(project.status_expiry[:10])
        days = (expiry - today).days
        if days <= EXPIRY_DAYS:
            left = f"осталось {days} дн." if days >= 0 else "срок истёк"
            status_line = f"{STATUS_NAME[project.ppr_status]} до {expiry:%d.%m.%Y}, {left}"
            reasons[EXPIRY] = f"{status_line}; {reasons[EXPIRY]}" if EXPIRY in reasons else status_line
    keys, _ = gaps_for(project.fields, project.ppr_status)
    if project.track == "Simplified (<30k)":
        keys = [k for k in keys if k in _PHASE1]  # Simplified: дальше уведомления не идут
    gaps = _gap_titles(keys)
    if gaps:
        more = f" и ещё {len(gaps) - MAX_GAPS}" if len(gaps) > MAX_GAPS else ""
        reasons[GAPS] = ", ".join(gaps[:MAX_GAPS]) + more
    if last_news is None:
        reasons[STALE] = "новостей ещё не было"
    elif (today - last_news).days >= STALE_DAYS:
        reasons[STALE] = f"последняя {last_news:%d.%m}"
    return Item(project.record_id, project.name, reasons) if reasons else None


def digest_text(items: list[Item], intro: str | None = None) -> str:
    """Сообщение, сгруппированное по причинам; пустые группы не показываем."""
    parts = [f"<b>{escape(intro or 'Напоминание по твоим плейсментам')}</b>"]
    for reason, title, hint in SECTIONS:
        rows = [f"• {escape(it.name)} — {escape(it.reasons[reason])}" for it in items if reason in it.reasons]
        if rows:
            parts += ["", title, *rows, hint]
    return "\n".join(parts)
