"""/preview {сценарий} — проверяющему все сообщения сценария с тестовыми данными, ровно как они будут выглядеть.

Сценарий прогоняется через настоящие обработчики бота (карточки, кнопки, решения), но с базой в памяти
(PreviewRepo) и «ботом», который не отправляет, а записывает сообщения. Потом записанное приходит
проверяющему по порядку с подписью «Дилеру» / «Тебе». Кнопки в просмотре не работают.
Ничего не пишется в Airtable и никому, кроме проверяющего, не уходит.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
from datetime import date, datetime, timedelta, timezone
from html import escape
from types import SimpleNamespace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from .airtable_ids import Decision, Projects, Registrations, RegStatus, RequestType
from .airtable_repo import DealerProject, Person

log = logging.getLogger(__name__)

CB_PREVIEW = "pv:"   # кнопки просмотра — ничего не делают
SCENARIOS = {
    "new": "Новый проект → проверка по шагам → карточки",
    "upgrade": "Запрос предрегистрации / регистрации",
    "fill": "Дополнить материалы",
    "news": "Новость и запрос информации",
    "quote": "Квотация по упрощённой схеме",
    "won": "Проект выигран",
    "lost": "Проект проигран",
    "call": "Звонок: подтвердить, другое время, не нужен",
    "expiry": "Истёк срок статуса, «условия выполнены»",
    "rollback": "Откат статуса",
    "unknown": "Незнакомый человек",
    "errors": "Ошибки и застрявшие решения",
    "digest": "Утренняя сводка и напоминание дилеру",
}

ADMIN, DEALER, OTHER = 900001, 900002, 900003
DEALER_ID, OTHER_ID, COMPANY, OTHER_CO = "recPrevDealer00001", "recPrevOther000001", "recPrevCompany0001", "recPrevCompany0002"
PROJECT_ID = "recPrevProject0001"
TODAY = date.today()


def _iso(d: date) -> str:
    return d.isoformat()


class PreviewRepo:
    """База в памяти: те же методы, что у AirtableRepo, которые зовут обработчики."""

    def __init__(self) -> None:
        self.people = {
            DEALER_ID: {"name": "Ирина Тестова", "companies": [COMPANY], "chat": str(DEALER), "country": "Russia"},
            OTHER_ID: {"name": "Павел Пробный", "companies": [OTHER_CO], "chat": str(OTHER), "country": "Russia"},
        }
        self.companies = {COMPANY: ("Тест Мебель", "Partner"), OTHER_CO: ("Другая Мебель", "Partner"),
                          "recPrevClient00001": ("Тест Банк", "End Client"),
                          "recPrevBureau00001": ("Бюро Пример", "Design Studio")}
        self.projects = {PROJECT_ID: {
            Projects.NAME: "TESTBANK Office FL3", Projects.PPR_REF: "AW269999", Projects.STATUS: "Active",
            Projects.END_CLIENT: ["recPrevClient00001"], Projects.SPECIFIER: ["recPrevBureau00001"],
            Projects.OBJECT_NAME: "Офис на Лесной", Projects.CITY: "Moscow", Projects.COUNTRY: "Russia",
            Projects.PROJECT_STAGE: "Design", Projects.EXW_POTENTIAL: 40500, Projects.PPR_TRACK: "Full",
            Projects.EXPECTED_ORDER_DATE: _iso(TODAY.replace(day=1) + timedelta(days=95)),
            Projects.AW_ALTERNATIVES: "No — AW only", Projects.PARALLEL_REGISTRATIONS: "Нет",
            Projects.NOTIFIED: [DEALER_ID], Projects.PRE_REGISTERED: [OTHER_ID],
            Projects.PPR_NEXT_MISSING: "Object Address; Dossier Files;",
            Projects.STATUS_CONFIRMED: _iso(TODAY - timedelta(days=40)),
            Projects.STATUS_EXPIRY: _iso(TODAY - timedelta(days=9)),
            Projects.PROJECT_LOG: "01.10.2026 · Тест Мебель — ответы из заявки партнёра:\nОбъём по прайсу: 90 000 €",
        }}
        self.regs: dict[str, dict] = {}
        self.memo: dict = {}
        self._n = 0

    # --- заявки ---
    def add_reg(self, fields: dict) -> str:
        self._n += 1
        rid = f"recPrevReg{self._n:08d}"
        base = {Registrations.SUBMITTED_BY: [DEALER_ID], Registrations.DEALER: [COMPANY],
                Registrations.REG_STATUS: RegStatus.PENDING,
                Registrations.SUBMITTED_AT: (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()}
        self.regs[rid] = {**base, **fields}
        return rid

    def create_registration(self, person, request_type, payload, name, target=None) -> str:
        fields = {Registrations.REQUEST_TYPE: request_type, Registrations.SUBMITTED_BY: [person.record_id],
                  Registrations.DEALER: person.company_ids, Registrations.DEALER_PROJECT_NAME: name,
                  Registrations.PAYLOAD: json.dumps(payload, ensure_ascii=False)}
        if target:
            fields[Registrations.TARGET_PROJECT] = [target]
        return self.add_reg(fields)

    def registration_fields(self, rid):
        return self.regs[rid]

    def update_registration(self, rid, fields):
        self.regs[rid].update({k: v for k, v in fields.items()})

    def set_decision(self, rid, decision, note=None, target=None):
        self.regs[rid][Registrations.DECISION] = decision
        if note is not None:
            self.regs[rid][Registrations.DECISION_NOTE] = note
        if target:
            self.regs[rid][Registrations.TARGET_PROJECT] = [target]

    def set_awaiting(self, rid, action, reply_id="", chat_id=None):
        self.regs[rid][Registrations.BOT_PROCESSED] = f"await:{action}:{reply_id or '-'}:0:{chat_id or '-'}"

    def clear_awaiting(self, rid):
        self.regs[rid][Registrations.BOT_PROCESSED] = ""

    def find_awaiting(self, chat_id=None):
        return None

    def close_reply(self, rid, target):
        if target:
            self.set_decision(rid, Decision.ACCEPT_MERGE, None, target)

    def set_analysis(self, rid, text):
        self.regs[rid][Registrations.ANALYSIS] = text

    def decisions_to_process(self):
        out = []
        for rid, f in self.regs.items():
            d = f.get(Registrations.DECISION)
            mark = str(f.get(Registrations.BOT_PROCESSED) or "")
            if d and mark != f"done:{d}" and not mark.startswith("await:"):
                out.append({"id": rid, "fields": f})
        return out

    def reg_status_events(self):
        return []

    def pending_without_decision(self, limit=30):
        return [{"id": k, "fields": v} for k, v in self.regs.items()
                if not v.get(Registrations.DECISION) and v.get(Registrations.REQUEST_TYPE) != RequestType.UNKNOWN_USER]

    undecided = pending_without_decision

    def blocked_registrations(self):
        return [{"id": k, "fields": v} for k, v in self.regs.items()
                if str(v.get(Registrations.BOT_PROCESSED) or "").startswith("blocked:")]

    def recent_calls(self, days=14):
        return [{"id": k, "fields": v} for k, v in self.regs.items() if "Call Slot" in str(v.get(Registrations.PAYLOAD))]

    def open_unknown_users(self, chat_id=None):
        return [{"id": k, "fields": v} for k, v in self.regs.items()
                if v.get(Registrations.REQUEST_TYPE) == RequestType.UNKNOWN_USER and not v.get(Registrations.BOT_PROCESSED)]

    def create_unknown_user(self, chat_id, username, full_name):
        return self.add_reg({Registrations.REQUEST_TYPE: RequestType.UNKNOWN_USER, Registrations.SUBMITTED_BY: [],
                             Registrations.PAYLOAD: json.dumps({"chat_id": chat_id, "username": username,
                                                                "name": full_name})})

    def mark_processed(self, rid, mark, closed=True):
        self.regs[rid][Registrations.BOT_PROCESSED] = mark

    def admin_stats(self):
        pending = self.pending_without_decision()
        return {"no_decision": len(pending), "need_info": 1,
                "blocked": [v.get(Registrations.DEALER_PROJECT_NAME) for v in
                            (r["fields"] for r in self.blocked_registrations())],
                "people": [(p["name"], "11:00–14:00", _iso(TODAY)) for p in self.people.values()]}

    # --- проекты ---
    def project_fields(self, pid):
        return self.projects[pid]

    def update_project(self, pid, fields):
        self.projects[pid].update(fields)
        self._recalc(pid)

    def _recalc(self, pid):
        p = self.projects[pid]
        if p.get(Projects.REGISTERED):
            p[Projects.PPR_NEXT_MISSING] = ""
        if p.get(Projects.OBJECT_ADDRESS) and p.get(Projects.DOSSIER_LINKS):
            p[Projects.PPR_NEXT_MISSING] = ""

    def create_project(self, name):
        pid = f"recPrevProject{len(self.projects) + 1:04d}"
        self.projects[pid] = {Projects.NAME: name, Projects.PPR_REF: f"AW26{9990 + len(self.projects)}",
                              Projects.STATUS: "Prospecting", Projects.PPR_TRACK: "Full",
                              Projects.PPR_NEXT_MISSING: "Expected Order Date; Object Address; Dossier Files;"}
        return pid

    def append_project_log(self, pid, existing, header, lines):
        self.projects[pid][Projects.PROJECT_LOG] = (existing or "") + "\n\n" + header + "\n" + "\n".join(lines)

    def dedup_projects(self):
        return [{"id": k, "name": v.get(Projects.NAME, ""), "ref": v.get(Projects.PPR_REF, ""),
                 "object": v.get(Projects.OBJECT_NAME, ""), "address": v.get(Projects.OBJECT_ADDRESS, ""),
                 "clients": [self.companies.get(c, ("",))[0] for c in v.get(Projects.END_CLIENT) or []]}
                for k, v in self.projects.items()]

    def expired_statuses(self):
        return [{"id": k, "fields": v} for k, v in self.projects.items() if v.get(Projects.STATUS_EXPIRY, "9") < _iso(TODAY)]

    def ready_for_upgrade(self):
        return [{"id": k, "fields": v} for k, v in self.projects.items() if not v.get(Projects.PPR_NEXT_MISSING)
                and not v.get(Projects.REGISTERED)]

    def projects_with_managers(self):
        return [{"id": k, "fields": v} for k, v in self.projects.items()]

    def quote_requests(self, pid):
        return []

    def dealer_projects(self, person_id):
        out = []
        for k, f in self.projects.items():
            for status, field in (("registered", Projects.REGISTERED), ("pre_registered", Projects.PRE_REGISTERED),
                                  ("notified", Projects.NOTIFIED)):
                if person_id in (f.get(field) or []):
                    out.append(DealerProject(k, f.get(Projects.NAME, ""), status, f.get(Projects.STATUS_EXPIRY),
                                             f.get(Projects.PPR_TRACK), f))
                    break
        return out

    def dealer_quotes(self, person_id):
        return {}

    def last_news_by_project(self, person_id):
        return {PROJECT_ID: _iso(TODAY - timedelta(days=30))}

    def pending_requests(self, person_id):
        return []

    # --- люди и компании ---
    def person_name(self, pid):
        return self.people.get(pid, {}).get("name", "")

    def person_companies(self, pid):
        return self.people.get(pid, {}).get("companies", [])

    def _person_fields(self, pid):
        from .airtable_ids import People
        return {People.COUNTRY: self.people.get(pid, {}).get("country")}

    def chat_ids_for_people(self, ids):
        return [self.people[i]["chat"] for i in ids if i in self.people]

    def chat_ids_for_companies(self, ids):
        return [p["chat"] for p in self.people.values() if set(p["companies"]) & set(ids)]

    def company_name(self, cid):
        return self.companies.get(cid, ("компания",))[0]

    def company_types(self, ids):
        return [self.companies.get(c, ("", ""))[1] for c in ids]

    def company_candidates(self, name):
        return []

    def find_or_create_company(self, name, ctype, country=None, force_new=False):
        cid = f"recPrevCo{len(self.companies):09d}"
        self.companies[cid] = (name, ctype)
        return cid, True

    def find_or_create_person(self, name, company, role):
        return "recPrevPerson00001", True

    def find_by_username(self, username):
        return None

    def find_by_chat_id(self, chat_id):
        return None

    def save_join_request(self, chat_id, username, tg_name, answers, reg_id=None):
        fields = {Registrations.REQUEST_TYPE: RequestType.UNKNOWN_USER, Registrations.SUBMITTED_BY: [],
                  Registrations.PAYLOAD: json.dumps({"chat_id": chat_id, "username": username, "tg_name": tg_name,
                                                     **answers}, ensure_ascii=False)}
        if reg_id:
            self.regs[reg_id].update(fields)
            return reg_id
        return self.add_reg(fields)

    def people_without_chat(self):
        return {"recPrevNoTg0000001": ("Олег Новиков", [OTHER_CO])}

    def person_fields(self, pid):
        from .airtable_ids import People
        return {People.NAME: "Олег Новиков", People.COMPANY: [OTHER_CO]}

    def create_person(self, fields):
        return "recPrevPerson00002"

    def update_person(self, pid, fields):
        pass

    def _company_names(self):
        return {k: v[0] for k, v in self.companies.items()}


class Recorder:
    """«Бот», который записывает сообщения вместо отправки."""

    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, **kw):
        self.sent.append((str(chat_id), text, reply_markup))
        return SimpleNamespace(message_id=len(self.sent))


class _Chat:
    def __init__(self, bot: Recorder, chat_id: int) -> None:
        self.bot, self.id, self.type = bot, chat_id, "private"

    async def send_message(self, text, parse_mode=None, reply_markup=None, **kw):
        return await self.bot.send_message(self.id, text, parse_mode, reply_markup)


def _world():
    """Песочница: репозиторий, записывающий бот, bot_data и ctx/update для проверяющего и дилера."""
    from . import main
    repo, bot = PreviewRepo(), Recorder()
    bot_data = {"repo": repo, "admin_chat_id": str(ADMIN), "memo": {}, "base_id": "app"}
    app = SimpleNamespace(bot=bot, bot_data=bot_data)
    bot_data["process_decisions"] = main.process_decisions
    bot_data["upgrade_status"] = main.upgrade_status
    bot_data["rollback_status"] = main.rollback_status
    ctx = SimpleNamespace(bot=bot, bot_data=bot_data, application=app, args=[])

    def update(chat_id: int, name: str = "Егор"):
        return SimpleNamespace(effective_chat=_Chat(bot, chat_id), effective_message=None,
                               effective_user=SimpleNamespace(full_name=name, username=None, id=chat_id),
                               callback_query=None)
    dealer = Person(DEALER_ID, "Ирина Тестова", [COMPANY], str(DEALER), None)
    return repo, bot, ctx, app, update, dealer


def _done(code: str) -> str:
    """Ответ дилеру после «Отправить» — тот же текст, что в боте (маршрут — Егор)."""
    from .scenarios import SCENARIOS
    return "Грасиас! " + SCENARIOS[code].done_text.format(reviewer="Егору", who="Егор")


def _payload_new() -> dict:
    return {
        "End Client": "Тест Банк", "Object Name": "Офис на Лесной", "Object Part": "3–5 этажи", "Country": "Russia",
        "City": "Moscow", "Object Address": "ул. Лесная, 5", "Area m2": 2400, "Area Source": "из документа",
        "PL Volume": 90000, "EXW Potential": 40500, "PPR Track": "Full", "Project Stage": "Design",
        "Specifier": "Бюро Пример", "AW Origin": "Да, заложен бюро", "Lead Architect": "Анна Образцова",
        "AW Alternatives": "No — AW only", "Decision Maker Company": "Тест Банк", "Competing Dealers": "Нет",
        "Parallel Registrations": "Нет", "Contact Level": "Verifiable (email/contact)",
        "Contact Evidence": [{"kind": "link", "url": "https://example.com/letter"}], "PL Volume Confirmed": 90000,
        "Order Month": _iso(TODAY.replace(day=1) + timedelta(days=95)),
        "Call Slot 15": f"{(TODAY + timedelta(days=2)):%d.%m.%Y} Пн, 13:00–15:00 Мск (Валенсия 12:00–14:00)",
        "Not Known Yet": "Влияют на решение",
        "Dealer Project Name": "Тест Банк — Офис на Лесной — 3–5 этажи",
    }


async def _new(w):
    from . import admin, engine, main
    from .scenarios.new_project import QUESTIONS
    repo, bot, ctx, app, update, dealer = w
    # Анкета: первый экран и сводка — как у дилера
    res = engine.start("new_project", {"_reviewer_with": "Егором"})
    for sc in res.screens:
        await bot.send_message(DEALER, sc.text, reply_markup=main._markup(sc.buttons))
    st = res.state
    st.phase = 2
    keys = {k for q in QUESTIONS for k in (q.key, q.qualifier_key, q.evidence_key) if k}
    st.answers.update({k: v for k, v in _payload_new().items() if k in keys})
    st.answers["Object Location"] = "moscow"
    st.answers["_unknown"] = ["Influencers"]
    st.awaiting = "confirm"
    s = engine.summary_screen(st)
    await bot.send_message(DEALER, s.text, reply_markup=main._markup(s.buttons))
    await bot.send_message(DEALER, _done("new_project"))
    payload = _payload_new()
    rid = repo.create_registration(dealer, RequestType.NEW_PRE_REGISTRATION, payload, payload["Dealer Project Name"])
    await main._notify_admin_new(ctx, dealer, rid, RequestType.NEW_PRE_REGISTRATION, payload,
                                 payload["Dealer Project Name"], ["planirovka.pdf"])
    up = update(ADMIN)
    await admin.on_admin_callback(up, ctx, f"ad:accn:{rid}")
    await admin.on_admin_callback(up, ctx, f"ad:acc:{rid}")
    await admin.on_admin_callback(up, ctx, f"ad:new:{rid}:-")
    await admin.on_admin_text(up, ctx, "TESTBANK Lesnaya FL3-5", resume=False)
    # Шаг 2: «Пока оставить» с комментарием
    pid = repo.regs[rid].get(Registrations.TARGET_PROJECT, [PROJECT_ID])[0]
    await admin.on_admin_callback(up, ctx, f"ad:upk:{pid}:{DEALER_ID}:pre")
    await admin.on_admin_text(up, ctx, "Пришли адрес и планировки — повышу сразу.", resume=False)
    # Отклонение другой заявки
    rid2 = repo.create_registration(dealer, RequestType.NEW_NOTIFICATION, {"End Client": "Тест"}, "Тест — Склад")
    await admin.on_admin_callback(up, ctx, f"ad:dec:{rid2}")
    await admin.on_admin_text(up, ctx, "", resume=False)


async def _upgrade(w):
    from . import admin, main
    repo, bot, ctx, app, update, dealer = w
    repo.projects[PROJECT_ID][Projects.PPR_NEXT_MISSING] = ""
    repo.projects[PROJECT_ID][Projects.PRE_REGISTERED] = []
    rid = repo.create_registration(dealer, RequestType.UPGRADE_PRE_REGISTRATION,
                                   {"Dealer Project Name": "TESTBANK Office FL3"}, "TESTBANK Office FL3", PROJECT_ID)
    await bot.send_message(DEALER, "Грасиас! Запрос на предрегистрацию по <code>TESTBANK Office FL3 AW269999</code> "
                                   "передал Егору. Ответ — в течение 72 часов, придёт сюда.")
    await main._notify_admin_new(ctx, dealer, rid, RequestType.UPGRADE_PRE_REGISTRATION,
                                 {"Dealer Project Name": "TESTBANK Office FL3"}, "TESTBANK Office FL3")
    up = update(ADMIN)
    await admin.on_admin_callback(up, ctx, f"ad:accn:{rid}")
    await admin.on_admin_text(up, ctx, "Отлично, ждём адрес объекта.", resume=False)
    # Отказ в регистрации на шаге: «Отказать»
    await admin.on_admin_callback(up, ctx, f"ad:upd:{PROJECT_ID}:{DEALER_ID}:reg")
    await admin.on_admin_callback(up, ctx, f"ad:upb:{PROJECT_ID}:{DEALER_ID}:reg:d")


async def _fill(w):
    from . import admin, main
    repo, bot, ctx, app, update, dealer = w
    payload = {"Dealer Project Name": "TESTBANK Office FL3", "Object Address": "ул. Лесная, 5",
               "Materials": [{"kind": "link", "url": "https://disk.example.com/plans"}]}
    rid = repo.create_registration(dealer, RequestType.UPDATE_FIELDS, payload, "TESTBANK Office FL3", PROJECT_ID)
    await bot.send_message(DEALER, _done("project_fill"))
    await main._notify_admin_new(ctx, dealer, rid, RequestType.UPDATE_FIELDS, payload, "TESTBANK Office FL3")
    up = update(ADMIN)
    await admin.on_admin_callback(up, ctx, f"ad:acc:{rid}")
    from . import engine
    await bot.send_message(DEALER, engine.NOTHING_NEW, reply_markup=main._markup(engine.nothing_new_buttons(PROJECT_ID)))


async def _news(w):
    from . import admin, main
    repo, bot, ctx, app, update, dealer = w
    payload = {"Dealer Project Name": "TESTBANK Office FL3", "Dealer News": "Клиент утвердил концепцию, тендер в марте."}
    rid = repo.create_registration(dealer, RequestType.UPDATE_NEWS, payload, "TESTBANK Office FL3", PROJECT_ID)
    await bot.send_message(DEALER, _done("project_news"))
    await main._notify_admin_new(ctx, dealer, rid, RequestType.UPDATE_NEWS, payload, "TESTBANK Office FL3")
    up = update(ADMIN)
    await admin.on_admin_callback(up, ctx, f"ad:inf:{rid}")
    await admin.on_admin_text(up, ctx, "Кто в тендерной комиссии?", resume=False)
    reply = {"Dealer Project Name": "TESTBANK Office FL3", "In Reply To": rid, "Dealer News": "Закупщик банка и бюро."}
    rid2 = repo.create_registration(dealer, RequestType.UPDATE_NEWS, reply, "TESTBANK Office FL3", PROJECT_ID)
    await main._notify_admin_new(ctx, dealer, rid2, RequestType.UPDATE_NEWS, reply, "TESTBANK Office FL3")
    await admin.on_admin_callback(up, ctx, f"ad:acc:{rid2}")


async def _quote(w):
    from . import admin, main
    from . import cards
    repo, bot, ctx, app, update, dealer = w
    p = repo.projects[PROJECT_ID]
    p.update({Projects.PPR_TRACK: "Simplified (<30k)", Projects.EXW_POTENTIAL: 11500})
    payload = {"Dealer Project Name": "TESTBANK Office FL3", "Quotation / Proforma №": "20101446"}
    rid = repo.create_registration(dealer, RequestType.QUOTE_SIMPLIFIED, payload, "TESTBANK Office FL3", PROJECT_ID)
    await bot.send_message(DEALER, _done("project_quote"))
    await main._notify_admin_new(ctx, dealer, rid, RequestType.QUOTE_SIMPLIFIED, payload, "TESTBANK Office FL3")
    await admin.on_admin_callback(update(ADMIN), ctx, f"ad:acc:{rid}")
    card = cards.accept_card(p, DEALER_ID, "Тест Мебель", {"PL Volume": 25000}, True)
    await bot.send_message(DEALER, card)


async def _won_lost(w, won: bool):
    from . import admin, main
    repo, bot, ctx, app, update, dealer = w
    if won:
        payload, rtype = {"Dealer Project Name": "TESTBANK Office FL3", "Order / Proforma №": "148928"}, RequestType.UPDATE_WON
    else:
        payload, rtype = {"Dealer Project Name": "TESTBANK Office FL3", "Lost Reason": "Lost to cheaper offer"}, \
            RequestType.UPDATE_LOST
    rid = repo.create_registration(dealer, rtype, payload, "TESTBANK Office FL3", PROJECT_ID)
    await bot.send_message(DEALER, _done("project_won" if won else "project_lost"))
    await main._notify_admin_new(ctx, dealer, rid, rtype, payload, "TESTBANK Office FL3")
    await admin.on_admin_callback(update(ADMIN), ctx, f"ad:acc:{rid}")


async def _call(w):
    from . import admin
    repo, bot, ctx, app, update, dealer = w
    payload = _payload_new()
    rid = repo.create_registration(dealer, RequestType.NEW_PRE_REGISTRATION, payload, payload["Dealer Project Name"])
    await admin.send_call_offer(bot, ADMIN, rid, repo.regs[rid], dealer.name)
    up = update(ADMIN)
    await admin.on_admin_callback(up, ctx, f"ad:cok:{rid}:0")
    await admin.on_admin_callback(up, ctx, f"ad:cot:{rid}:0")
    await admin.on_admin_text(up, ctx, "завтра в 16:00 по Москве", resume=False)
    await admin.dealer_call_callback(update(DEALER, "Ирина"), ctx, f"dc:other:{rid}:0")
    await admin.dealer_call_text(update(DEALER, "Ирина"), ctx, "Послезавтра в 11:00")
    await admin.dealer_call_callback(update(DEALER, "Ирина"), ctx, f"dc:ok:{rid}:0")
    await admin.on_admin_callback(up, ctx, f"ad:cno:{rid}:0")


async def _expiry(w):
    from . import main, routing
    repo, bot, ctx, app, update, dealer = w
    await main._warn_expired(app, routing.EGOR, ADMIN)
    repo.projects[PROJECT_ID][Projects.PPR_NEXT_MISSING] = ""
    await main._warn_ready(app, routing.EGOR, ADMIN)


async def _rollback(w):
    from . import admin
    repo, bot, ctx, app, update, dealer = w
    repo.projects[PROJECT_ID][Projects.PRE_REGISTERED] = [DEALER_ID]
    repo.projects[PROJECT_ID][Projects.NOTIFIED] = []
    up = update(ADMIN)
    await admin.on_admin_callback(up, ctx, f"ad:ups:{PROJECT_ID}:{DEALER_ID}")
    await admin.on_admin_callback(up, ctx, f"ad:rb:{PROJECT_ID}:{DEALER_ID}")
    await admin.on_admin_callback(up, ctx, f"ad:rbd:{PROJECT_ID}:{DEALER_ID}")
    await admin.on_admin_text(up, ctx, "Срок истёк, новостей нет", resume=False)


async def _unknown(w):
    from . import onboarding
    repo, bot, ctx, app, update, dealer = w

    def say(text=None, data=None):
        up = update(OTHER, "Олег Новиков")
        up.effective_user.username = "oleg_new"
        if data:
            up.callback_query = SimpleNamespace(data=data)
        else:
            up.effective_message = SimpleNamespace(text=text)
        return onboarding.handle(up, ctx)

    await say("/start")
    await say(data=f"{onboarding.CB_USER}tg")
    for answer in ("Другая Мебель", "Менеджер проектов"):
        await say(answer)
    await say(data=f"{onboarding.CB_USER}c:Russia")
    await say("Москва")
    await say("12")                       # не телефон — бот переспросит
    await say("+7 912 345 67 89")
    await say("oleg@example.com")
    await say(data=f"{onboarding.CB_USER}ok")
    await say("когда подключите?")
    rid = next(k for k, v in repo.regs.items() if v.get(Registrations.REQUEST_TYPE) == RequestType.UNKNOWN_USER)
    admin_up = update(ADMIN)
    await onboarding.on_admin_callback(admin_up, ctx, f"{onboarding.CB_ADMIN}oc:{rid}")
    from . import admin
    await admin.on_admin_text(admin_up, ctx, "Новая Студия", resume=False)
    await onboarding.on_admin_callback(admin_up, ctx, f"{onboarding.CB_ADMIN}co:{rid}:{OTHER_CO}")
    # Отказ — на второй заявке
    rid2 = repo.save_join_request(OTHER + 1, None, "Аноним", {"name": "Иван Иванов", "company": "Неизвестно"})
    await onboarding.send_card(bot, repo, ADMIN, rid2)
    await onboarding.on_admin_callback(admin_up, ctx, f"{onboarding.CB_ADMIN}no:{rid2}")
    await admin.on_admin_text(admin_up, ctx, "Работаем только с официальными дилерами", resume=False)


async def _errors(w):
    from . import alerts, main
    repo, bot, ctx, app, update, dealer = w
    h = alerts.AlertHandler()
    lg = logging.getLogger("bot.preview.sample")
    lg.addHandler(h)
    lg.propagate = False
    try:
        lg.error("x", extra={"alert": "Не дошла карточка новой заявки «Тест Банк — Офис» от Ирина Тестова. "
                                      "Открой /inbox — она там."})
        try:
            raise TimeoutError("timeout")
        except TimeoutError:
            lg.exception("Сетевой сбой")
    finally:
        lg.removeHandler(h)
    for _, text in h.drain().items():
        await bot.send_message(ADMIN, text)
    # Застрявшее решение: не хватает данных для предрегистрации
    p = repo.projects[PROJECT_ID]
    p.pop(Projects.OBJECT_ADDRESS, None)
    rid = repo.create_registration(dealer, RequestType.UPGRADE_PRE_REGISTRATION,
                                   {"Dealer Project Name": "TESTBANK Office FL3"}, "TESTBANK Office FL3", PROJECT_ID)
    repo.set_decision(rid, Decision.ACCEPT_MERGE, "")
    await main.process_decisions(app)
    rid2 = repo.create_registration(dealer, RequestType.NEW_NOTIFICATION, {"End Client": "Тест"}, "Тест — Склад")
    repo.set_decision(rid2, Decision.ACCEPT, "")
    await main.process_decisions(app)
    await bot.send_message(DEALER, "Что-то пошло не так на моей стороне. Ответы, которые ты уже дал, сохранены: "
                                   "нажми /start — я предложу продолжить. Начать заново — /cancel.")


async def _digest(w):
    from . import alerts, main, routing
    repo, bot, ctx, app, update, dealer = w
    h = alerts.AlertHandler()
    h.day_count, h.survived_count = 2, 5
    await main._morning(app, routing.EGOR, ADMIN, h, datetime.now())
    text, buttons, _ = await main._build_digest(repo, DEALER_ID, TODAY, "Ирина Тестова")
    await bot.send_message(DEALER, text, reply_markup=main._markup(buttons))


RUNNERS = {
    "new": _new, "upgrade": _upgrade, "fill": _fill, "news": _news, "quote": _quote,
    "won": lambda w: _won_lost(w, True), "lost": lambda w: _won_lost(w, False), "call": _call,
    "expiry": _expiry, "rollback": _rollback, "unknown": _unknown, "errors": _errors, "digest": _digest,
}


def _inert(markup):
    """Кнопки просмотра ничего не делают — те же подписи, другой callback."""
    if not isinstance(markup, InlineKeyboardMarkup):
        return None
    rows = [[InlineKeyboardButton(b.text, callback_data=f"{CB_PREVIEW}{i}") for i, b in enumerate(row)]
            for row in markup.inline_keyboard]
    return InlineKeyboardMarkup(rows)


def menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(title, callback_data=f"am:preview:{code}")]
                                 for code, title in SCENARIOS.items()])


async def record(scenario: str) -> list[tuple[str, str, object]]:
    """Прогнать сценарий в песочнице — список (кому, текст, кнопки)."""
    w = _world()
    bot = w[1]
    await RUNNERS[scenario](w)
    return bot.sent


async def run(bot, chat_id, scenario: str) -> None:
    from . import admin
    if scenario not in RUNNERS:
        await bot.send_message(chat_id, "🛠 👁 Какой сценарий показать? Сообщения придут с тестовыми данными — "
                                        "в базу ничего не пишется, дилерам ничего не уходит.",
                               reply_markup=menu_markup())
        return
    try:
        sent = await record(scenario)
    except Exception:
        log.exception("Просмотр %s", scenario)
        await bot.send_message(chat_id, f"🛠 Просмотр «{SCENARIOS[scenario]}» не собрался — передай Claude, "
                                        "подробности в логах Railway.")
        return
    label = {str(ADMIN): "🟦 Тебе", str(DEALER): "🟩 Дилеру", str(OTHER): "🟨 Другому человеку"}
    await bot.send_message(chat_id, f"👁 <b>Просмотр: {escape(SCENARIOS[scenario])}</b> — {len(sent)} сообщ. "
                                    "Тестовые данные, ничего не записано и не отправлено. Кнопки не работают.",
                           parse_mode=ParseMode.HTML)
    for to, text, markup in sent:
        head = label.get(to, "🟨 Другому человеку")
        await admin.send_card(bot, chat_id, f"<b>{head}:</b>\n{text}", _inert(markup))
        await asyncio.sleep(0.05)
