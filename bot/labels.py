"""Русские подписи для значений Airtable: поля формулы PPR Next Step Missing / PPR Missing, решения, статусы.

Сырые английские имена полей и значений в сообщениях не показываем (правило 1.2 задания от 08.11).
Неизвестный ключ формулы — в лог и «другие данные», не сырым текстом.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Префикс ключа формулы → (коротко для напоминаний, подробно для запроса дилеру)
MISSING_RU = (
    ("EXW Potential", "объём", "уточнённый объём Andreu World по прайсу"),
    ("Expected Order Date", "месяц заказа", "месяц размещения заказа"),
    ("Object Address", "адрес", "адрес объекта"),
    ("Specifier", "бюро", "архитектурное бюро"),
    ("Contact Level", "подтверждённый контакт с клиентом",
     "подтверждённый контакт с клиентом — переписка с клиентом или его контакт"),
    ("AW Alternatives", "альтернативы Andreu World", "предлагаешь ли клиенту альтернативы Andreu World"),
    ("Parallel Registrations", "заявлено ли у других производителей",
     "заявлен ли объём у других производителей мебели"),
    ("Dossier Files", "материалы", "материалы — визуализации, планировки, ведомость"),
    ("Quotation", "номер квотации", "номер квотации Andreu World"),
)
OTHER = "другие данные"
SIMPLIFIED_PREFIX = "Simplified"


def _keys(raw: str | list | None) -> list[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [k.strip() for k in str(raw or "").split(";") if k.strip()]


def missing_ru(raw: str | list | None, detailed: bool = False) -> list[str]:
    """«Object Address; Dossier Files;» → ["адрес", "материалы"]. Упрощённый трек — пусто."""
    out: list[str] = []
    for key in _keys(raw):
        if key.startswith(SIMPLIFIED_PREFIX):
            continue
        hit = next((row for row in MISSING_RU if key.startswith(row[0])), None)
        if hit is None:
            log.warning("Неизвестный ключ формулы недостающего: %r", key)
            label = OTHER
        else:
            label = hit[2] if detailed else hit[1]
        if label not in out:
            out.append(label)
    return out


def missing_text(raw: str | list | None, detailed: bool = False) -> str:
    return ", ".join(missing_ru(raw, detailed))


DECISION_RU = {
    "Accept": "принята",
    "Accept — merge into Target Project": "принята в проект",
    "Decline": "отклонена",
    "Need more info": "запрошена информация у дилера",
}

REQUEST_RU = {
    "New — Notification": "уведомление",
    "New — Pre-registration": "предрегистрация",
    "New — Registration": "регистрация",
    "Upgrade — Pre-registration": "переход на предрегистрацию",
    "Upgrade — Registration": "переход на регистрацию",
    "Update — info only": "обновление",
    "Update — fields": "дополнение",
    "Update — news": "новость",
    "Update — Won": "выигран",
    "Update — Lost": "проигран",
    "Quotation — Simplified": "квотация по упрощённой схеме",
    "Unknown user": "новый человек",
}

REG_STATUS_RU = {
    "PENDING": "на проверке", "Notification": "уведомление", "Pre-registration": "предрегистрация",
    "Registration": "регистрация", "Declined": "отклонена", "Cancelled": "отменена",
}

TRACK_RU = {"Full": "полная схема", "Simplified (<30k)": "упрощённая схема (до 30 000 € по прайсу)"}


def date_ru(iso: str | None) -> str:
    """2026-11-08 → 08.11.2026; пусто — пусто."""
    s = str(iso or "")[:10]
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return f"{s[8:10]}.{s[5:7]}.{s[:4]}"
    return s
