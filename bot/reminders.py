"""Напоминания дилерам по их плейсментам — раз в неделю, в выбранное время.

Поводы (по каждому проекту):
- до конца срока предрегистрации/регистрации или квотации 7 дней или меньше;
- не хватает данных для следующего этапа — ровно то, что в PPR Next Step Missing;
- по проекту нет новостей от дилера 21 день и больше (или не было вовсе).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from html import escape
from zoneinfo import ZoneInfo

from .airtable_ids import Projects
from .labels import missing_ru

EXPIRY_DAYS = 7  # до конца статуса или квотации — напоминаем за неделю
STALE_DAYS = 21
REMIND_EVERY_DAYS = 7

# Страна человека (People.Country) → часовой пояс; по умолчанию — Москва
PERSON_TZ = {
    "Russia": "Europe/Moscow", "Kazakhstan": "Asia/Almaty", "Uzbekistan": "Asia/Tashkent",
    "Georgia": "Asia/Tbilisi", "Armenia": "Asia/Yerevan", "Kyrgyzstan": "Asia/Bishkek",
    "Belarus": "Europe/Minsk", "Spain": "Europe/Madrid", "Italy": "Europe/Rome", "Poland": "Europe/Warsaw",
}


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
    (GAPS, "📝 <b>Не хватает данных для следующего этапа</b>", "Добавь через «Обновить проект» → «Дополнить материалы»."),
    (STALE, f"💤 <b>Нет новостей больше {STALE_DAYS} дня</b>", "Расскажи, что нового, — «Сообщить новость»."),
)
STATUS_NAME = {"registered": "регистрация", "pre_registered": "предрегистрация"}


@dataclass
class Item:
    project_id: str
    name: str
    # причина → подробность («до 20.10.2026, осталось 8 дн.»)
    reasons: dict


def next_step_gaps(raw: str | None, stage: str | None = None) -> list[str]:
    """«Object Address; Dossier Files;» → ["адрес", "материалы"]; упрощённый трек и пусто — ничего.
    На выборе архитектора бюро ещё нет — не просим."""
    gaps = missing_ru(raw)
    if stage == "Architect selection":
        gaps = [g for g in gaps if g != "бюро"]
    return gaps


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
        # Истёкший срок — в одном напоминании после даты истечения, не каждую неделю
        if -REMIND_EVERY_DAYS < days <= EXPIRY_DAYS:
            left = f"осталось {days} дн." if days >= 0 else "срок истёк"
            status_line = f"{STATUS_NAME[project.ppr_status]} до {expiry:%d.%m.%Y}, {left}"
            reasons[EXPIRY] = f"{status_line}; {reasons[EXPIRY]}" if EXPIRY in reasons else status_line
    # Только то, что считает PPR Next Step Missing: старые проекты законно не имеют многих полей анкеты
    gaps = next_step_gaps(project.fields.get(Projects.PPR_NEXT_MISSING), project.fields.get(Projects.PROJECT_STAGE))
    if gaps:
        reasons[GAPS] = ", ".join(gaps)
    if last_news is None:
        reasons[STALE] = "новостей ещё не было"
    elif (today - last_news).days >= STALE_DAYS:
        reasons[STALE] = f"последняя новость — {last_news:%d.%m.%Y}"
    return Item(project.record_id, project.name, reasons) if reasons else None


def digest_text(items: list[Item], intro: str | None = None) -> str:
    """Сообщение, сгруппированное по причинам; пустые группы не показываем."""
    parts = [f"<b>{escape(intro or 'Напоминание по твоим плейсментам')}</b>"]  # REM-01
    for reason, title, hint in SECTIONS:
        rows = [f"• {escape(it.name)} — {escape(it.reasons[reason])}" for it in items if reason in it.reasons]
        if rows:
            parts += ["", title, *rows, hint]
    return "\n".join(parts)
