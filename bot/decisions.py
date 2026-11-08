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

from .airtable_ids import Decision, Partners, Projects, Registrations, RegStatus, RequestType
from .labels import MISSING_RU

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
UPGRADE_TYPES = {
    RequestType.NEW_PRE_REGISTRATION, RequestType.UPGRADE_PRE_REGISTRATION,
    RequestType.NEW_REGISTRATION, RequestType.UPGRADE_REGISTRATION,
}
NO_STATUS_TYPES = {*RequestType.INFO_UPDATES, RequestType.UPDATE_WON, RequestType.UPDATE_LOST}

# Те же поля, что в формуле PPR Missing: она считает только когда в проекте уже есть
# пред/регистрация, поэтому для первой предрегистрации проверяем сами.
_PPR_REQUIRED = (
    (Projects.EXW_POTENTIAL, "EXW Potential"),
    (Projects.EXPECTED_ORDER_DATE, "Expected Order Date"),
    (Projects.OBJECT_ADDRESS, "Object Address"),
    (Projects.SPECIFIER, "Specifier"),
    (Projects.AW_ALTERNATIVES, "AW Alternatives"),
    (Projects.PARALLEL_REGISTRATIONS, "Parallel Registrations"),
    (Projects.DOSSIER_FILES, "Dossier Files"),
)


def _empty(v) -> bool:
    return v in (None, "", [], 0)


# Ответы дилера из Payload → поля Projects (переносятся только в пустые поля,
# кроме _OVERWRITE: объём и стадию дилер уточняет по ходу — новое значение заменяет старое)
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
    ("PPR Track", Projects.PPR_TRACK),
)
_OVERWRITE = {Projects.EXW_POTENTIAL: "💶 Объём EXW", Projects.PROJECT_STAGE: "🏗 Стадия"}
STAGE_RU = {"Architect selection": "выбор архитектора", "Concept": "концепция", "Design": "проектирование",
            "Tender": "тендер", "Procurement": "закупка"}
FULL_TRACK = "Full"
MAX_PRE_REGISTERED = 3
# Ключи Payload с материалами — это Dossier Files
DOSSIER_KEYS = (
    "Materials",  # все материалы одним вопросом; ключи ниже — у заявок до 22.10
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
        if _empty(value):
            continue
        current = project.get(fid)
        if _empty(current) or (fid in _OVERWRITE and value != current):
            updates[fid] = value
        elif fid == Projects.PPR_TRACK and value == FULL_TRACK and current != FULL_TRACK:
            updates[fid] = value  # трек только вверх: вырос объём — Full; вниз — никогда
    links = dossier_links(payload)
    have = str(project.get(Projects.DOSSIER_LINKS) or "").splitlines()
    new_links = [u for u in links if u not in have]
    if new_links:
        updates[Projects.DOSSIER_LINKS] = "\n".join([*(h for h in have if h.strip()), *new_links])
    if links or str(project.get(Projects.DOSSIER_LINKS) or "").strip():
        provided.add(Projects.DOSSIER_FILES)  # ссылки на материалы засчитываются как досье
    order = payload.get("Order Month")
    if isinstance(order, str) and _ISO_DATE.fullmatch(order) and _empty(project.get(Projects.EXPECTED_ORDER_DATE)):
        updates[Projects.EXPECTED_ORDER_DATE] = order
    # Месяц размещения заказа на регистрации — самый свежий: заменяет прежний
    final = payload.get("Order Month Final")
    if isinstance(final, str) and _ISO_DATE.fullmatch(final) and final != project.get(Projects.EXPECTED_ORDER_DATE):
        updates[Projects.EXPECTED_ORDER_DATE] = final
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


OWN_DESIGN = "Собственная проработка"


COMPANY_PICKS = "_Company Picks"


def partner_for(reg: dict, project: dict | None = None) -> str | None:
    """Партнёр проекта для заявок с общего профиля AW: по стране объекта (иначе None)."""
    if not set(reg.get(Registrations.SUBMITTED_BY) or []) & Partners.SENDERS:
        return None
    country = payload_of(reg).get("Country") or (project or {}).get(Projects.COUNTRY)
    return Partners.BY_COUNTRY.get(str(country or ""))


def company_links(reg: dict, project: dict, dealer: str | None) -> list[dict]:
    """Связи проекта с Companies по заявке: End Client, Specifier, DEALER, PARTNER.

    Каждая запись: {"field", "name" (найти или создать по названию) или "company_id", "type"}.
    Заполненные End Client / Specifier не трогаем; DEALER — дописываем компанию дилера.
    """
    payload = payload_of(reg)
    links: list[dict] = []
    picked = payload.get(COMPANY_PICKS) or {}   # выбор проверяющего: {ключ анкеты: id компании или "new"}

    def named(field: str, key: str, name: str, ctype: str) -> dict:
        choice = picked.get(key)
        if choice and choice != "new":
            return {"field": field, "company_id": choice}
        link = {"field": field, "name": name, "type": ctype}
        if choice == "new":
            link["new"] = True
        return link

    client = str(payload.get("End Client") or "").strip()
    if client and _empty(project.get(Projects.END_CLIENT)):
        links.append(named(Projects.END_CLIENT, "End Client", client, "End Client"))
    spec = str(payload.get("Specifier") or "").strip()
    if spec and _empty(project.get(Projects.SPECIFIER)):
        if spec == OWN_DESIGN and dealer:
            links.append({"field": Projects.SPECIFIER, "company_id": dealer})  # проектирует сам дилер
        elif spec != OWN_DESIGN:
            solo = payload.get("Specifier Type") == "Частный архитектор"
            links.append(named(Projects.SPECIFIER, "Specifier", spec, "Architect" if solo else "Design Studio"))
    if dealer and dealer not in (project.get(Projects.DEALER) or []):
        links.append({"field": Projects.DEALER, "company_id": dealer, "append": True})
    partner = partner_for(reg, project)
    if partner and _empty(project.get(Projects.PARTNER)):
        links.append({"field": Projects.PARTNER, "company_id": partner})
    return links


def dossier_links(payload: dict) -> list[str]:
    """Ссылки на материалы и подтверждения из заявки (файлы идут вложениями отдельно)."""
    out: list[str] = []
    for key in (*DOSSIER_KEYS, "Contact Evidence"):
        items = payload.get(key)
        for item in items if isinstance(items, list) else []:
            url = item.get("url") if isinstance(item, dict) and item.get("kind") == "link" else None
            if url and url not in out:
                out.append(url)
    return out


def change_lines(project: dict, moved: dict) -> list[str]:
    """«💶 Объём EXW: 18 000 → 24 750 €» — что заявка заменила в проекте (для проверяющего и лога)."""
    lines = []
    for fid, label in _OVERWRITE.items():
        old = project.get(fid)
        if fid in moved and not _empty(old):
            new = moved[fid]
            if fid == Projects.EXW_POTENTIAL:
                fmt = lambda v: f"{float(v):,.0f} €".replace(",", " ")  # noqa: E731
            else:
                fmt = lambda v: STAGE_RU.get(v, v)  # noqa: E731
            lines.append(f"{label}: {fmt(old)} → {fmt(new)}")
    old_order = project.get(Projects.EXPECTED_ORDER_DATE)
    if Projects.EXPECTED_ORDER_DATE in moved and not _empty(old_order):
        fmt = lambda v: f"{str(v)[5:7]}.{str(v)[:4]}"  # noqa: E731
        lines.append(f"📅 Размещение заказа: {fmt(old_order)} → {fmt(moved[Projects.EXPECTED_ORDER_DATE])}")
    if Projects.PPR_TRACK in moved and not _empty(project.get(Projects.PPR_TRACK)):
        lines.append("📈 Схема: упрощённая → полная (объём больше 30 000 € по прайсу)")
    return lines


def _log_extras(project: dict, moved: dict, payload: dict) -> list[str]:
    links = [f"Материалы (ссылка): {u}" for u in dossier_links(payload)]
    return [ln.split(" ", 1)[1] for ln in change_lines(project, moved)] + links


def ppr_missing(project: dict, status: str, filled: dict | None = None, provided: set | None = None) -> list[str]:
    """Чего не хватает для Pre-registered / Registered — с учётом того, что дал дилер в заявке."""
    if status not in (PPR_PRE, PPR_REG):
        return []
    filled = filled or {}
    provided = provided or set()

    def has(fid: str) -> bool:
        if fid == Projects.DOSSIER_FILES and not _empty(project.get(Projects.DOSSIER_LINKS)):
            return True
        return not _empty(project.get(fid)) or not _empty(filled.get(fid)) or fid in provided

    missing = [name for fid, name in _PPR_REQUIRED if not has(fid)]
    if status == PPR_REG:
        # Контакт с клиентом: для предрегистрации не обязателен, для регистрации — подтверждённый (не «Declared»)
        contact = filled.get(Projects.CONTACT_LEVEL) or project.get(Projects.CONTACT_LEVEL)
        if (_empty(contact) or contact == "Declared") and Projects.CONTACT_LEVEL not in provided:
            missing.append("Contact Level")
        if not has(Projects.QUOTATION):
            missing.append("Quotation / Proforma №")
    return missing


_FIELD_NAMES = {fid: key for key, fid in _PAYLOAD_TO_PROJECT}
_FIELD_NAMES.update({Projects.EXPECTED_ORDER_DATE: "Expected Order Date", Projects.DOSSIER_FILES: "Dossier Files",
                     Projects.DOSSIER_LINKS: "Dossier Links"})


FIELD_RU = {
    "Object Name": "объект", "Object Part": "часть объекта", "Country": "страна", "City": "город",
    "Object Address": "адрес", "Project Stage": "стадия", "EXW Potential": "объём", "AW Alternatives":
    "альтернативы Andreu World", "Contact Level": "уровень контакта с клиентом",
    "Parallel Registrations": "заявлено ли у других производителей", "Quotation / Proforma №": "номер квотации",
    "PPR Track": "схема", "Expected Order Date": "месяц размещения заказа", "Dossier Files": "материалы (файлы)",
    "Dossier Links": "материалы (ссылки)",
}


def _names(updates: dict) -> list[str]:
    return [FIELD_RU.get(_FIELD_NAMES.get(fid, ""), "другие данные") for fid in updates
            if fid not in (Projects.STATUS_CONFIRMED, Projects.NOTIFIED, Projects.PRE_REGISTERED, Projects.REGISTERED)]


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
    # Менеджеры (People), которым сигнал о новой предрегистрации на объекте
    broadcast_people: list[str] = field(default_factory=list)
    broadcast_text: str | None = None
    # Блокировка из-за недостающих данных: готовый текст запроса дилеру
    ask_text: str | None = None
    # Что заявка заменила в проекте: «💶 Объём EXW: 18 000 → 24 750 €»
    changes: list = field(default_factory=list)
    # Сообщать ли админу об этой блокировке (только первый раз)
    blocked: bool = False
    # Какие поля бот перенёс из заявки в проект — для сообщения админу
    transferred: list[str] = field(default_factory=list)
    # Кнопка «Ответить» под сообщением дилеру (запрос доп. информации)
    reply_button: bool = False
    # Связи с Companies, которые применяющий код найдёт/создаст (см. company_links)
    links: list[dict] = field(default_factory=list)
    # Строки заявки для Project Log (дописываются с датой и дилером)
    log_lines: list[str] = field(default_factory=list)
    # Для сводки админу
    status_label: str | None = None
    project_name: str | None = None
    # Запрос квотации по упрощённой схеме — условия считает применяющий код
    quote: bool = False
    # Отказ в пред/регистрации, плейсмент зафиксирован уведомлением
    declined_upgrade: bool = False
    # Почему решение не прошло (для кнопок проверяющего): no_project / no_status / missing / reg_taken /
    # pre_full / no_author / no_note
    block_kind: str = ""
    # dealer_text — готовый итоговый текст, карточкой не заменять (выигран / проигран)
    final_text: bool = False


def _note(reg: dict) -> str:
    note = (reg.get(Registrations.DECISION_NOTE) or "").strip()
    return f"\n\n💬 Комментарий: {escape(note)}" if note else ""


def name_ref(project: dict | None, fallback: str = "") -> str:
    """«PVSMART InsaitBC FL3 AW260250» — название и референс; без проекта — название из заявки."""
    if not project:
        return fallback or "без названия"
    return " ".join(x for x in ((project.get(Projects.NAME) or fallback or "").strip(),
                                str(project.get(Projects.PPR_REF) or "").strip()) if x)


def missing_names_ru(missing: list[str], detailed: bool = False) -> list[str]:
    """Имена полей из ppr_missing → по-русски."""
    out = []
    for key in missing:
        row = next((r for r in MISSING_RU if key.startswith(r[0])), None)
        out.append(row[2] if detailed else row[1] if row else "другие данные")
    return out


def _blocked(reg: dict, decision: str, kind: str, reason: str, ask_text: str | None = None,
             project: dict | None = None) -> Plan:
    name = name_ref(project, reg.get(Registrations.DEALER_PROJECT_NAME) or "")
    first_time = reg.get(Registrations.BOT_PROCESSED) != processed_mark(decision, blocked=True)
    return Plan(
        registration_updates={Registrations.BOT_PROCESSED: processed_mark(decision, blocked=True)},
        admin_text=f"🛠 ⚠️ Решение по «{escape(name)}» не прошло: {reason}" if first_time else None,
        blocked=True,
        ask_text=ask_text,
        block_kind=kind,
    )


def ask_text(status: str, missing: list[str], project_title: str = "") -> str:
    """Готовый запрос дилеру по списку недостающего (проверяющий видит его до отправки)."""
    where = f" по проекту {project_title}" if project_title else ""
    lines = [f"Чтобы дать {STATUS_LABEL[status]}{where}, не хватает:"]
    lines += [f"• {m}" for m in missing_names_ru(missing, detailed=True)]
    lines.append("Добавь через «Обновить проект» → «Дополнить материалы» или нажми «Ответить» под этим сообщением.")
    return "\n".join(lines)


def _decline_text(reg: dict, project: dict | None, note: str) -> str:
    """Отказ: что не принято и что можно сделать дальше."""
    title = name_ref(project, reg.get(Registrations.DEALER_PROJECT_NAME) or "")
    rtype = reg.get(Registrations.REQUEST_TYPE) or ""
    again = ("«Новый проект»" if rtype.startswith("New") else "«Обновить проект»")
    lines = [f"Заявка по проекту <code>{escape(title)}</code> не принята."]
    if note:
        lines.append(f"💬 Комментарий: {escape(note)}")
    lines.append(f"Если появятся новые данные — подай заново через {again}.")
    return "\n".join(lines)


def plan(reg: dict, project: dict | None) -> Plan:
    """Что сделать по решению Егора. reg и project — поля записей по id."""
    decision = reg.get(Registrations.DECISION)
    request_type = reg.get(Registrations.REQUEST_TYPE)
    dealer_name = reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия"
    note = (reg.get(Registrations.DECISION_NOTE) or "").strip()
    done = {Registrations.BOT_PROCESSED: processed_mark(decision)}

    if decision in (Decision.DECLINE, Decision.NEED_INFO):
        if not note and decision == Decision.NEED_INFO:   # отказ можно и без комментария
            return _blocked(reg, decision, "no_note", "для запроса информации нужен текст вопроса дилеру.",
                            project=project)
        if decision == Decision.DECLINE and request_type in UPGRADE_TYPES:
            # Отказ в пред/регистрации: любое обращение всё равно фиксируется уведомлением
            if project is None:
                return _blocked(
                    reg, decision, "no_project",
                    "не выбран проект. Отказ в повышении всё равно фиксирует плейсмент уведомлением — "
                    "выбери, в каком проекте.",
                )
            as_notice = {**reg, Registrations.DECISION: Decision.ACCEPT_MERGE,
                         Registrations.REG_STATUS: RegStatus.NOTIFICATION, Registrations.DECISION_NOTE: ""}
            pl = plan(as_notice, project)
            if pl.blocked:
                return pl
            pl.registration_updates = {**done, Registrations.REG_STATUS: RegStatus.NOTIFICATION}
            pl.declined_upgrade = True
            pl.dealer_text = _decline_text(reg, project, note)   # заменяется карточкой отказа
            return pl
        if decision == Decision.DECLINE:
            return Plan(
                registration_updates={**done, Registrations.REG_STATUS: RegStatus.DECLINED},
                dealer_text=_decline_text(reg, project, note),
                project_name=name_ref(project, dealer_name),
            )
        return Plan(
            registration_updates=done,
            dealer_text=(f"❓ По проекту <code>{escape(name_ref(project, dealer_name))}</code> "
                         f"нужна дополнительная информация:\n{escape(note)}\n\n"
                         "Ответь кнопкой «Ответить» — текстом, голосом или файлами."),
            reply_button=True,
        )

    if decision not in (Decision.ACCEPT, Decision.ACCEPT_MERGE):
        return Plan()

    if project is None:
        return _blocked(reg, decision, "no_project", "не выбран проект, куда принять заявку.")
    project_name = project.get(Projects.NAME) or dealer_name
    # Дилеру — объект его словами; имя в базе (латиницей) — в скобках, если отличается
    obj = f"«{escape(dealer_name)}»"
    if project_name.strip().lower() != dealer_name.strip().lower():
        obj += f" (в базе — {escape(project_name)})"
    # Ответ на запрос принимается вместе с исходной заявкой — дилеру второй раз не пишем
    is_reply = bool(payload_of(reg).get("In Reply To"))

    log_lines = [ln for ln in str(payload_of(reg).get("Project Log") or "").splitlines() if ln.strip()]

    # Запрос квотации по упрощённой схеме: статус не меняется, спецификация — в досье
    if request_type == RequestType.QUOTE_SIMPLIFIED:
        moved, _ = transfers(reg, project or {})
        quote_no = str(payload_of(reg).get("Quotation / Proforma №") or "").strip()
        reg_updates = dict(done)
        if quote_no:
            # Упрощённая схема: номер квотации от дилера = регистрация; дилер — в уведомлении проекта
            reg_updates[Registrations.QUOTE_NO] = quote_no
            reg_updates[Registrations.REG_STATUS] = RegStatus.NOTIFICATION
            managers_q = reg.get(Registrations.SUBMITTED_BY) or []
            p = project or {}
            on_project = set(p.get(Projects.NOTIFIED) or []) | set(p.get(Projects.PRE_REGISTERED) or []) \
                | set(p.get(Projects.REGISTERED) or [])
            if managers_q and managers_q[0] not in on_project:
                moved[Projects.NOTIFIED] = list(p.get(Projects.NOTIFIED) or []) + [managers_q[0]]
        return Plan(
            project_updates=moved,
            registration_updates=reg_updates,
            links=company_links(reg, project or {}, (reg.get(Registrations.DEALER) or [None])[0]) if quote_no else [],
            dealer_text=(f"По запросу {obj} квотацию № {escape(quote_no)} зафиксировали.{_note(reg)}" if quote_no
                         else f"По запросу {obj} спецификацию передали на квотацию.{_note(reg)}"),
            log_lines=log_lines + _log_extras(project or {}, moved, payload_of(reg)),
            project_name=project_name, quote=True, changes=change_lines(project or {}, moved),
        )

    # Выигран / проигран: итог записывается в проект (Status, Probability, номер заказа, причина)
    if request_type in (RequestType.UPDATE_WON, RequestType.UPDATE_LOST):
        return _won_lost(reg, project, request_type, done, log_lines)
    moved, provided = transfers(reg, project)
    changes = change_lines(project, moved)
    log_lines = log_lines + _log_extras(project, moved, payload_of(reg))
    dealers_ = reg.get(Registrations.DEALER) or []
    links = company_links(reg, project, dealers_[0] if dealers_ else None)
    if request_type in RequestType.INFO_UPDATES:
        return Plan(
            project_updates=moved,
            links=links,
            changes=changes,
            log_lines=log_lines,
            project_name=project_name,
            registration_updates=done,
            dealer_text=None if is_reply else f"По запросу {obj} информацию приняли и внесли в проект.{_note(reg)}",
            transferred=_names(moved),
        )

    reg_status = reg.get(Registrations.REG_STATUS)
    status = reg_status if reg_status in STATUS_FIELD else REQUEST_STATUS.get(request_type)
    if status is None:
        return _blocked(reg, decision, "no_status", "непонятно, какой статус дать.", project=project)

    # Статус — менеджеру (People из Submitted By), компания получает скидку через него
    managers = reg.get(Registrations.SUBMITTED_BY) or []
    if not managers:
        return _blocked(reg, decision, "no_author",
                        "в заявке не указан менеджер дилера — некому поставить статус.", project=project)
    dealer = managers[0]

    missing = ppr_missing(project, status, moved, provided)
    if missing:
        return _blocked(
            reg, decision, "missing",
            f"для статуса «{STATUS_RU[status]}» не хватает: "
            f"{escape(', '.join(missing_names_ru(missing)))} — ни в проекте, ни в заявке.",
            ask_text=ask_text(status, missing, name_ref(project, dealer_name)), project=project,
        )

    notified = list(project.get(Projects.NOTIFIED) or [])
    pre = list(project.get(Projects.PRE_REGISTERED) or [])
    registered = list(project.get(Projects.REGISTERED) or [])
    others_registered = [c for c in registered if c != dealer]
    others_pre = [c for c in pre if c != dealer]

    if status == PPR_REG and others_registered:
        return _blocked(reg, decision, "reg_taken",
                        "регистрация на проекте уже у другого партнёра — она бывает только у одного.",
                        project=project)

    updates: dict = dict(moved)
    broadcast: list[str] = []
    if status == PPR_NOTIFIED:
        if dealer not in notified + pre + registered:
            updates[Projects.NOTIFIED] = notified + [dealer]
    elif status == PPR_PRE:
        if dealer not in pre and len(pre) >= MAX_PRE_REGISTERED:
            return _blocked(reg, decision, "pre_full",
                            f"на проекте уже {len(pre)} предрегистрации — по правилам не больше трёх.",
                            project=project)
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

    # Новый статус (дилер впервые в этом поле) — срок отсчитывается заново с сегодняшнего дня
    entered = (status == PPR_PRE and dealer not in (project.get(Projects.PRE_REGISTERED) or [])) or \
        (status == PPR_REG and dealer not in (project.get(Projects.REGISTERED) or []))
    if status in (PPR_PRE, PPR_REG) and (entered or _empty(project.get(Projects.STATUS_CONFIRMED))):
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
                f"Ты на {pre.index(dealer) + 1}-м месте из {len(pre)} в предрегистрации.")
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
        links=links,
        log_lines=log_lines,
        changes=changes,
        status_label={PPR_NOTIFIED: "уведомление", PPR_PRE: "предрегистрация", PPR_REG: "регистрация"}[status],
        project_name=project_name,
        transferred=_names(moved),
        dealer_text=text + _note(reg),
        broadcast_people=broadcast,
        broadcast_text=(f"🔎 На объекте <code>{escape(name_ref(project, dealer_name))}</code> другой партнёр "
                        "получил предрегистрацию. Чтобы сохранить приоритет — дополни данные или запроси "
                        "предрегистрацию." if broadcast else None),
    )


STATUS_RU = {PPR_NOTIFIED: "уведомление", PPR_PRE: "предрегистрация", PPR_REG: "регистрация"}


def _won_lost(reg: dict, project: dict, request_type: str, done: dict, log_lines: list[str]) -> Plan:
    """Р6: выигран → Won, 100 %, номер заказа; проигран → Lost, 0 %, причина. Дилеру — короткое «Записали»."""
    payload = payload_of(reg)
    title = escape(name_ref(project, reg.get(Registrations.DEALER_PROJECT_NAME) or ""))
    updates: dict = {}
    lines = list(log_lines)
    if request_type == RequestType.UPDATE_WON:
        updates = {Projects.STATUS: "Won", Projects.PROBABILITY: "100"}
        order = str(payload.get("Order / Proforma №") or "").strip()
        if order:
            current = str(project.get(Projects.QUOTATION) or "").strip()
            if order not in current:
                updates[Projects.QUOTATION] = f"{current} / {order}" if current else order
            lines.append(f"Проект выигран, заказ / проформа № {order}")
        else:
            lines.append("Проект выигран")
        text = f"🏆 Записали: проект <code>{title}</code> выигран. Спасибо!"
        changes = ["🏆 Проект: выигран, вероятность 100 %" + (f", заказ № {escape(order)}" if order else "")]
    else:
        updates = {Projects.STATUS: "Lost", Projects.PROBABILITY: "0"}
        reason_code = payload.get("Lost Reason")
        reason = LOST_REASON_RU.get(reason_code) if reason_code else None
        if reason_code in LOST_REASON_RU:
            updates[Projects.LOST_REASON] = reason_code
        if not reason:
            free = [ln.split(": ", 1)[1] for ln in lines if ln.startswith("Причина проигрыша")]
            reason = free[0] if free else "не указана"
        if not any(ln.startswith("Причина проигрыша") for ln in lines):
            lines.append(f"Причина проигрыша: {reason}")
        text = f"Записали: проект <code>{title}</code> проигран, причина — {escape(reason)}."
        changes = [f"✖️ Проект: проигран, вероятность 0 %, причина — {escape(reason)}"]
    return Plan(project_updates=updates, registration_updates=done, dealer_text=text + _note(reg),
                final_text=True, log_lines=lines, changes=changes,
                project_name=name_ref(project, reg.get(Registrations.DEALER_PROJECT_NAME) or ""))


LOST_REASON_RU = {
    "Lost to equal competitor": "ушли к равному конкуренту",
    "Lost to cheaper offer": "ушли к более дешёвому предложению",
    "No budget": "нет бюджета",
    "Project cancelled": "проект отменён",
}
