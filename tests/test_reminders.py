from datetime import date, datetime
from zoneinfo import ZoneInfo

from bot import reminders
from bot.airtable_ids import Projects as P
from bot.airtable_repo import DealerProject

TODAY = date(2026, 10, 12)  # понедельник
FULL = {P.NAME: "X", P.END_CLIENT: ["c"], P.OBJECT_NAME: "o", P.COUNTRY: "Russia", P.CITY: "Moscow",
        P.OBJECT_ADDRESS: "a", P.PROJECT_STAGE: "Design", P.SPECIFIER: ["s"], P.EXW_POTENTIAL: 1,
        P.AW_ALTERNATIVES: "No — AW only", P.EXPECTED_ORDER_DATE: "2027-01-01", P.CONTACT_LEVEL: "Declared",
        P.PARALLEL_REGISTRATIONS: "нет", P.DOSSIER_FILES: [{"id": "a"}], P.QUOTATION: "Q"}


def proj(status="pre_registered", expiry="2026-10-20", fields=None, track="Full"):
    return DealerProject("recP", "Petrovitch", status, expiry, track, dict(FULL if fields is None else fields))


def test_expiry_soon_and_no_gaps():
    it = reminders.project_item(proj(), TODAY, date(2026, 10, 1))
    assert it.lines == ["срок предрегистрации до 20.10.2026, осталось 8 дн. Если есть прогресс — "
                        "«Сообщить новость», чтобы запросить продление."]


def test_nothing_to_remind():
    assert reminders.project_item(proj(expiry="2026-12-31"), TODAY, date(2026, 10, 1)) is None


def test_stale_news_and_gaps_titles():
    f = dict(FULL)
    for k in (P.OBJECT_ADDRESS, P.CONTACT_LEVEL, P.DOSSIER_FILES):
        del f[k]
    it = reminders.project_item(proj("notified", None, f), TODAY, date(2026, 9, 15))
    assert "не хватает данных: Адрес, Уровень контакта, Материалы." in it.lines[0]
    assert it.lines[1] == "нет новостей с 15.09 — больше 21 дней."


def test_simplified_only_phase1_gaps():
    f = dict(FULL)
    del f[P.CONTACT_LEVEL]
    assert reminders.project_item(proj("notified", None, f, "Simplified (<30k)"), TODAY, date(2026, 10, 1)) is None


def test_slot_weekday_and_weekly():
    now = datetime(2026, 10, 12, 9, 30, tzinfo=ZoneInfo("Europe/Moscow"))
    assert reminders.in_slot(now, "08:00–11:00") and not reminders.in_slot(now, "11:00–14:00")
    assert reminders.due(None, TODAY) and not reminders.due("2026-10-08", TODAY)
    assert reminders.due("2026-10-05", TODAY)
    assert not reminders.due(None, date(2026, 10, 10))  # суббота
