"""Напоминания дилерам по их плейсментам — раз в неделю, в выбранное время.

Поводы (по каждому проекту):
- до конца срока предрегистрации/регистрации 14 дней или меньше;
- не хватает данных для следующего этапа (и пропуски уведомления);
- по проекту нет новостей от дилера 21 день и больше (или не было вовсе).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from html import escape
from zoneinfo import ZoneInfo

from .scenarios.new_project import QUESTIONS
from .scenarios.update_project import MATERIAL_KEYS, gaps_for

EXPIRY_DAYS = 14
STALE_DAYS = 21
REMIND_EVERY_DAYS = 7
MAX_GAPS = 4

# Страна человека (People.Country) → часовой пояс; по умолчанию — Москва
PERSON_TZ = {
    "Russia": "Europe/Moscow", "Kazakhstan": "Asia/Almaty", "Uzbekistan": "Asia/Tashkent",
    "Georgia": "Asia/Tbilisi", "Armenia": "Asia/Yerevan", "Kyrgyzstan": "Asia/Bishkek",
    "Belarus": "Europe/Minsk", "Spain": "Europe/Madrid", "Italy": "Europe/Rome", "Poland": "Europe/Warsaw",
}
STATUS_WORD = {"registered": "регистрации", "pre_registered": "предрегистрации"}
_TITLES = {q.key: q.title for q in QUESTIONS}
# Необязательное в напоминаниях не требуем
_SKIP_IN_REMINDERS = {"Object Part"}
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


@dataclass
class Item:
    project_id: str
    name: str
    lines: list[str]


def _gap_titles(keys: list[str]) -> list[str]:
    out: list[str] = []
    for k in keys:
        if k in _SKIP_IN_REMINDERS:
            continue
        title = "Материалы" if k in MATERIAL_KEYS else _TITLES.get(k, k)
        if title not in out:
            out.append(title)
    return out


def project_item(project, today: date, last_news: date | None) -> Item | None:
    """Что напомнить по одному проекту (DealerProject); None — напоминать нечего."""
    lines: list[str] = []
    if project.ppr_status in STATUS_WORD and project.status_expiry:
        expiry = date.fromisoformat(project.status_expiry[:10])
        days = (expiry - today).days
        if days <= EXPIRY_DAYS:
            left = f"осталось {days} дн." if days >= 0 else "срок истёк"
            lines.append(f"срок {STATUS_WORD[project.ppr_status]} до {expiry:%d.%m.%Y}, {left} "
                         "Если есть прогресс — «Сообщить новость», чтобы запросить продление.")
    keys, _ = gaps_for(project.fields, project.ppr_status)
    if project.track == "Simplified (<30k)":
        keys = [k for k in keys if k in _PHASE1]  # Simplified: дальше уведомления не идут
    gaps = _gap_titles(keys)
    if gaps:
        more = f" и ещё {len(gaps) - MAX_GAPS}" if len(gaps) > MAX_GAPS else ""
        lines.append("не хватает данных: " + ", ".join(gaps[:MAX_GAPS]) + more + ". «Дополнить материалы».")
    if last_news is None:
        lines.append("новостей по проекту от вас ещё не было.")
    elif (today - last_news).days >= STALE_DAYS:
        lines.append(f"нет новостей с {last_news:%d.%m} — больше {STALE_DAYS} дней.")
    return Item(project.record_id, project.name, lines) if lines else None


def digest_text(items: list[Item]) -> str:
    parts = ["<b>Напоминание по вашим плейсментам</b>"]
    for it in items:
        parts.append("")
        parts.append(f"<b>{escape(it.name)}</b>")
        parts += [f"• {escape(line)}" for line in it.lines]
    return "\n".join(parts)
