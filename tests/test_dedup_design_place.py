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


TBANK = {"End Client": "T Bank", "Object Name": "Telegraph additional furniture", "Object Part": "Areas from Principal",
         "Country": "Russia", "City": "Moscow", "Object Address": "Tverskaya, 7"}
ARTEM, OSRU = "recPVdkamoaXPgFl4", "recOSRU"
ICITY = {"id": "rec234", "name": "MRGroup iCity NUEZ24", "ref": "AW260234", "object": "iCity", "address": "",
         "clients": ["MR Group"], "city": "Moscow", "status": "Active", "dealers": [OSRU], "managers": [ARTEM],
         "created": "2026-10-22T09:21:51Z"}
ALFA = {"id": "rec206", "name": "ABFinance (Альфа-Банк, Москва, Маши Порываевой 7 корп. Г) — AOF", "ref": "AW260206",
        "object": "", "address": "", "clients": ["Alfa-Bank (Альфа-Банк)"], "city": "", "status": "Active",
        "dealers": [], "managers": [], "created": "2026-10-02T11:40:51Z"}
BCC = {"id": "rec246", "name": "Bank CenterCredit (BCC) — Almaty — F6 VIP lounge", "ref": "AW260246", "object": "BCC",
       "address": "", "clients": [], "city": "Almaty", "status": "Active", "dealers": [], "managers": [],
       "created": "2026-10-28T12:26:52Z"}


def test_tbank_17_11_no_false_matches():
    now = datetime(2026, 11, 17, 13, 4, tzinfo=timezone.utc)
    found = dedup.find(TBANK, "T Bank — Telegraph additional furniture — Areas from Principal", [ICITY, ALFA, BCC],
                       now=now, dealer_ids=[OSRU], person_ids=[ARTEM])
    assert found == []


def test_same_manager_with_real_overlap_goes_first():
    now = datetime(2026, 11, 17, 13, 4, tzinfo=timezone.utc)
    tg = {"id": "recTG", "name": "TBank Telegraph FL2", "ref": "AW260270", "object": "Telegraph", "address": "",
          "clients": ["T Bank"], "city": "Moscow", "status": "Active", "dealers": [OSRU], "managers": [ARTEM],
          "created": "2026-11-01T10:00:00Z"}
    found = dedup.find(TBANK, "", [ICITY, ALFA, tg], now=now, dealer_ids=[OSRU], person_ids=[ARTEM])
    assert [c.ref for c in found] == ["AW260270"]
    assert found[0].reasons[0].startswith("тот же менеджер")


def test_search_by_ref_and_words():
    projects = [ICITY, ALFA, BCC]
    assert [c.ref for c in dedup.search("AW260234", projects)] == ["AW260234"]
    assert [c.ref for c in dedup.search("aw-26-0246", projects)] == ["AW260246"]
    assert [c.ref for c in dedup.search("260206", projects)] == ["AW260206"]
    assert [c.ref for c in dedup.search("iCity", projects)] == ["AW260234"]
    assert [c.ref for c in dedup.search("Альфа", projects)] == ["AW260206"]
    assert dedup.search("Telegraph", projects) == []


def test_find_button_flow():
    import asyncio
    from bot import admin, preview
    from bot.airtable_ids import Registrations as R, RequestType as T

    async def go():
        repo, bot, ctx, app, update, dealer = preview._world()
        rid = repo.create_registration(dealer, T.NEW_NOTIFICATION, {"End Client": "Тест"}, "Тест — Склад")
        up = update(preview.ADMIN)
        await admin.on_admin_callback(up, ctx, f"ad:acc:{rid}")
        rows = bot.sent[-1][2].inline_keyboard
        assert any(b.text.startswith("🔎 Другой существующий") for r in rows for b in r)
        await admin.on_admin_callback(up, ctx, f"ad:fnd:{rid}:-")
        await admin.on_admin_text(up, ctx, "AW269999", resume=False)
        rows = bot.sent[-1][2].inline_keyboard
        assert rows[0][0].text.startswith("В проект: TESTBANK")
        await admin.on_admin_callback(up, ctx, rows[0][0].callback_data)
        assert repo.regs[rid][R.TARGET_PROJECT] == [preview.PROJECT_ID]
        assert str(repo.regs[rid][R.BOT_PROCESSED]).startswith(("done:", "blocked:"))
    asyncio.run(go())
