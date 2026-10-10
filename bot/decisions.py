"""Решения Егора по заявкам Inbox: статус дилера в проекте и ответы в Telegram.

Егор ставит Decision в Registrations — бот раз в минуту находит такие заявки,
применяет решение и отмечает в Bot Processed. Логика решения (plan) — чистая
функция без Airtable и Telegram; применение — apply_plan в main.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import escape

from .airtable_ids import Decision, Projects, Registrations, RegStatus, RequestType

PPR_NOTIFIED, PPR_PRE, PPR_REG = "Notification", "Pre-registration", "Registration"
STATUS_FIELD = {
    PPR_NOTIFIED: Projects.NOTIFIED,
    PPR_PRE: Projects.PRE_REGISTERED,
    PPR_REG: Projects.REGISTERED,
}
STATUS_LABEL = {PPR_NOTIFIED: "уведомление", PPR_PRE: "предрегистрацию", PPR_REG: "регистрацию"}
STATUS_FIELD_NAME = {PPR_NOTIFIED: "Notified", PPR_PRE: "Pre-registered", PPR_REG: "Registered"}

# Request Type → какой статус даёт Accept, если Егор не выставил Reg Status явно
REQUEST_STATUS = {
    RequestType.NEW_NOTIFICATION: PPR_NOTIFIED,
    RequestType.NEW_PRE_REGISTRATION: PPR_PRE,
    RequestType.UPGRADE_PRE_REGISTRATION: PPR_PRE,
    RequestType.NEW_REGISTRATION: PPR_REG,
    RequestType.UPGRADE_REGISTRATION: PPR_REG,
}
NO_STATUS_TYPES = {RequestType.UPDATE_INFO, RequestType.UPDATE_WON, RequestType.UPDATE_LOST}

# Те же поля, что в формуле PPR Missing: она считает только когда в проекте уже есть
# пред/регистрация, поэтому для первой предрегистрации проверяем сами.
_PPR_REQUIRED = (
    (Projects.EXW_POTENTIAL, "EXW Potential"),
    (Projects.EXPECTED_ORDER_DATE, "Expected Order Date"),
    (Projects.OBJECT_ADDRESS, "Object Address"),
    (Projects.SPECIFIER, "Specifier"),
    (Projects.CONTACT_LEVEL, "Contact Level"),
    (Projects.AW_ALTERNATIVES, "AW Alternatives"),
    (Projects.PARALLEL_REGISTRATIONS, "Parallel Registrations"),
    (Projects.DOSSIER_FILES, "Dossier Files"),
)


def _empty(v) -> bool:
    return v in (None, "", [], 0)


# Ответы дилера из Payload → поля Projects (переносятся только в пустые поля)
_PAYLOAD_TO_PROJECT = (
    ("Object Name", Projects.OBJECT_NAME),
    ("Object Part", Projects.OBJECT_PART),
    ("Country", Projects.COUNTRY),
    ("City", Projects.CITY),
    ("Object Address", Projects.OBJECT_ADDRESS),
    ("Project Stage", Projects.PROJECT_STAGE),
    ("EXW Potential", Projects.EXW_POTENTIAL),
    ("AW Alternatives", Projects.AW_ALTERNATIVES),
    ("Contact Level", Projects.CONTACT_LEVEL),
    ("Parallel Registrations", Projects.PARALLEL_REGISTRATIONS),
    ("Quotation / Proforma №", Projects.QUOTATION),
)
# Ключи Payload с материалами — это Dossier Files
DOSSIER_KEYS = (
    "Visualizations", "Layouts", "Partner Presentation", "Bureau Documents", "Dealer Specification",
    "AW Quotation File", "Third-party Statement", "Tender Brief", "Purchase Request",
    "Other Third-party Document", "Extra Materials",
)
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def payload_of(reg: dict) -> dict:
    try:
        data = json.loads(reg.get(Registrations.PAYLOAD) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def transfers(reg: dict, project: dict) -> tuple[dict, set]:
    """Что перенести из заявки в проект и какие поля дилер дал, но бот записать не может.

    Возвращает (обновления пустых полей проекта, id полей «предоставлено» — Specifier текстом,
    материалы ссылками).
    """
    payload = payload_of(reg)
    updates: dict = {}
    provided: set = set()
    for key, fid in _PAYLOAD_TO_PROJECT:
        value = payload.get(key)
        if not _empty(value) and _empty(project.get(fid)):
            updates[fid] = value
    order = payload.get("Order Month")
    if isinstance(order, str) and _ISO_DATE.fullmatch(order) and _empty(project.get(Projects.EXPECTED_ORDER_DATE)):
        updates[Projects.EXPECTED_ORDER_DATE] = order
    if payload.get("Specifier") and _empty(project.get(Projects.SPECIFIER)):
        provided.add(Projects.SPECIFIER)  # связь с Companies ставит Егор
    # Файлы заявки — в Dossier Files (дописываем к уже лежащим)
    files = reg.get(Registrations.ATTACHMENTS) or []
    if files:
        kept = [{"id": a["id"]} for a in project.get(Projects.DOSSIER_FILES) or [] if a.get("id")]
        updates[Projects.DOSSIER_FILES] = kept + [
            {"url": a["url"], "filename": a.get("filename")} for a in files if a.get("url")
        ]
    if any(isinstance(payload.get(k), list) and payload[k] for k in DOSSIER_KEYS):
        provided.add(Projects.DOSSIER_FILES)  # в том числе ссылками
    return updates, provided


def ppr_missing(project: dict, status: str, filled: dict | None = None, provided: set | None = None) -> list[str]:
    """Чего не хватает для Pre-registered / Registered — с учётом того, что дал дилер в заявке."""
    if status not in (PPR_PRE, PPR_REG):
        return []
    filled = filled or {}
    provided = provided or set()

    def has(fid: str) -> bool:
        return not _empty(project.get(fid)) or not _empty(filled.get(fid)) or fid in provided

    missing = [name for fid, name in _PPR_REQUIRED if not has(fid)]
    if status == PPR_REG and not has(Projects.QUOTATION):
        missing.append("Quotation / Proforma №")
    return missing


_FIELD_NAMES = {fid: key for key, fid in _PAYLOAD_TO_PROJECT}
_FIELD_NAMES.update({Projects.EXPECTED_ORDER_DATE: "Expected Order Date", Projects.DOSSIER_FILES: "Dossier Files"})


def _names(updates: dict) -> list[str]:
    return [_FIELD_NAMES.get(fid, fid) for fid in updates]


def processed_mark(decision: str, blocked: bool = False) -> str:
    return f"{'blocked' if blocked else 'done'}:{decision}"


@dataclass
class Plan:
    # Что записать
    project_updates: dict = field(default_factory=dict)
    registration_updates: dict = field(default_factory=dict)
    # Кому что написать
    dealer_text: str | None = None
    admin_text: str | None = None
    # Компании, которым разослать «появилась предрегистрация другого партнёра»
    broadcast_companies: list[str] = field(default_factory=list)
    broadcast_text: str | None = None
    # Сообщать ли админу об этой блокировке (только первый раз)
    blocked: bool = False
    # Какие поля бот перенёс из заявки в проект — для сообщения админу
    transferred: list[str] = field(default_factory=list)
    # Кнопка «Ответить» под сообщением дилеру (запрос доп. информации)
    reply_button: bool = False


def _note(reg: dict) -> str:
    note = (reg.get(Registrations.DECISION_NOTE) or "").strip()
    return f"\n\nКомментарий: {escape(note)}" if note else ""


def _blocked(reg: dict, decision: str, reason: str) -> Plan:
    name = reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия"
    first_time = reg.get(Registrations.BOT_PROCESSED) != processed_mark(decision, blocked=True)
    return Plan(
        registration_updates={Registrations.BOT_PROCESSED: processed_mark(decision, blocked=True)},
        admin_text=f"🛠 ⚠️ Заявка «{escape(name)}»: {reason}" if first_time else None,
        blocked=True,
    )


def plan(reg: dict, project: dict | None) -> Plan:
    """Что сделать по решению Егора. reg и project — поля записей по id."""
    decision = reg.get(Registrations.DECISION)
    request_type = reg.get(Registrations.REQUEST_TYPE)
    dealer_name = reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия"
    note = (reg.get(Registrations.DECISION_NOTE) or "").strip()
    done = {Registrations.BOT_PROCESSED: processed_mark(decision)}

    if decision in (Decision.DECLINE, Decision.NEED_INFO):
        if not note:
            return _blocked(reg, decision, f"для «{decision}» заполните Decision Note — он уходит дилеру.")
        if decision == Decision.DECLINE:
            return Plan(
                registration_updates={**done, Registrations.REG_STATUS: RegStatus.DECLINED},
                dealer_text=f"По запросу «{escape(dealer_name)}»: отказ.{_note(reg)}",
            )
        return Plan(
            registration_updates=done,
            dealer_text=f"По запросу «{escape(dealer_name)}» нужна дополнительная информация.{_note(reg)}",
            reply_button=True,
        )

    if decision not in (Decision.ACCEPT, Decision.ACCEPT_MERGE):
        return Plan()

    if project is None:
        return _blocked(
            reg, decision,
            "не указан Target Project. Перенос в Projects пока ручной: создайте или найдите проект "
            "и поставьте его в Target Project — бот продолжит сам.",
        )
    project_name = project.get(Projects.NAME) or dealer_name
    # Дилеру — объект его словами; имя в базе (латиницей) — в скобках, если отличается
    obj = f"«{escape(dealer_name)}»"
    if project_name.strip().lower() != dealer_name.strip().lower():
        obj += f" (в базе — {escape(project_name)})"
    # Ответ на запрос принимается вместе с исходной заявкой — дилеру второй раз не пишем
    is_reply = bool(payload_of(reg).get("In Reply To"))

    # Выигрыш и проигрыш одного дилера — не повод менять проект: только ответ
    if request_type in (RequestType.UPDATE_WON, RequestType.UPDATE_LOST):
        return Plan(
            registration_updates=done,
            dealer_text=f"По запросу {obj} информацию приняли.{_note(reg)}",
        )
    moved, provided = transfers(reg, project)
    if request_type == RequestType.UPDATE_INFO:
        return Plan(
            project_updates=moved,
            registration_updates=done,
            dealer_text=None if is_reply else f"По запросу {obj} информацию приняли и внесли в проект.{_note(reg)}",
            transferred=_names(moved),
        )

    reg_status = reg.get(Registrations.REG_STATUS)
    status = reg_status if reg_status in STATUS_FIELD else REQUEST_STATUS.get(request_type)
    if status is None:
        return _blocked(reg, decision, "не понятно, какой статус дать — выставьте Reg Status.")

    dealers = reg.get(Registrations.DEALER) or []
    if not dealers:
        return _blocked(reg, decision, "в заявке не указан Dealer.")
    dealer = dealers[0]

    missing = ppr_missing(project, status, moved, provided)
    if missing:
        return _blocked(
            reg, decision,
            f"нельзя поставить {STATUS_FIELD_NAME[status]} в «{escape(project_name)}»: "
            f"не хватает {escape('; '.join(missing))} — ни в проекте, ни в заявке.\n"
            "Запросить у дилера: Decision → Need more info, текст — в Decision Note.",
        )

    notified = list(project.get(Projects.NOTIFIED) or [])
    pre = list(project.get(Projects.PRE_REGISTERED) or [])
    registered = list(project.get(Projects.REGISTERED) or [])
    others_registered = [c for c in registered if c != dealer]
    others_pre = [c for c in pre if c != dealer]

    if status == PPR_REG and others_registered:
        return _blocked(
            reg, decision,
            f"в «{escape(project_name)}» уже есть Registered — регистрация только у одного партнёра.",
        )

    updates: dict = dict(moved)
    broadcast: list[str] = []
    if status == PPR_NOTIFIED:
        if dealer not in notified + pre + registered:
            updates[Projects.NOTIFIED] = notified + [dealer]
    elif status == PPR_PRE:
        if dealer not in pre:
            pre = pre + [dealer]
            updates[Projects.PRE_REGISTERED] = pre
            if dealer in notified:
                updates[Projects.NOTIFIED] = [c for c in notified if c != dealer]
            # Остальным партнёрам на объекте — сигнал о новой предрегистрации
            broadcast = [c for c in notified + others_pre if c != dealer]
    else:  # регистрация
        updates[Projects.REGISTERED] = [dealer]
        if dealer in pre:
            updates[Projects.PRE_REGISTERED] = [c for c in pre if c != dealer]
        if dealer in notified:
            updates[Projects.NOTIFIED] = [c for c in notified if c != dealer]

    if status in (PPR_PRE, PPR_REG) and _empty(project.get(Projects.STATUS_CONFIRMED)):
        updates[Projects.STATUS_CONFIRMED] = "__today__"  # дату подставит apply_plan

    # Ответ дилеру — без имён других партнёров
    if status == PPR_NOTIFIED:
        text = f"По запросу {obj} установлен статус: <b>уведомление</b>."
        if others_registered:
            text += "\nНа проекте есть зарегистрированный партнёр."
        elif others_pre:
            text += "\nНа проекте есть предрегистрация другого партнёра."
    elif status == PPR_PRE:
        text = (f"По запросу {obj} установлен статус: <b>предрегистрация</b>.\n"
                f"Вы {pre.index(dealer) + 1}-й из {len(pre)} в предрегистрации.")
        if others_registered:
            text += "\nНа проекте есть зарегистрированный партнёр."
    else:
        text = f"По запросу {obj} установлен статус: <b>регистрация</b>."

    reg_updates = dict(done)
    if reg_status != status:
        reg_updates[Registrations.REG_STATUS] = status
    return Plan(
        project_updates=updates,
        registration_updates=reg_updates,
        transferred=_names(moved),
        dealer_text=text + _note(reg),
        broadcast_companies=broadcast,
        broadcast_text=(f"По объекту «{escape(project_name)}» появилась предрегистрация другого партнёра."
                        if broadcast else None),
    )
