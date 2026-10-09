"""Доступ к Airtable. Синхронный pyairtable; из async-кода вызывать через asyncio.to_thread."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from pyairtable import Api

from .airtable_ids import Companies, People, Projects, Registrations, RegStatus
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
