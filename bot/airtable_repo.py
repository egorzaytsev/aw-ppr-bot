"""Доступ к Airtable. Синхронный pyairtable; из async-кода вызывать через asyncio.to_thread."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from pyairtable import Api

from .airtable_ids import Companies, People, Projects, Registrations, RegStatus, RequestType
from .state import DialogState

log = logging.getLogger(__name__)


def _quote(value: str) -> str:
    """Строковый литерал для formula Airtable: экранируем \\ и "."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# Статус дилера в проекте по программе, от старшего к младшему
PPR_REGISTERED, PPR_PRE_REGISTERED, PPR_NOTIFIED = "registered", "pre_registered", "notified"
_ROLE_FIELDS = (
    (PPR_REGISTERED, Companies.AS_REGISTERED),
    (PPR_PRE_REGISTERED, Companies.AS_PRE_REGISTERED),
    (PPR_NOTIFIED, Companies.AS_NOTIFIED),
)
# Закрытые проекты дилеру не показываем
_HIDDEN_PROJECT_STATUSES = {"Lost", "Won"}
# Что читаем из Projects: карточка + проверка пропусков для «Дополнить материалы».
# Чужих дилеров, Project Log, Analysis и баллы не читаем вообще.
_PROJECT_FIELDS = [
    Projects.NAME, Projects.STATUS, Projects.STATUS_EXPIRY, Projects.PPR_TRACK, Projects.PPR_MISSING,
    Projects.END_CLIENT, Projects.OBJECT_NAME, Projects.OBJECT_PART, Projects.COUNTRY, Projects.CITY,
    Projects.OBJECT_ADDRESS, Projects.PROJECT_STAGE, Projects.SPECIFIER, Projects.EXW_POTENTIAL,
    Projects.AW_ALTERNATIVES, Projects.EXPECTED_ORDER_DATE, Projects.CONTACT_LEVEL,
    Projects.PARALLEL_REGISTRATIONS, Projects.DOSSIER_FILES, Projects.QUOTATION,
    Projects.PPR_REF, Projects.PPR_NEXT_MISSING, Projects.PROJECT_LOG,
    # Только для проверки «есть ли регистрация на объекте» — имена дилерам не показываются
    Projects.REGISTERED,
]


@dataclass
class DealerProject:
    record_id: str
    name: str
    ppr_status: str
    status_expiry: str | None
    track: str | None
    # Сырые поля проекта по id (только из _PROJECT_FIELDS)
    fields: dict


@dataclass
class PendingRequest:
    record_id: str
    name: str
    request_type: str | None
    submitted_at: str | None


def _ids_formula(ids: list[str]) -> str:
    return "OR(" + ",".join(f"RECORD_ID()={_quote(i)}" for i in ids) + ")"


@dataclass
class Person:
    record_id: str
    name: str
    company_ids: list[str]
    chat_id: str | None
    state: DialogState | None
    reminder_slot: str | None = None


class AirtableRepo:
    def __init__(self, token: str, base_id: str) -> None:
        api = Api(token)
        self._people = api.table(base_id, People.TABLE)
        self._registrations = api.table(base_id, Registrations.TABLE)
        self._companies = api.table(base_id, Companies.TABLE)
        self._projects = api.table(base_id, Projects.TABLE)

    # ---------- People ----------

    def _to_person(self, record: dict) -> Person:
        f = record["fields"]
        return Person(
            record_id=record["id"],
            name=f.get(People.NAME, ""),
            company_ids=f.get(People.COMPANY, []),
            chat_id=f.get(People.TELEGRAM_CHAT_ID),
            state=DialogState.from_json(f.get(People.BOT_STATE)),
            reminder_slot=f.get(People.REMINDER_SLOT),
        )

    def _first(self, formula: str) -> Person | None:
        records = self._people.all(formula=formula, max_records=2, use_field_ids=True)
        if len(records) > 1:
            log.warning("People: больше одной записи по %s — беру первую", formula)
        return self._to_person(records[0]) if records else None

    def find_by_chat_id(self, chat_id: int) -> Person | None:
        return self._first(f"{{{People.TELEGRAM_CHAT_ID}}}={_quote(str(chat_id))}")

    def find_by_username(self, username: str) -> Person | None:
        uname = username.lstrip("@").lower()
        # В базе username могут записать с @ и в любом регистре.
        formula = (
            f"LOWER(SUBSTITUTE(TRIM({{{People.TELEGRAM_USERNAME}}}), \"@\", \"\"))"
            f"={_quote(uname)}"
        )
        return self._first(formula)

    def bind_chat_id(self, person_id: str, chat_id: int) -> None:
        self._people.update(person_id, {People.TELEGRAM_CHAT_ID: str(chat_id)})

    def set_reminder_slot(self, person_id: str, slot: str) -> None:
        self._people.update(person_id, {People.REMINDER_SLOT: slot})

    def save_state(self, person_id: str, state: DialogState | None) -> None:
        value = state.to_json() if state else ""
        self._people.update(person_id, {People.BOT_STATE: value})

    # ---------- Registrations ----------

    def create_registration(
        self,
        person: Person,
        request_type: str,
        payload: dict,
        dealer_project_name: str | None,
        target_project_id: str | None = None,
    ) -> str:
        fields = {
            Registrations.REG_STATUS: RegStatus.PENDING,
            Registrations.REQUEST_TYPE: request_type,
            Registrations.SUBMITTED_BY: [person.record_id],
            Registrations.PAYLOAD: json.dumps(payload, ensure_ascii=False, indent=2),
        }
        if person.company_ids:
            fields[Registrations.DEALER] = person.company_ids
        if dealer_project_name:
            fields[Registrations.DEALER_PROJECT_NAME] = dealer_project_name
        if target_project_id:
            fields[Registrations.TARGET_PROJECT] = [target_project_id]
        record = self._registrations.create(fields)
        return record["id"]

    def upload_attachment(
        self, record_id: str, filename: str, content: bytes, content_type: str | None = None
    ) -> None:
        self._registrations.upload_attachment(
            record_id, Registrations.ATTACHMENTS, filename, content, content_type
        )

    # ---------- проекты дилера ----------

    def _company_fields(self, company_ids: list[str]) -> list[dict]:
        if not company_ids:
            return []
        fields = [f for _, f in _ROLE_FIELDS] + [Companies.REGISTRATIONS]
        recs = self._companies.all(formula=_ids_formula(company_ids), fields=fields, use_field_ids=True)
        return [r["fields"] for r in recs]

    def dealer_projects(self, company_ids: list[str]) -> list[DealerProject]:
        """Проекты, где компания дилера стоит в Notified / Pre-registered / Registered."""
        status_by_project: dict[str, str] = {}
        for f in self._company_fields(company_ids):
            for status, field in _ROLE_FIELDS:
                for pid in f.get(field, []):
                    status_by_project.setdefault(pid, status)  # старший статус побеждает
        if not status_by_project:
            return []
        recs = self._projects.all(
            formula=_ids_formula(list(status_by_project)),
            fields=_PROJECT_FIELDS,
            use_field_ids=True,
        )
        out = []
        for r in recs:
            f = r["fields"]
            if f.get(Projects.STATUS) in _HIDDEN_PROJECT_STATUSES:
                continue
            out.append(DealerProject(
                record_id=r["id"],
                name=(f.get(Projects.NAME) or "").strip() or "Без названия",
                ppr_status=status_by_project[r["id"]],
                status_expiry=f.get(Projects.STATUS_EXPIRY),
                track=f.get(Projects.PPR_TRACK),
                fields=f,
            ))
        out.sort(key=lambda p: p.name.lower())
        return out

    def pending_requests(self, company_ids: list[str]) -> list[PendingRequest]:
        """Заявки компании дилера, которые ещё на проверке."""
        reg_ids = [rid for f in self._company_fields(company_ids) for rid in f.get(Companies.REGISTRATIONS, [])]
        if not reg_ids:
            return []
        recs = self._registrations.all(
            formula=f"AND({_ids_formula(reg_ids)}, {{{Registrations.REG_STATUS}}}={_quote(RegStatus.PENDING)})",
            fields=[Registrations.DEALER_PROJECT_NAME, Registrations.REQUEST_TYPE, Registrations.SUBMITTED_AT],
            use_field_ids=True,
        )
        out = [
            PendingRequest(
                record_id=r["id"],
                name=r["fields"].get(Registrations.DEALER_PROJECT_NAME) or "Без названия",
                request_type=r["fields"].get(Registrations.REQUEST_TYPE),
                submitted_at=r["fields"].get(Registrations.SUBMITTED_AT),
            )
            for r in recs
        ]
        out.sort(key=lambda p: p.submitted_at or "", reverse=True)
        return out

    # ---------- решения Егора ----------

    def decisions_to_process(self) -> list[dict]:
        """Заявки с Decision, которые бот ещё не довёл до конца (done:<Decision>)."""
        formula = (
            f"AND({{{Registrations.DECISION}}}!='', "
            f"{{{Registrations.BOT_PROCESSED}}}!=CONCATENATE('done:', {{{Registrations.DECISION}}}), "
            f"LEFT({{{Registrations.BOT_PROCESSED}}}, 6)!='await:')"  # админ ещё вводит текст
        )
        return self._registrations.all(formula=formula, use_field_ids=True)

    def registration_fields(self, reg_id: str) -> dict:
        return self._registrations.get(reg_id, use_field_ids=True)["fields"]

    def project_fields(self, project_id: str) -> dict:
        return self._projects.get(project_id, use_field_ids=True)["fields"]

    def update_project(self, project_id: str, fields: dict) -> None:
        self._projects.update(project_id, fields)

    def update_registration(self, reg_id: str, fields: dict) -> None:
        self._registrations.update(reg_id, fields)

    def chat_ids_for_people(self, person_ids: list[str]) -> list[str]:
        if not person_ids:
            return []
        recs = self._people.all(formula=_ids_formula(person_ids), fields=[People.TELEGRAM_CHAT_ID],
                                use_field_ids=True)
        return [r["fields"][People.TELEGRAM_CHAT_ID] for r in recs if r["fields"].get(People.TELEGRAM_CHAT_ID)]

    def chat_ids_for_companies(self, company_ids: list[str]) -> list[str]:
        """Telegram всех людей компаний (менеджеры дилера)."""
        if not company_ids:
            return []
        recs = self._companies.all(formula=_ids_formula(company_ids), fields=[Companies.PEOPLE], use_field_ids=True)
        people = sorted({pid for r in recs for pid in r["fields"].get(Companies.PEOPLE, [])})
        return self.chat_ids_for_people(people)

    # ---------- решения из Telegram (админ) ----------

    def pending_without_decision(self, limit: int = 10) -> list[dict]:
        """На проверке: без решения или с запросом доп. информации (ждут окончательного решения)."""
        formula = (f"AND({{{Registrations.REG_STATUS}}}={_quote(RegStatus.PENDING)}, "
                   f"OR({{{Registrations.DECISION}}}='', {{{Registrations.DECISION}}}={_quote('Need more info')}))")
        return self._registrations.all(formula=formula, use_field_ids=True, max_records=limit,
                                       sort=["-" + Registrations.SUBMITTED_AT])

    def set_decision(self, reg_id: str, decision: str, note: str | None = None,
                     target_project_id: str | None = None) -> None:
        fields: dict = {Registrations.DECISION: decision}
        if note is not None:
            fields[Registrations.DECISION_NOTE] = note
        if target_project_id:
            fields[Registrations.TARGET_PROJECT] = [target_project_id]
        self._registrations.update(reg_id, fields)

    def close_reply(self, reg_id: str, target_project_id: str | None) -> None:
        """Ответ дилера на запрос — закрыть вместе с исходной заявкой.

        Есть проект — принимаем в него (бот перенесёт данные, дилеру не пишет); нет — снимаем с проверки.
        """
        if target_project_id:
            self.set_decision(reg_id, "Accept — merge into Target Project", None, target_project_id)
        else:
            self._registrations.update(reg_id, {Registrations.REG_STATUS: "Cancelled"})

    def create_project(self, name: str) -> str:
        # Новый проект из заявки — Prospecting, вероятность 0; дальше двигает Егор
        rec = self._projects.create({Projects.NAME: name, Projects.STATUS: "Prospecting", Projects.PROBABILITY: "0"})
        return rec["id"]

    # ---------- напоминания ----------

    def people_for_reminders(self) -> list[dict]:
        formula = (f"AND({{{People.TELEGRAM_CHAT_ID}}}!='', {{{People.REMINDER_SLOT}}}!='', "
                   f"COUNTA({{{People.COMPANY}}})>0)")
        fields = [People.NAME, People.COMPANY, People.TELEGRAM_CHAT_ID, People.REMINDER_SLOT,
                  People.LAST_REMINDER, People.COUNTRY]
        return self._people.all(formula=formula, fields=fields, use_field_ids=True)

    def set_last_reminder(self, person_id: str, day: str) -> None:
        self._people.update(person_id, {People.LAST_REMINDER: day})

    def last_news_by_project(self, company_ids: list[str]) -> dict:
        """Последняя заявка компании по каждому проекту (Target Project) — дата YYYY-MM-DD."""
        reg_ids = [rid for f in self._company_fields(company_ids) for rid in f.get(Companies.REGISTRATIONS, [])]
        if not reg_ids:
            return {}
        out: dict = {}
        for i in range(0, len(reg_ids), 50):  # длина формулы
            recs = self._registrations.all(
                formula=_ids_formula(reg_ids[i:i + 50]),
                fields=[Registrations.TARGET_PROJECT, Registrations.SUBMITTED_AT], use_field_ids=True,
            )
            for r in recs:
                at = (r["fields"].get(Registrations.SUBMITTED_AT) or "")[:10]
                for pid in r["fields"].get(Registrations.TARGET_PROJECT) or []:
                    if at and at > out.get(pid, ""):
                        out[pid] = at
        return out

    # ---------- поиск дублей ----------

    _DEDUP_TTL = 600  # секунд: проекты для сравнения кэшируем

    def dedup_projects(self) -> list[dict]:
        cached = getattr(self, "_dedup_cache", None)
        if cached and time.monotonic() - cached[0] < self._DEDUP_TTL:
            return cached[1]
        names = {r["id"]: r["fields"].get(Companies.NAME, "")
                 for r in self._companies.all(fields=[Companies.NAME], use_field_ids=True)}
        recs = self._projects.all(
            fields=[Projects.NAME, Projects.OBJECT_NAME, Projects.OBJECT_ADDRESS, Projects.END_CLIENT],
            use_field_ids=True,
        )
        projects = [{
            "id": r["id"],
            "name": (r["fields"].get(Projects.NAME) or "").strip(),
            "object": r["fields"].get(Projects.OBJECT_NAME) or "",
            "address": r["fields"].get(Projects.OBJECT_ADDRESS) or "",
            "clients": [names.get(c, "") for c in r["fields"].get(Projects.END_CLIENT) or []],
        } for r in recs]
        self._dedup_cache = (time.monotonic(), projects)
        return projects

    def set_analysis(self, reg_id: str, text: str) -> None:
        self._registrations.update(reg_id, {Registrations.ANALYSIS: text})

    # ---------- компании ----------

    def _company_names(self) -> dict:
        cached = getattr(self, "_companies_cache", None)
        if cached and time.monotonic() - cached[0] < self._DEDUP_TTL:
            return cached[1]
        names = {r["id"]: r["fields"].get(Companies.NAME, "")
                 for r in self._companies.all(fields=[Companies.NAME], use_field_ids=True)}
        self._companies_cache = (time.monotonic(), names)
        return names

    def find_or_create_company(self, name: str, ctype: str, country: str | None = None) -> tuple[str, bool]:
        """Компания по названию (с учётом транслитерации); нет — создаём. (id, создана ли)."""
        from . import dedup  # без цикла импортов
        found = dedup.match_company(name, self._company_names())
        if found:
            return found, False
        fields: dict = {Companies.NAME: name.strip(), Companies.TYPE: ctype}
        if country:
            fields[Companies.COUNTRY] = [country]
        rec = self._companies.create(fields, typecast=True)
        self._company_names()[rec["id"]] = name.strip()
        return rec["id"], True

    def company_name(self, company_id: str) -> str:
        return self._company_names().get(company_id) or company_id

    # ---------- сводка для админа ----------

    def admin_stats(self) -> dict:
        pending = self._registrations.all(
            formula=f"AND({{{Registrations.REG_STATUS}}}={_quote(RegStatus.PENDING)}, "
                    f"OR({{{Registrations.DECISION}}}='', {{{Registrations.DECISION}}}={_quote('Need more info')}))",
            fields=[Registrations.DECISION], use_field_ids=True)
        blocked = self._registrations.all(
            formula=f"LEFT({{{Registrations.BOT_PROCESSED}}}, 8)='blocked:'",
            fields=[Registrations.DEALER_PROJECT_NAME], use_field_ids=True)
        people = self._people.all(
            formula=f"AND({{{People.TELEGRAM_CHAT_ID}}}!='', COUNTA({{{People.COMPANY}}})>0)",
            fields=[People.NAME, People.REMINDER_SLOT, People.LAST_REMINDER], use_field_ids=True)
        return {
            "no_decision": sum(1 for r in pending if not r["fields"].get(Registrations.DECISION)),
            "need_info": sum(1 for r in pending if r["fields"].get(Registrations.DECISION)),
            "blocked": [r["fields"].get(Registrations.DEALER_PROJECT_NAME) or r["id"] for r in blocked],
            "people": [(r["fields"].get(People.NAME, ""), r["fields"].get(People.REMINDER_SLOT),
                        r["fields"].get(People.LAST_REMINDER)) for r in people],
        }

    def person_name(self, person_id: str) -> str:
        try:
            return self._people.get(person_id, use_field_ids=True)["fields"].get(People.NAME, "")
        except Exception:
            return ""

    # ---------- ввод админа, переживающий перезапуск ----------

    AWAIT_TTL = 6 * 3600  # секунд: забытый ввод не подхватываем

    def set_awaiting(self, reg_id: str, action: str, reply_id: str = "") -> None:
        mark = f"await:{action}:{reply_id or '-'}:{int(time.time())}"
        self._registrations.update(reg_id, {Registrations.BOT_PROCESSED: mark})

    def find_awaiting(self) -> tuple | None:
        """(действие, id заявки, id ответа) — последний незавершённый ввод админа."""
        recs = self._registrations.all(
            formula=f"LEFT({{{Registrations.BOT_PROCESSED}}}, 6)='await:'",
            fields=[Registrations.BOT_PROCESSED], use_field_ids=True,
        )
        best = None
        for r in recs:
            parts = r["fields"][Registrations.BOT_PROCESSED].split(":")
            if len(parts) < 4 or not parts[3].isdigit():
                continue
            ts = int(parts[3])
            if time.time() - ts > self.AWAIT_TTL:
                continue
            if not best or ts > best[0]:
                best = (ts, parts[1], r["id"], "" if parts[2] == "-" else parts[2])
        return best[1:] if best else None

    def clear_awaiting(self, reg_id: str) -> None:
        self._registrations.update(reg_id, {Registrations.BOT_PROCESSED: ""})

    # ---------- квотации по упрощённой схеме ----------

    def quote_requests(self, project_id: str) -> list[dict]:
        """Принятые запросы квотации по проекту: [{"id", "dealers", "valid", "no", "at"}], старые первыми."""
        formula = (f"AND({{{Registrations.REQUEST_TYPE}}}={_quote(RequestType.QUOTE_SIMPLIFIED)}, "
                   f"LEFT({{{Registrations.BOT_PROCESSED}}}, 5)='done:')")
        recs = self._registrations.all(
            formula=formula,
            fields=[Registrations.TARGET_PROJECT, Registrations.DEALER, Registrations.QUOTE_VALID,
                    Registrations.QUOTE_NO, Registrations.SUBMITTED_AT],
            use_field_ids=True,
        )
        out = [{
            "id": r["id"], "dealers": r["fields"].get(Registrations.DEALER) or [],
            "valid": r["fields"].get(Registrations.QUOTE_VALID), "no": r["fields"].get(Registrations.QUOTE_NO),
            "at": r["fields"].get(Registrations.SUBMITTED_AT) or "",
        } for r in recs if project_id in (r["fields"].get(Registrations.TARGET_PROJECT) or [])]
        return sorted(out, key=lambda q: q["at"])

    def dealer_quotes(self, company_ids: list[str]) -> dict:
        """Последний принятый запрос квотации компании по каждому проекту: {project: {...}}."""
        reg_ids = [rid for f in self._company_fields(company_ids) for rid in f.get(Companies.REGISTRATIONS, [])]
        out: dict = {}
        for i in range(0, len(reg_ids), 50):
            recs = self._registrations.all(
                formula=(f"AND({_ids_formula(reg_ids[i:i + 50])}, "
                         f"{{{Registrations.REQUEST_TYPE}}}={_quote(RequestType.QUOTE_SIMPLIFIED)})"),
                fields=[Registrations.TARGET_PROJECT, Registrations.QUOTE_VALID, Registrations.QUOTE_NO,
                        Registrations.SUBMITTED_AT, Registrations.BOT_PROCESSED],
                use_field_ids=True,
            )
            for r in recs:
                f = r["fields"]
                q = {"valid": f.get(Registrations.QUOTE_VALID), "no": f.get(Registrations.QUOTE_NO),
                     "at": (f.get(Registrations.SUBMITTED_AT) or "")[:10],
                     "accepted": (f.get(Registrations.BOT_PROCESSED) or "").startswith("done:")}
                for pid in f.get(Registrations.TARGET_PROJECT) or []:
                    if q["at"] >= out.get(pid, {}).get("at", ""):
                        out[pid] = q
        return out

    def append_project_log(self, project_id: str, existing: str | None, header: str, lines: list[str]) -> None:
        block = header + "\n" + "\n".join(lines)
        text = f"{existing.rstrip()}\n\n{block}" if (existing or "").strip() else block
        self._projects.update(project_id, {Projects.PROJECT_LOG: text})
