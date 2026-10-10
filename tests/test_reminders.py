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
    assert it.reasons == {"expiry": "предрегистрация до 20.10.2026, осталось 8 дн."}


def test_nothing_to_remind():
    assert reminders.project_item(proj(expiry="2026-12-31"), TODAY, date(2026, 10, 1)) is None


def test_stale_news_and_gaps_titles():
    f = dict(FULL)
    for k in (P.OBJECT_ADDRESS, P.CONTACT_LEVEL, P.DOSSIER_FILES):
        del f[k]
    it = reminders.project_item(proj("notified", None, f), TODAY, date(2026, 9, 15))
    assert it.reasons["gaps"] == "Адрес, Уровень контакта, Материалы"
    assert it.reasons["stale"] == "последняя 15.09"


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



def test_digest_grouped_by_reason():
    items = [reminders.Item("p1", "Petrovitch", {"expiry": "предрегистрация до 20.10.2026, осталось 8 дн.",
                                                 "stale": "последняя 15.09"}),
             reminders.Item("p2", "JTI Saint Petersburg", {"gaps": "Адрес, Материалы",
                                                           "stale": "новостей ещё не было"})]
    text = reminders.digest_text(items)
    expiry_at, gaps_at, stale_at = (text.index(h) for h in ("Срок подходит", "Не хватает данных", "Нет новостей"))
    assert expiry_at < gaps_at < stale_at
    assert "• Petrovitch — предрегистрация до 20.10.2026, осталось 8 дн." in text
    assert "• JTI Saint Petersburg — Адрес, Материалы" in text
    assert text.count("• Petrovitch") == 2 and "• JTI Saint Petersburg — новостей ещё не было" in text
    only_stale = reminders.digest_text([reminders.Item("p", "X", {"stale": "последняя 01.09"})])
    assert "Срок подходит" not in only_stale and "Не хватает" not in only_stale
