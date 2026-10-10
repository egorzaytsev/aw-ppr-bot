from bot import dedup

PROJECTS = [
    {"id": "p1", "name": "PAO Simvol", "object": "", "address": "", "clients": []},
    {"id": "p2", "name": "Petrovitch", "object": "Petrovitch HQ", "address": "Lesnaya 9", "clients": ["Petrovich LLC"]},
    {"id": "p3", "name": "Sber Tower", "object": "Sberbank HQ", "address": "ул. Вавилова, 19", "clients": ["Сбербанк"]},
    {"id": "p4", "name": "Hyatt Regency", "object": "Hotel", "address": "", "clients": []},
]


def test_translit_and_transcription():
    assert dedup.sound("Символ") == "simvol" and dedup.sound("Symbol") == "simbol"
    assert dedup.sound("Петрович") == dedup.sound("Petrovitch")
    assert dedup.overlap("Символ", "Symbol")


def test_find_by_object_client_address():
    found = dedup.find({"Object Name": "Символ", "End Client": "Пао"}, "Пао — Символ", PROJECTS)
    assert [c.project_id for c in found] == ["p1"] and "объект" in found[0].reasons
    found = dedup.find({"Object Name": "Штаб-квартира", "End Client": "ПАО Сбербанк",
                        "Object Address": "улица Вавилова 19"}, "", PROJECTS)
    assert found[0].project_id == "p3" and found[0].reasons == ["адрес", "клиент"]
    found = dedup.find({"Object Name": "Офис Петровича", "Object Address": "ул. Лесной, 9"}, "", PROJECTS)
    assert found[0].project_id == "p2" and "адрес" in found[0].reasons


def test_no_false_match_on_common_words():
    assert dedup.find({"Object Name": "Офис", "End Client": "ООО Ромашка"}, "", PROJECTS) == []
    assert "не найдено" in dedup.analysis_text([])


def test_match_company_ignores_legal_form_and_script():
    companies = {"c1": "Sberbank", "c2": "UNK Project", "c3": "Petrovich"}
    assert dedup.match_company("ПАО Сбербанк", companies) == "c1"
    assert dedup.match_company("ООО «Петрович»", companies) == "c3"
    assert dedup.match_company("Ромашка", companies) is None
