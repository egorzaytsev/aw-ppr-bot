"""Кейс Design Place 12.11.2026: дубль AW260262 к AW260261 и ложное совпадение с Radisson по «апартаментам»."""
from datetime import datetime, timedelta, timezone

from bot import dedup
from bot.scenarios.new_project import check_city

NOW = datetime(2026, 11, 12, 16, 20, tzinfo=timezone.utc)
PAYLOAD = {"End Client": "Частное лицо в Дубае", "Object Name": "Апартаменты", "Country": "Other",
           "City": "Dubai", "Object Address": "Grand Blue Tower Beach", "Project Stage": "Design"}
DESIGN_PLACE, GULSHAT = "rectmKCT8H74kqQyN", "rec9jOpa5msxAgAmF"
BARSTOOLS = {"id": "rec261", "name": "DesignPlace Dubai BarStools", "ref": "AW260261", "object": "", "address": "",
             "clients": [], "city": "", "status": "Active", "dealers": [DESIGN_PLACE], "managers": [],
             "created": (NOW - timedelta(minutes=20)).isoformat()}
RADISSON = {"id": "rec235", "name": "Radisson Chorsu Tashkent (Manar) — лобби, бар, ресторан", "ref": "AW260235",
            "object": "Чорсу (бывшая гостиница «Москва») — Radisson, 210 номеров + ~190 сервисных апартаментов",
            "address": "", "clients": ["Manar Group"], "city": "Tashkent", "status": "Prospecting",
            "dealers": [], "managers": [], "created": "2026-10-22T16:28:42Z"}


def _find(projects, **kw):
    return dedup.find(PAYLOAD, "Частное лицо в Дубае — Апартаменты", projects, now=NOW,
                      dealer_ids=[DESIGN_PLACE], person_ids=[GULSHAT], **kw)


def test_fresh_project_of_same_dealer_found_first():
    found = _find([RADISSON, BARSTOOLS])
    assert [c.ref for c in found] == ["AW260261"]
    assert found[0].reasons[0] == "та же компания, заведён сегодня"
    assert "город" in found[0].reasons            # «в Дубае» = Dubai в названии


def test_generic_words_do_not_match():
    assert _find([RADISSON]) == []
    assert not dedup.overlap("Апартаменты", "190 сервисных апартаментов")
    assert not dedup.overlap("Частное лицо", "частного лица")
    assert not dedup.overlap("Офис", "Office Tower")


def test_cyrillic_city_matches_latin():
    assert dedup.overlap("Частное лицо в Дубае", "Dubai")
    assert dedup.overlap("Ташкенте", "Tashkent")


def test_old_or_closed_project_of_same_dealer_not_pushed():
    old = dict(BARSTOOLS, created=(NOW - timedelta(days=45)).isoformat(), name="DesignPlace Riyadh Chairs")
    won = dict(BARSTOOLS, status="Won", name="DesignPlace Riyadh Tables")
    assert _find([old, won]) == []


def test_city_alone_is_not_a_match():
    other = dict(RADISSON, name="Some Dubai Hotel", city="Dubai", object="", clients=[])
    assert _find([other]) == []


def test_private_person_name_is_dealer_city_building():
    assert dedup.latin_name(PAYLOAD, "Design Place") == "DesignPlace Dubai GrandBlueTowerBeach"
    assert "CHASTNO" not in dedup.latin_name(PAYLOAD)


def test_company_name_unchanged():
    assert dedup.latin_name({"End Client": "Сбер", "Object Name": "ЖК Символ, корпус 2"}) == "SBER SimvolRC K2"


def test_city_field_only_city():
    assert check_city("Grand Blue Tower Beach")
    assert check_city("ул. Лесная, 5")
    assert check_city("Дубай") is None and check_city("Dubai") is None
