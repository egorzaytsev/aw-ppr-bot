"""«📅 В календарь»: Outlook-ссылка и .ics по подтверждённому звонку."""
from urllib.parse import parse_qs, urlparse

from bot.calendar_links import call_event, minutes_of

SLOT = "15.11.2026 Вс, 13:00–15:00 Мск (Валенсия 11:00–13:00)"


def test_outlook_link_and_ics():
    ev = call_event("Звонок 15 минут", SLOT, "Ирина Тестова", "TESTBANK Office FL3 AW269999", "+7 900 000-00-00",
                    "Тест Мебель")
    assert ev.minutes == 15 and ev.start.hour == 11
    q = parse_qs(urlparse(ev.outlook_url()).query)
    assert q["subject"] == ["Звонок · Ирина Тестова · TESTBANK Office FL3 AW269999"]
    assert q["startdt"][0].startswith("2026-11-15T11:00:00") and q["enddt"][0].startswith("2026-11-15T11:15:00")
    ics = ev.ics().decode()
    assert "DTSTART:20261115T100000Z" in ics and "DTEND:20261115T101500Z" in ics   # Валенсия UTC+1 в ноябре
    assert "Телефон: +7 900 000-00-00" in ics.replace("\\n", "\n")


def test_free_text_time_gives_no_event():
    assert call_event("Звонок", "в среду после обеда", "X", "Y") is None


def test_minutes():
    assert minutes_of("Звонок на 30 минут перед регистрацией") == 30
    assert minutes_of("Звонок") == 30
