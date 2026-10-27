import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bot import admin, alerts, dedup, main
from bot.airtable_ids import Registrations as R
from bot.slots import VALENCIA
from bot.state import DialogState


def test_alert_handler_collects_and_drains():
    h = alerts.AlertHandler()
    log = logging.getLogger("bot.test_alerts")
    log.addHandler(h)
    try:
        log.error("Ошибка обработки решений")
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("Сбой")
        log.warning("не ошибка")
    finally:
        log.removeHandler(h)
    text = h.drain()
    assert "ошибки (2)" in text and "Сбой — ValueError: boom" in text
    assert h.drain() is None and h.day_count == 2


def test_summary_only_in_morning_hour_once():
    day = datetime(2026, 10, 21, 9, 5, tzinfo=VALENCIA)
    assert alerts.summary_due(day, None)
    assert not alerts.summary_due(day, "2026-10-21")
    assert not alerts.summary_due(day.replace(hour=14), None)
    text = alerts.summary_text(day, 0, {"no_decision": 2, "need_info": 1, "blocked": []}, 1, 0)
    assert "Заявок без решения: 2 (дольше 72 ч: 1)" in text and "Ошибок за сутки не было" in text


def test_age_line():
    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    assert admin.age_line("2026-10-21T07:00:00.000Z", now) == "\n⏰ На проверке 5 ч"
    assert admin.age_line("2026-10-18T07:00:00.000Z", now) == "\n⏰ На проверке 3 дн. — срок 72 ч прошёл"


def test_latin_name_suggestion():
    assert dedup.latin_name({"End Client": "ООО «Айкон Тайерс»", "Object Name": "VIP этаж"}) == "Aikon Taiers VIP Etazh"
    assert dedup.latin_name({}) == ""


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((str(chat_id), text))


def test_overdue_reviewer_then_dealer():
    created = lambda h: (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()
    rec = {"id": "recR", "fields": {R.SUBMITTED_AT: created(50), R.DEALER_PROJECT_NAME: "Медалл",
                                    R.REQUEST_TYPE: "New — Notification", R.SUBMITTED_BY: ["recP"]}}
    updates = []
    repo = SimpleNamespace(
        undecided=lambda: [rec], company_types=lambda ids: [], company_name=lambda c: c,
        chat_ids_for_people=lambda ids: ["555"], dedup_projects=lambda: [],
        update_registration=lambda rid, f: updates.append(f[R.BOT_PROCESSED]))
    app = SimpleNamespace(bot=Bot(), bot_data={"repo": repo, "admin_chat_id": "1", "base_id": "appX"})
    asyncio.run(main._check_overdue(app))
    assert updates == ["late:48"] and app.bot.sent[0][0] == "1" and "больше 48 часов" in app.bot.sent[0][1]
    rec["fields"][R.BOT_PROCESSED] = "late:48"
    rec["fields"][R.SUBMITTED_AT] = created(73)
    asyncio.run(main._check_overdue(app))
    assert updates[-1] == "late:72" and app.bot.sent[-1] == ("555", main.LATE_TEXT.format(name="Медалл"))
    rec["fields"][R.BOT_PROCESSED] = "late:72"
    asyncio.run(main._check_overdue(app))
    assert len(app.bot.sent) == 2


def test_abandoned_dialog_nudged_once(monkeypatch):
    st = DialogState(scenario="new_project", answers={"End Client": "X"},
                     started_at=(datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds"))
    person = SimpleNamespace(record_id="recP", chat_id="555", state=st)
    saved = []
    repo = SimpleNamespace(people_with_dialogs=lambda: [(person, "Russia")],
                           save_state=lambda pid, s: saved.append(s.nudged))
    app = SimpleNamespace(bot=Bot(), bot_data={"repo": repo})
    monkeypatch.setattr(main.reminders, "person_now", lambda c: datetime(2026, 10, 21, 12))
    asyncio.run(main._check_abandoned(app))
    assert saved == [True] and "незаконченная заявка" in app.bot.sent[0][1]
    asyncio.run(main._check_abandoned(app))
    assert len(app.bot.sent) == 1


def test_architect_linked_to_people_and_project():
    import json
    from bot.airtable_ids import Projects as P
    calls = {}
    repo = SimpleNamespace(
        find_or_create_person=lambda name, company, role: calls.setdefault("person", (name, company, role)) and ("recG", True),
        update_project=lambda pid, f: calls.setdefault("project", (pid, f)))
    reg = {R.PAYLOAD: json.dumps({"Lead Architect": "Иван Петров", "Specifier": "UNK Project"})}
    note = asyncio.run(main._link_architect(repo, reg, {P.PEOPLE: ["recX"]}, "recP", ["recBUREAU"], ["recDEALER"]))
    assert calls["person"] == ("Иван Петров", "recBUREAU", "ГАП")
    assert calls["project"] == ("recP", {P.PEOPLE: ["recX", "recG"]})
    assert note == "ГАП Иван Петров (новый в People)"
    # Бюро — сам дилер (проект ведёт дилер / AW предлагает дилер) — ГАП не заводим
    assert asyncio.run(main._link_architect(repo, reg, {}, "recP", ["recDEALER"], ["recDEALER"])) is None


def test_same_alert_at_most_once_per_hour():
    h = alerts.AlertHandler()
    t0 = datetime(2026, 10, 27, 10, 0, tzinfo=VALENCIA)
    for _ in range(3):
        h.queue.append((t0, "Необработанная ошибка — AttributeError: x"))
    text = h.drain(t0)
    assert "ошибки (3)" in text and "(×3)" in text
    h.queue.append((t0, "Необработанная ошибка — AttributeError: x"))
    assert h.drain(t0 + timedelta(minutes=30)) is None             # тот же текст — молчим час
    h.queue.append((t0, "Необработанная ошибка — AttributeError: x"))
    assert h.drain(t0 + timedelta(minutes=61)) is not None


def test_upgrade_resets_status_date_and_card_shows_expiry():
    from bot import cards
    from bot.airtable_ids import Projects as P
    from bot.decisions import plan
    reg = {R.DECISION: "Accept — merge into Target Project", R.REQUEST_TYPE: "Upgrade — Pre-registration",
           R.REG_STATUS: "Pre-registration", R.SUBMITTED_BY: ["recM"], R.DEALER: ["recC"],
           R.TARGET_PROJECT: ["recP"], R.DEALER_PROJECT_NAME: "X", R.PAYLOAD: "{}"}
    full = {P.NAME: "X", P.EXW_POTENTIAL: 45000, P.EXPECTED_ORDER_DATE: "2027-03-01", P.OBJECT_ADDRESS: "a",
            P.SPECIFIER: ["s"], P.CONTACT_LEVEL: "Verifiable (email/contact)", P.AW_ALTERNATIVES: "No — AW only",
            P.PARALLEL_REGISTRATIONS: "нет", P.DOSSIER_FILES: [{"id": "f"}], P.NOTIFIED: ["recM"],
            P.STATUS_CONFIRMED: "2026-01-01"}
    pl = plan(reg, full)
    assert pl.project_updates[P.PRE_REGISTERED] == ["recM"] and pl.project_updates[P.NOTIFIED] == []
    assert pl.project_updates[P.STATUS_CONFIRMED] == "__today__"   # новый статус — новый срок
    card = cards.accept_card({**full, P.NOTIFIED: [], P.PRE_REGISTERED: ["recM"], P.STATUS_EXPIRY: "2026-11-27"},
                             "recM", "STATIO", {}, False)
    assert "⏳ Статус действует до 27.11.2026" in card


def test_offer_upgrade_only_when_next_step_complete():
    from bot.airtable_ids import Projects as P
    app = SimpleNamespace(bot=Bot(), bot_data={})
    ready = {P.NAME: "Raiffeisen Private", P.PPR_REF: "AW-26-0094", P.NOTIFIED: ["recM"], P.PPR_TRACK: "Full"}
    asyncio.run(main._offer_upgrade(app, "1", ready, "recP", "recM"))
    assert "Условия для предрегистрации выполнены</b> · AW-26-0094 · Raiffeisen Private" in app.bot.sent[0][1]
    asyncio.run(main._offer_upgrade(app, "1", dict(ready, **{P.PPR_NEXT_MISSING: "Dossier Files;"}), "recP", "recM"))
    asyncio.run(main._offer_upgrade(app, "1", dict(ready, **{P.REGISTERED: ["recX"]}), "recP", "recM"))
    assert len(app.bot.sent) == 1
    pre = {**ready, P.NOTIFIED: [], P.PRE_REGISTERED: ["recM"]}
    asyncio.run(main._offer_upgrade(app, "1", pre, "recP", "recM"))
    assert "Условия для регистрации выполнены" in app.bot.sent[1][1]


def test_reg_status_event_non_status_just_consumed():
    updates = []
    repo = SimpleNamespace(update_registration=lambda rid, f: updates.append((rid, f)))
    app = SimpleNamespace(bot=Bot(), bot_data={"repo": repo, "admin_chat_id": "1"})
    asyncio.run(main._handle_reg_status_event(app, "recR", {R.REG_STATUS: "PENDING"}))
    assert updates == [("recR", {R.BOT_REG_STATUS: "PENDING"})] and app.bot.sent == []
    asyncio.run(main._handle_reg_status_event(app, "recR", {R.REG_STATUS: "Pre-registration",
                                                            R.DEALER_PROJECT_NAME: "X"}))
    assert "нет Target Project" in app.bot.sent[0][1]
