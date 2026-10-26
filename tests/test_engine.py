import pytest

from bot import engine
from bot.scenarios import NEW_PROJECT
from bot.scenarios.base import ParseError, parse_month, parse_number
from bot.state import DialogState


def test_parse_number():
    assert parse_number("45000") == 45000
    assert parse_number("45к") == 45000
    assert parse_number("120 000 €") == 120000
    assert parse_number("1,5 млн") == 1_500_000
    assert parse_number("45.000") == 45000
    with pytest.raises(ParseError):
        parse_number("много")


def test_parse_month():
    assert parse_month("03.2027") == "2027-03-01"
    assert parse_month("март 2027") == "2027-03-01"
    assert parse_month("Q3 2027") == "2027-07-01"
    assert parse_month("15.11.2026") == "2026-11-15"
    with pytest.raises(ParseError):
        parse_month("скоро")


# Индексы: фаза 1 — 0 клиент, 1 объект, 2 часть, 3 где объект, 4 страна, 5 город, 6 адрес,
# 7 площадь, 8 объём, 9 стадия, 10 AW в проекте бюро, 11 бюро, 12 ГАП, 13 альтернативы;
# фаза 2 — 14…22 (материалы одним экраном — 21, звонок — 22); фаза 3 — 23…30 (документы третьей стороны 25…28)
CH = engine.CB_CHOICE
FILE = {"kind": "file", "file_id": "F1", "file_unique_id": "U1", "name": "plan.pdf", "size": 1000,
        "mime": "application/pdf"}


def _run_phase1(exw="120к", stage=3, skip_optional=False, other_place=False):
    r = engine.start(NEW_PROJECT.code)
    r = engine.handle_text(r.state, "ПАО Сбербанк")
    r = engine.handle_text(r.state, "Штаб-квартира на Лесной")
    if skip_optional:
        r = engine.handle_callback(r.state, engine.CB_SKIP)
    else:
        r = engine.handle_text(r.state, "3–5 этажи, кафе")
    if other_place:
        r = engine.handle_callback(r.state, CH + "2")             # Другое
        r = engine.handle_callback(r.state, CH + "1")             # Казахстан
        r = engine.handle_text(r.state, "Almaty")
    else:
        r = engine.handle_callback(r.state, CH + "0")             # Москва
    if skip_optional:
        r = engine.handle_callback(r.state, engine.CB_SKIP)       # адрес
        r = engine.handle_callback(r.state, engine.CB_SKIP)       # площадь
    else:
        r = engine.handle_text(r.state, "ул. Лесная, 9, корп. 2")
        r = engine.handle_text(r.state, "4 500 м2")
        assert r.state.awaiting == "qualifier"
        r = engine.handle_callback(r.state, engine.CB_QUALIFIER + "1")
    r = engine.handle_text(r.state, exw)                          # объём
    r = engine.handle_callback(r.state, CH + str(stage))          # стадия
    if stage != 0:
        assert r.state.step == 10
        r = engine.handle_callback(r.state, CH + "2")             # бюро нет — проект ведём мы (бюро и ГАП не спрашиваем)
    r = engine.handle_callback(r.state, CH + "1")                 # да, по тендеру
    return r


def _notification_summary(**kw):
    r = _run_phase1(**kw)
    if r.state.awaiting == "gate":
        r = engine.handle_callback(r.state, engine.CB_GATE_NO)
    return r


def _run_phase2(r):
    assert r.state.awaiting == "gate"
    assert "ещё девять вопросов" in r.screens[0].text
    r = engine.handle_callback(r.state, engine.CB_GATE_YES)
    assert r.state.phase == 2 and r.state.step == 14
    assert r.screens[0].text == "<b>Какая компания принимает решение по мебели?</b>"
    r = engine.handle_text(r.state, "ПАО Сбербанк, АХО")
    r = engine.handle_callback(r.state, engine.CB_SKIP)           # влияющие
    r = engine.handle_callback(r.state, CH + "0")                 # конкурентов нет
    r = engine.handle_callback(r.state, CH + "1")                 # заявлен другим — да
    assert r.state.awaiting == "detail"
    r = engine.handle_text(r.state, "Vitra, Arper")
    # «Бюро нет» → варианта «Документы от бюро» нет: 0 моё слово, 1 переписка, 2 встреча
    assert "Документы от бюро" not in str(r.screens[-1].buttons)
    r = engine.handle_callback(r.state, CH + "1")                 # переписка → нужен файл
    assert r.state.awaiting == "evidence"
    r = engine.handle_text(r.state, "вот письмо")
    assert r.state.awaiting == "evidence"
    r = engine.handle_text(r.state, "https://disk.yandex.ru/d/abc")
    assert "120 000 €" in r.screens[0].text                       # «Вы указывали …»
    r = engine.handle_callback(r.state, CH + "0")                 # подтверждаю
    r = engine.handle_text(r.state, "март 2027")
    # Материалы — одним экраном со списком
    assert r.state.step == 21
    text = r.screens[0].text
    assert "<b>Пришли материалы по проекту — всё, что есть:</b>\n• визуализации" in text
    assert "• квотация Andreu World" in text
    assert "спецификация бюро" not in text                        # «Бюро нет» — документов бюро не просим
    r = engine.handle_file(r.state, FILE)
    r = engine.handle_text(r.state, "https://drive.google.com/x")
    r = engine.handle_file(r.state, dict(FILE, file_id="F3", name="spec.xlsx"))
    assert len(r.state.answers["Materials"]) == 3
    r = engine.handle_callback(r.state, engine.CB_DONE)
    assert r.state.step == 22
    r = engine.handle_text(r.state, "14.10, 11:00, Мск")
    return r


def test_full_phase1_offers_prereg_then_notification_summary():
    r = _run_phase1()
    assert r.state.awaiting == "gate"
    r = engine.handle_callback(r.state, engine.CB_GATE_NO)
    summary = r.screens[0].text
    # Имя проекта дилеру не показываем, утверждений нет
    assert "ПАО Сбербанк — Штаб-квартира" not in summary
    assert "4 500 м² (из документа)" in summary
    assert "Подаётся как: <b>уведомление</b>. Решение — после проверки." in summary
    assert "трек" not in summary and "можно" not in summary.lower()
    assert "Пропущено" not in summary
    # Передумал — кнопка в сводке
    assert engine.CB_NEXT_PHASE in str(r.screens[0].buttons)
    assert engine.stage_of(r.state)[0] == "New — Notification"
    r = engine.handle_callback(r.state, engine.CB_SUBMIT)
    assert r.submit
    p = engine.build_payload(r.state)
    assert p["Country"] == "Russia" and p["City"] == "Moscow" and p["Project Stage"] == "Tender"
    assert "Object Location" not in p and "PL Volume Confirmed" not in p
    assert "Страна" not in summary and "Город" not in summary   # одна кнопка «Москва»
    assert p["PL Volume"] == 120000 and p["EXW Potential"] == 54000
    assert "EXW" not in summary                                  # пересчёт дилеру не показываем
    assert p["Project Log"] == ("Площадь: 4 500 м² (из документа)\n"
                                "Объём по прайсу: 120 000 € → EXW 54 000 € (−55%)\n"
                                "AW в проекте бюро: Бюро нет — проект ведём мы")
    assert p["Specifier"] == "Собственная проработка"
    assert "Project Name" not in p
    assert p["Dealer Project Name"] == "ПАО Сбербанк — Штаб-квартира на Лесной — 3–5 этажи, кафе"


def test_architect_selection_skips_specifier():
    r = _notification_summary(stage=0)
    assert "Specifier" not in r.state.answers
    assert "Архбюро" not in r.screens[0].text
    assert engine.build_payload(r.state)["Project Stage"] == "Architect selection"


def test_skips_listed_gate_still_offered_and_address_label():
    r = _run_phase1(skip_optional=True)
    # Предрегистрацию предлагаем и с пропусками — с пометкой, что их сначала дополним
    assert r.state.awaiting == "gate"
    assert "Сначала дополним: Часть объекта / поставки, Адрес, Площадь." in r.screens[0].text
    r = engine.handle_callback(r.state, engine.CB_GATE_NO)
    text = r.screens[0].text
    assert "Адрес: не указан" in text
    assert "Пропущено: Часть объекта / поставки, Адрес, Площадь." in text
    assert engine.COMPLETENESS_NOTE in text
    assert "Для предрегистрации не хватает" in text
    assert engine.CB_FILL in str(r.screens[0].buttons)
    assert engine.CB_NEXT_PHASE in str(r.screens[0].buttons)
    assert engine.handle_callback(r.state, engine.CB_SUBMIT).submit


def test_prereg_prefills_skipped_phase1_without_skip():
    r = _run_phase1(skip_optional=True)
    r = engine.handle_callback(r.state, engine.CB_GATE_YES)
    assert r.state.step == 2 and r.state.phase == 1               # сначала пропущенное
    assert "Пропустить" not in str(r.screens[-1].buttons)
    r = engine.handle_callback(r.state, engine.CB_SKIP)           # пропустить нельзя
    assert r.state.step == 2
    r = engine.handle_text(r.state, "Лобби")
    assert r.state.step == 6
    r = engine.handle_text(r.state, "ул. Лесная, 9")
    r = engine.handle_text(r.state, "4500")
    r = engine.handle_callback(r.state, engine.CB_QUALIFIER + "0")
    assert r.state.phase == 2 and r.state.step == 14 and not r.state.prefill
    assert engine.stage_of(r.state)[0] == "New — Pre-registration"


def test_fill_completes_and_offers_prereg():
    r = _run_phase1(skip_optional=True)
    r = engine.handle_callback(r.state, engine.CB_GATE_NO)
    r = engine.handle_callback(r.state, engine.CB_FILL)
    assert r.state.step == 2
    r = engine.handle_text(r.state, "Лобби")
    assert r.state.step == 6
    r = engine.handle_text(r.state, "ул. Лесная, 9")
    r = engine.handle_text(r.state, "4500")
    r = engine.handle_callback(r.state, engine.CB_QUALIFIER + "0")
    # От предрегистрации уже отказался — сводка, но с кнопкой «Подать на предрегистрацию»
    assert r.state.awaiting == "confirm" and engine.CB_NEXT_PHASE in str(r.screens[0].buttons)


def test_fill_skip_again_returns_to_summary():
    r = _run_phase1(skip_optional=True)
    r = engine.handle_callback(r.state, engine.CB_GATE_NO)
    r = engine.handle_callback(r.state, engine.CB_FILL)
    for _ in range(3):
        r = engine.handle_callback(r.state, engine.CB_SKIP)
    assert r.state.awaiting == "confirm"


def test_skip_only_on_three_phase1_questions():
    skippable = [q.key for q in NEW_PROJECT.questions if q.optional and q.phase == 1]
    assert skippable == ["Object Part", "Object Address", "Area m2"]


def test_simplified_track_notification_only():
    r = _run_phase1(exw="25 000")
    assert r.state.awaiting == "gate"
    assert r.screens[0].text.startswith(
        "Давай зарегистрируем! Для этого отправь запрос на квотацию и пришли мне номер квотации.")
    assert "Номер квотации есть" in str(r.screens[0].buttons)
    r = engine.handle_callback(r.state, engine.CB_GATE_NO)
    assert r.state.awaiting == "confirm"
    assert "Подаётся как: <b>уведомление</b>" in r.screens[0].text
    assert "Simplified" not in r.screens[0].text
    assert engine.CB_NEXT_PHASE in str(r.screens[0].buttons)   # «Прислать номер квотации»
    assert engine.build_payload(r.state)["PPR Track"] == "Simplified (<30k)"


def test_simplified_quote_number_registers():
    r = _run_phase1(exw="25 000")
    r = engine.handle_callback(r.state, engine.CB_GATE_YES)
    assert r.state.phase == 1 and engine._current_q(r.state).key == "Simplified Quote №"
    assert "<b>Номер квотации?</b>" in r.screens[-1].text
    r = engine.handle_text(r.state, "QT-2026-5501")
    assert r.state.awaiting == "confirm"                          # второй раз не предлагаем
    assert engine.stage_of(r.state)[0] == "Quotation — Simplified"
    assert "регистрация по упрощённой схеме" in r.screens[-1].text
    p = engine.build_payload(r.state)
    assert p["Quotation / Proforma №"] == "QT-2026-5501" and "Simplified Quote №" not in p


def test_prereg_then_registration_full_flow():
    r = _run_phase2(_run_phase1())
    assert r.state.awaiting == "gate"
    assert "квотация" in r.screens[0].text
    r = engine.handle_callback(r.state, engine.CB_GATE_NO)
    assert "Подаётся как: <b>предрегистрация</b>" in r.screens[0].text
    assert engine.stage_of(r.state)[0] == "New — Pre-registration"
    r = engine.handle_callback(r.state, engine.CB_NEXT_PHASE)     # передумал — регистрация
    assert r.state.phase == 3 and r.state.step == 23
    r = engine.handle_text(r.state, "Q-2026-1187")
    r = engine.handle_text(r.state, "Иванов И., ПАО Сбербанк, директор АХО")
    assert "<b>Есть ведомость с Andreu World внутри?</b>" in r.screens[0].text
    r = engine.handle_file(r.state, dict(FILE, file_id="F2", name="vedomost.pdf"))
    r = engine.handle_callback(r.state, engine.CB_DONE)
    r = engine.handle_callback(r.state, CH + "0")                 # тендерного задания нет
    r = engine.handle_callback(r.state, CH + "0")                 # запроса на закупку нет
    r = engine.handle_callback(r.state, CH + "0")                 # прочего нет
    assert r.state.step == 29
    r = engine.handle_text(r.state, "июнь 2027")
    r = engine.handle_text(r.state, "16.10, 15:00, Мск")
    assert r.state.awaiting == "confirm"
    assert "Подаётся как: <b>регистрация</b>" in r.screens[0].text
    assert engine.stage_of(r.state)[0] == "New — Registration"
    r = engine.handle_callback(r.state, engine.CB_SUBMIT)
    assert r.submit
    p = engine.build_payload(r.state)
    assert p["Parallel Registrations"] == "Vitra, Arper"
    assert p["Contact Level"] == "Verifiable (email/contact)"
    assert p["Contact Evidence"] == [{"kind": "link", "url": "https://disk.yandex.ru/d/abc"}]
    assert p["PL Volume"] == 120000 and p["EXW Potential"] == 54000
    assert p["Quotation / Proforma №"] == "Q-2026-1187"
    assert "Competing Dealers" in p and p["Competing Dealers"] == "Нет"
    log = p["Project Log"].splitlines()
    assert "AW в проекте бюро: Бюро нет — проект ведём мы" in log
    assert "Звонок 30 минут: 16.10, 15:00, Мск" in log
    names = [it["name"] for _, it in engine.collected_files(r.state)]
    assert names == ["plan.pdf", "spec.xlsx", "vedomost.pdf"]
    assert "Layouts" not in p and "Specification For Quotation" not in p


def test_confirmed_volume_change_logged():
    r = _run_phase1()
    r = engine.handle_callback(r.state, engine.CB_GATE_YES)
    st = r.state
    st.answers.update({"AW Introduced By": "x", "Decision Maker Company": "y"})
    st.step = 19
    r = engine.handle_text(st, "150к")
    p = engine.build_payload(r.state)
    assert p["PL Volume"] == 150000 and p["EXW Potential"] == 67500
    assert "Объём по прайсу при уведомлении: 120 000 €" in p["Project Log"]


def test_typed_answer_on_detail_choice():
    r = _run_phase1()
    r = engine.handle_callback(r.state, engine.CB_GATE_YES)
    st = r.state
    st.step = 16
    r = engine.handle_text(st, "Офис Солюшнз")                    # сразу текстом, без кнопки «Да»
    assert r.state.answers["Competing Dealers"] == "Офис Солюшнз"


def test_file_not_expected():
    r = engine.start(NEW_PROJECT.code)
    r = engine.handle_file(r.state, FILE)
    assert "не нужен" in r.screens[0].text and not r.state.answers


def test_stage_change_to_other_asks_specifier():
    r = _notification_summary(stage=0)
    r = engine.handle_callback(r.state, engine.CB_EDIT)
    r = engine.handle_callback(r.state, engine.CB_EDIT_STEP + "9")
    r = engine.handle_callback(r.state, CH + "2")                 # Проектирование
    assert r.state.step == 10 and r.state.awaiting == "answer"  # AW в проекте бюро?
    r = engine.handle_callback(r.state, CH + "0")                 # да, заложен бюро
    assert engine._current_q(r.state).key == "Specifier"
    r = engine.handle_text(r.state, "UNK Project")
    assert engine._current_q(r.state).key == "Lead Architect"   # за бюро — ГАП
    r = engine.handle_text(r.state, "Иванов")
    assert r.state.awaiting == "confirm"


def test_stage_change_to_architect_drops_specifier():
    r = _notification_summary()
    r = engine.handle_callback(r.state, engine.CB_EDIT)
    r = engine.handle_callback(r.state, engine.CB_EDIT_STEP + "9")
    r = engine.handle_callback(r.state, CH + "0")
    assert r.state.awaiting == "confirm"
    assert "AW Origin" not in r.state.answers and "Specifier" not in engine.build_payload(r.state)


def test_text_matches_choice_label():
    r = _notification_summary()
    r = engine.handle_callback(r.state, engine.CB_EDIT)
    r = engine.handle_callback(r.state, engine.CB_EDIT_STEP + "13")
    r = engine.handle_text(r.state, "нет, только AW")
    assert r.state.answers["AW Alternatives"] == "No — AW only"
    assert r.state.awaiting == "confirm"


def test_required_question_has_no_skip():
    r = engine.start(NEW_PROJECT.code)
    r = engine.handle_callback(r.state, engine.CB_SKIP)
    assert r.state.step == 0 and "End Client" not in r.state.answers


def test_bad_answer_repeats_question():
    r = engine.start(NEW_PROJECT.code)
    st = r.state
    st.step = 8  # объём AW
    r = engine.handle_text(st, "не знаю")
    assert r.state.step == 8
    assert r.screens[0].text.startswith("⚠️")


def test_state_roundtrip_json():
    r = _run_phase2(_run_phase1())
    restored = DialogState.from_json(r.state.to_json())
    assert restored == r.state
    assert DialogState.from_json("") is None
    assert DialogState.from_json("{broken") is None


def test_month_picker_year_then_month():
    from datetime import date
    r = _run_phase1()
    r = engine.handle_callback(r.state, engine.CB_GATE_YES)
    st = r.state
    st.step = 20  # месяц размещения заказа
    screen = engine.question_screen(st)
    y = date.today().year + 1
    assert f"{engine.CB_YEAR}{y}" in str(screen.buttons)
    r = engine.handle_callback(st, f"{engine.CB_YEAR}{y}")
    assert r.state.awaiting == "month" and len(r.screens[0].buttons) == 5
    r = engine.handle_callback(r.state, engine.CB_YEAR_BACK)      # передумал с годом
    assert r.state.awaiting == "answer"
    r = engine.handle_callback(r.state, f"{engine.CB_YEAR}{y}")
    r = engine.handle_callback(r.state, engine.CB_MONTH + "3")
    assert r.state.answers["Order Month"] == f"{y}-03-01"
    assert r.state.step == 21
    p = engine.build_payload(r.state)
    assert f"Месяц размещения заказа: 03.{y}" in p["Project Log"]


def test_month_typed_while_grid_open():
    r = _run_phase1()
    r = engine.handle_callback(r.state, engine.CB_GATE_YES)
    st = r.state
    st.step = 20
    r = engine.handle_callback(st, f"{engine.CB_YEAR}2027")
    r = engine.handle_text(r.state, "Q2 2027")
    assert r.state.answers["Order Month"] == "2027-04-01"


def test_materials_one_screen_can_skip():
    r = _run_phase1()
    r = engine.handle_callback(r.state, engine.CB_GATE_YES)
    st = r.state
    st.step = 21
    labels = [b[0] for row in engine.question_screen(st).buttons for b in row]
    assert labels == ["Пропустить"]
    r = engine.handle_callback(st, engine.CB_SKIP)
    assert r.state.step == 22
    st.answers["AW Origin"] = "Да, заложен бюро"
    st.step = 21
    assert "• ведомость или спецификация бюро" in engine.question_screen(st).text


def test_slot_helpers_timezones():
    from datetime import date
    from bot import slots
    # Пятница → следующие 4 рабочих дня, выходные пропускаем
    days = slots.next_workdays(date(2026, 10, 9))
    assert [d.isoformat() for d in days] == ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"]
    assert slots.day_label(days[0]) == "Пн 12.10"
    # Окно 10–12 Валенсии в местном времени. Летнее время Валенсии (UTC+2): Москва +1 ч, Алматы +3 ч
    assert slots.window_label(date(2026, 10, 12), 0, "Russia") == "11:00–13:00"
    assert slots.window_label(date(2026, 10, 12), 0, "Kazakhstan") == "13:00–15:00"
    # После 25.10 Валенсия на зимнем (UTC+1): Москва +2 ч
    assert slots.window_label(date(2026, 10, 26), 0, "Russia") == "12:00–14:00"
    assert slots.window_label(date(2026, 10, 12), 0, "Other") == "10:00–12:00"
    assert slots.slot_value(date(2026, 10, 14), 2, "Russia") == "14.10.2026 Ср, 16:00–18:00 Мск (Валенсия 15:00–17:00)"
    assert slots.slot_value(date(2026, 10, 14), 2, "Other") == "14.10.2026 Ср, 15:00–17:00 Валенсия"


def test_slot_picker_day_then_window():
    from bot import slots
    r = _run_phase1()
    r = engine.handle_callback(r.state, engine.CB_GATE_YES)
    st = r.state
    st.step = 22  # звонок 15 минут
    screen = engine.question_screen(st)
    day = slots.next_workdays()[1]
    assert f"{engine.CB_DAY}{day.isoformat()}" in str(screen.buttons)
    r = engine.handle_callback(st, f"{engine.CB_DAY}{day.isoformat()}")
    assert r.state.awaiting == "slot"
    assert "(Мск)" in r.screens[0].text
    assert "Другой день" not in str(r.screens[0].buttons)
    r = engine.handle_callback(r.state, engine.CB_WINDOW + "2")
    value = r.state.answers["Call Slot 15"]
    assert value.startswith(f"{day:%d.%m.%Y}") and "Мск" in value and "(Валенсия 15:00–17:00)" in value
    assert r.state.awaiting == "confirm"


def test_slot_free_text_accepted():
    r = _run_phase1()
    r = engine.handle_callback(r.state, engine.CB_GATE_YES)
    st = r.state
    st.step = 22
    r = engine.handle_text(st, "Любой день после 20.10, утро")
    assert r.state.answers["Call Slot 15"] == "Любой день после 20.10, утро"


def test_slot_text_after_day_chosen():
    from bot import slots
    r = _run_phase1()
    r = engine.handle_callback(r.state, engine.CB_GATE_YES)
    st = r.state
    st.step = 22
    r = engine.handle_callback(st, f"{engine.CB_DAY}{slots.next_workdays()[0].isoformat()}")
    r = engine.handle_text(r.state, "лучше в пятницу после обеда")
    assert r.state.answers["Call Slot 15"] == "лучше в пятницу после обеда"


def test_stale_qualifier_state_heals():
    r = _run_phase1()
    r = engine.handle_callback(r.state, engine.CB_GATE_YES)
    st = r.state
    st.step, st.awaiting = 21, "qualifier"  # у материалов уточнения нет — состояние устарело
    r = engine.handle_callback(st, engine.CB_QUALIFIER + "0")
    assert r.state.awaiting == "answer" and r.state.step == 21
    assert r.screens and r.screens[-1].text


def test_third_party_docs_all_no_requires_one():
    r = _run_phase2(_run_phase1())
    r = engine.handle_callback(r.state, engine.CB_GATE_YES)
    st = r.state
    st.step = 25
    for _ in range(4):
        r = engine.handle_callback(st, CH + "0")
        st = r.state
    assert "Документы третьей стороны: нужен хотя бы один файл или ссылка" in r.screens[0].text
    assert r.state.step == 25


def test_other_place_asks_country_and_city():
    r = _notification_summary(other_place=True)
    p = engine.build_payload(r.state)
    assert p["Country"] == "Kazakhstan" and p["City"] == "Almaty"
    assert "Страна: Казахстан" in r.screens[0].text


def test_location_change_to_moscow_drops_country_city():
    r = _notification_summary(other_place=True)
    r = engine.handle_callback(r.state, engine.CB_EDIT)
    r = engine.handle_callback(r.state, engine.CB_EDIT_STEP + "3")
    r = engine.handle_callback(r.state, CH + "0")                 # Москва
    assert r.state.awaiting == "confirm"
    assert "Country" not in r.state.answers and "City" not in r.state.answers
    assert engine.build_payload(r.state)["City"] == "Moscow"


def test_threshold_is_on_price_list():
    assert "трек" not in _run_phase1(exw="30 000").screens[0].text  # 30 000 по прайсу — Full, гейт
    assert "предрегистрацию" in _run_phase1(exw="30 000").screens[0].text
    assert "номер квотации" in _run_phase1(exw="29 999").screens[0].text  # упрощённая схема


def test_city_buttons_by_country():
    r = engine.start(NEW_PROJECT.code)
    st = r.state
    st.answers.update({"End Client": "x", "Object Name": "y", "Object Part": "z"})
    st.step = 3
    r = engine.handle_callback(st, CH + "2")                      # Другое
    r = engine.handle_callback(r.state, CH + "6")                 # Беларусь
    labels = [b[0] for row in r.screens[0].buttons for b in row]
    assert labels == ["Минск", "Другой город"]
    r = engine.handle_callback(r.state, CH + "1")                 # Другой город
    assert r.state.awaiting == "detail"
    r = engine.handle_text(r.state, "Гродно")
    assert r.state.answers["City"] == "Гродно" and r.state.step == 6


def test_city_button_stores_latin_and_shows_russian():
    r = engine.start(NEW_PROJECT.code)
    st = r.state
    st.step = 4
    st.answers["Object Location"] = "other"
    r = engine.handle_callback(st, CH + "1")                      # Казахстан
    r = engine.handle_callback(r.state, CH + "0")                 # Алматы
    assert r.state.answers["City"] == "Almaty"
    from bot.engine import _display
    from bot.scenarios.new_project import _BY_KEY
    assert _display(_BY_KEY["City"], r.state.answers) == "Алматы"


def test_country_change_resets_city():
    r = _notification_summary(other_place=True)                   # Казахстан / Almaty
    r = engine.handle_callback(r.state, engine.CB_EDIT)
    r = engine.handle_callback(r.state, engine.CB_EDIT_STEP + "4")
    r = engine.handle_callback(r.state, CH + "0")                 # Россия
    assert "City" not in r.state.answers and r.state.step == 5    # город спрашивается заново
    assert "Краснодар" in str(r.screens[0].buttons)


TARGET = {"_target": {"id": "recPROJECT00000001", "name": "Medall Clinic Levashovsky"}}


def test_news_auto_submits_with_text_and_audio():
    r = engine.start("project_news", dict(TARGET))
    assert "Сообщить новость</b> · Medall Clinic Levashovsky" in r.screens[0].text
    st = r.state
    st.answers["Dealer News Audio"] = [{"kind": "file", "file_id": "V1", "name": "voice_1.ogg", "size": 10,
                                        "mime": "audio/ogg"}]
    r = engine.handle_text(st, "Клиент утвердил концепцию, тендер в декабре")
    assert r.submit and not r.screens                             # без сводки
    assert engine.stage_of(r.state)[0] in ("Update — news", "Update — fields")
    p = engine.build_payload(r.state)
    assert p["Project Log"] == "Новости от дилера: Клиент утвердил концепцию, тендер в декабре"
    assert p["Dealer News"] == "Клиент утвердил концепцию, тендер в декабре"
    assert [it["name"] for _, it in engine.collected_files(r.state)] == ["voice_1.ogg"]


def test_won_with_number_and_skip():
    r = engine.start("project_won", dict(TARGET))
    r2 = engine.handle_text(r.state, "PF-148928")
    assert r2.submit and engine.build_payload(r2.state)["Order / Proforma №"] == "PF-148928"
    assert engine.stage_of(r2.state)[0] == "Update — Won"
    r = engine.start("project_won", dict(TARGET))
    r3 = engine.handle_callback(r.state, engine.CB_SKIP)
    assert r3.submit and "Order / Proforma №" not in engine.build_payload(r3.state)


def test_lost_reason_mapping_and_other():
    r = engine.start("project_lost", dict(TARGET))
    r = engine.handle_callback(r.state, CH + "1")
    assert r.submit
    p = engine.build_payload(r.state)
    assert p == {"Dealer Project Name": "Medall Clinic Levashovsky", "Lost Reason": "Lost to cheaper offer"}
    assert engine.stage_of(r.state)[0] == "Update — Lost"
    r = engine.start("project_lost", dict(TARGET))
    r = engine.handle_callback(r.state, CH + "4")                 # Другое
    assert r.state.awaiting == "detail"
    r = engine.handle_text(r.state, "Клиент купил б/у мебель")
    p = engine.build_payload(r.state)
    assert r.submit and "Lost Reason" not in p
    assert p["Project Log"] == "Причина проигрыша (со слов дилера): Клиент купил б/у мебель"


FULL_LOG = ("AW в проекте бюро: Да, заложен бюро\nРешение по мебели принимает: клиент\n"
            "Влияют на решение: ГП\nКонкурирующие дилеры: Нет")


def test_fill_asks_only_gaps_in_phase_order():
    from bot.airtable_ids import Projects as P
    from bot.scenarios.update_project import fill_preset
    fields = {P.NAME: "X", P.END_CLIENT: ["rec1"], P.OBJECT_NAME: "Obj", P.OBJECT_PART: "Lobby",
              P.COUNTRY: "Russia", P.CITY: "Moscow", P.OBJECT_ADDRESS: "Lesnaya 9", P.PROJECT_STAGE: "Design",
              P.SPECIFIER: ["rec2"], P.EXW_POTENTIAL: 50000, P.AW_ALTERNATIVES: "No — AW only",
              P.EXPECTED_ORDER_DATE: "2027-03-01", P.CONTACT_LEVEL: "Declared", P.PARALLEL_REGISTRATIONS: "нет",
              P.DOSSIER_FILES: [{"id": "att1"}], P.PROJECT_LOG: FULL_LOG}
    # Уведомление, всё заполнено → только приём файлов
    r = engine.start("project_fill", fill_preset(dict(TARGET["_target"]), fields, "notified"))
    assert r.state.step == len(engine._qs(r.state)) - 1 and "Все данные" in r.screens[1].text
    r = engine.handle_callback(r.state, engine.CB_DONE)          # пусто — нельзя
    assert r.state.awaiting == "answer"
    r = engine.handle_text(r.state, "https://disk.yandex.ru/new")
    r = engine.handle_callback(r.state, engine.CB_DONE)
    assert r.state.awaiting == "confirm"
    assert engine.build_payload(r.state)["Extra Materials"] == [{"kind": "link", "url": "https://disk.yandex.ru/new"}]
    # Нет адреса и квотации у предрегистрации → только они, по порядку фаз
    f2 = dict(fields)
    del f2[P.OBJECT_ADDRESS]
    preset = fill_preset(dict(TARGET["_target"]), f2, "pre_registered")
    assert preset["_ask"] == ["Object Address", "Quotation / Proforma №"]
    r = engine.start("project_fill", preset)
    assert engine._current_q(r.state).key == "Object Address"
    r = engine.handle_text(r.state, "ул. Лесная, 9")              # адрес — можно было и пропустить
    assert engine._current_q(r.state).key == "Quotation / Proforma №"
    r = engine.handle_text(r.state, "Q-77")
    assert r.state.awaiting == "confirm"
    p = engine.build_payload(r.state)
    assert p == {"Object Address": "ул. Лесная, 9", "Quotation / Proforma №": "Q-77",
                 "Dealer Project Name": "Medall Clinic Levashovsky"}


def test_fill_location_gap_asks_where():
    from bot.airtable_ids import Projects as P
    from bot.scenarios.update_project import gaps_for
    keys, known = gaps_for({P.PROJECT_STAGE: "Architect selection"}, "registered")
    assert keys[:3] == ["End Client", "Object Name", "Object Part"]
    assert "Object Location" in keys and "Specifier" not in keys   # бюро на выборе архитектора не нужно
    assert known == {"Project Stage": "Architect selection"}


def test_project_list_sorting_and_labels():
    from datetime import date, timedelta
    from bot.airtable_repo import DealerProject
    from bot.main import _button_label, _sorted
    today = date.today()
    mk = lambda name, st, days: DealerProject(
        name, name, st, (today + timedelta(days=days)).isoformat() if days is not None else None, "Full", {})
    items = [mk("Notif B", "notified", None), mk("Reg 40", "registered", 40),
             mk("Pre 12", "pre_registered", 12), mk("Notif A", "notified", None)]
    order = [p.name for p in _sorted(items)]
    assert order == ["Pre 12", "Reg 40", "Notif A", "Notif B"]
    assert _button_label(items[2]) in ("Pre 12 · предрегистрация · 12 дн.", "Pre 12 · предрегистрация · 11 дн.",
                                       "Pre 12 · предрегистрация · 13 дн.")
    assert _button_label(items[0]) == "Notif B · уведомление"


def _to_specifier():
    r = _run_phase1()
    r = engine.handle_callback(r.state, engine.CB_GATE_NO)
    r = engine.handle_callback(r.state, engine.CB_EDIT)
    r = engine.handle_callback(r.state, engine.CB_EDIT_STEP + "10")
    return engine.handle_callback(r.state, CH + "0")              # AW заложен бюро → название бюро


def test_bureau_then_architect_unknown():
    r = _to_specifier()
    r = engine.handle_text(r.state, "UNK Project")
    assert engine._current_q(r.state).key == "Lead Architect"
    labels = [b[0] for row in r.screens[0].buttons for b in row]
    assert labels == ["Пока не знаю"]
    r = engine.handle_callback(r.state, engine.CB_SKIP)
    assert r.state.awaiting == "confirm"
    text = r.screens[0].text
    assert "ГАП: пока не известен" in text and "Пропущено: ГАП." in text
    p = engine.build_payload(r.state)
    assert p["Specifier"] == "UNK Project" and "Lead Architect" not in p


def test_solo_architect_name_required():
    r = _to_specifier()
    r = engine.handle_callback(r.state, CH + "0")                 # Частный архитектор
    q = engine._current_q(r.state)
    assert q.key == "Lead Architect" and q.prompt == "Как зовут архитектора?" and not q.optional
    assert "Пока не знаю" not in str(r.screens[0].buttons)
    r = engine.handle_callback(r.state, engine.CB_SKIP)           # пропустить нельзя
    assert engine._current_q(r.state).key == "Lead Architect"
    r = engine.handle_text(r.state, "Иван Петров")
    p = engine.build_payload(r.state)
    assert p["Specifier"] == "Иван Петров" and p["Specifier Type"] == "Частный архитектор"
    assert p["Lead Architect"] == "Иван Петров"


def test_own_design_skips_bureau_and_architect():
    r = _notification_summary()                                   # «Бюро нет — проект ведём мы»
    text = r.screens[0].text
    assert "AW в проекте бюро: Бюро нет — проект ведём мы" in text and "ГАП" not in text and "Архбюро" not in text
    assert engine.build_payload(r.state)["Specifier"] == "Собственная проработка"


def test_dealer_offers_aw_needs_bureau_not_architect():
    r = _run_phase1()
    r = engine.handle_callback(r.state, engine.CB_GATE_NO)
    r = engine.handle_callback(r.state, engine.CB_EDIT)
    r = engine.handle_callback(r.state, engine.CB_EDIT_STEP + "10")
    r = engine.handle_callback(r.state, CH + "1")                 # нет — AW предлагаем мы взамен
    assert engine._current_q(r.state).key == "Specifier"
    r = engine.handle_text(r.state, "UNK Project")
    assert r.state.awaiting == "confirm" and "ГАП" not in r.screens[0].text
    p = engine.build_payload(r.state)
    # Specifier — тот, кто заложил AW: дилер; бюро — строкой в логе
    assert p["Specifier"] == "Собственная проработка" and "Lead Architect" not in p
    assert "Архбюро (AW не закладывало): UNK Project" in p["Project Log"]
    assert "AW в проекте бюро: Нет — AW предлагаем мы взамен" in p["Project Log"]


def test_info_reply_flow():
    r = engine.start("info_reply", {"_target": {"id": None, "name": "Пао — Символ"}, "_reply_to": "recREG"})
    r = engine.handle_text(r.state, "Планировки будут в понедельник")
    r = engine.handle_callback(r.state, CH + "0")                 # без файлов
    assert r.submit
    p = engine.build_payload(r.state)
    assert p["In Reply To"] == "recREG" and p["Dealer Project Name"] == "Пао — Символ"
    assert p["Project Log"] == "Ответ дилера на запрос: Планировки будут в понедельник"
    assert engine.stage_of(r.state)[0] in ("Update — news", "Update — fields")


def test_fill_flat_numbering_across_phases():
    from bot.airtable_ids import Projects as P
    from bot.scenarios.update_project import fill_preset
    fields = {P.END_CLIENT: ["c"], P.OBJECT_NAME: "o", P.OBJECT_PART: "p", P.COUNTRY: "Russia", P.CITY: "Moscow",
              P.PROJECT_STAGE: "Design", P.SPECIFIER: ["s"], P.EXW_POTENTIAL: 1, P.AW_ALTERNATIVES: "No — AW only",
              P.EXPECTED_ORDER_DATE: "2027-01-01", P.DOSSIER_FILES: [{"id": "a"}], P.PROJECT_LOG: FULL_LOG}
    r = engine.start("project_fill", fill_preset({"id": "recP", "name": "X"}, fields, "notified"))
    assert r.screens[1].text.startswith("<b>Адрес объекта?</b>")
    r = engine.handle_callback(r.state, engine.CB_SKIP)
    assert "<b>Запрашиваемый объём заявлен тобой у других производителей мебели?</b>" in r.screens[-1].text


def test_fill_asks_bureau_on_architect_selection_and_log_answers():
    from bot.airtable_ids import Projects as P
    from bot.scenarios.update_project import fill_preset
    fields = {P.END_CLIENT: ["c"], P.OBJECT_NAME: "o", P.OBJECT_PART: "p", P.COUNTRY: "Russia", P.CITY: "Moscow",
              P.OBJECT_ADDRESS: "a", P.PROJECT_STAGE: "Architect selection", P.EXW_POTENTIAL: 1,
              P.AW_ALTERNATIVES: "No — AW only", P.EXPECTED_ORDER_DATE: "2027-01-01", P.CONTACT_LEVEL: "Declared",
              P.PARALLEL_REGISTRATIONS: "нет", P.DOSSIER_FILES: [{"id": "a"}],
              P.PROJECT_LOG: "Влияют на решение: ГП"}
    preset = fill_preset({"id": "recP", "name": "X"}, fields, "notified")
    assert preset["_ask"] == ["Specifier", "Lead Architect", "Decision Maker Company", "Competing Dealers"]
    r = engine.start("project_fill", preset)
    assert engine._current_q(r.state).key == "Specifier"
    assert ("Бюро ещё не выбрано", "ch:1") in [b for row in r.screens[-1].buttons for b in row]
    r = engine.handle_callback(r.state, "ch:1")
    assert engine._current_q(r.state).key == "Decision Maker Company"   # ГАП не спрашиваем
    r = engine.handle_text(r.state, "Петрович")
    r = engine.handle_callback(r.state, "ch:0")
    assert r.state.awaiting == "confirm"
    p = engine.build_payload(r.state)
    assert "Specifier" not in p
    assert p["Project Log"].splitlines() == [
        "Решение по мебели принимает: Петрович", "Конкурирующие дилеры: Нет", "Архбюро: ещё не выбрано"]
    # Simplified до предрегистрации не доходит — вопросов из лога нет
    fields[P.PPR_TRACK] = "Simplified (<30k)"
    assert "Decision Maker Company" not in fill_preset({"id": "recP", "name": "X"}, fields, "notified")["_ask"]


def test_last_step_hint_only_on_last_material():
    from bot.airtable_ids import Projects as P
    from bot.scenarios.update_project import fill_preset
    fields = {P.END_CLIENT: ["c"], P.OBJECT_NAME: "o", P.OBJECT_PART: "p", P.COUNTRY: "Russia", P.CITY: "Moscow",
              P.OBJECT_ADDRESS: "a", P.PROJECT_STAGE: "Design", P.SPECIFIER: ["s"], P.EXW_POTENTIAL: 1,
              P.AW_ALTERNATIVES: "No — AW only", P.EXPECTED_ORDER_DATE: "2027-01-01", P.CONTACT_LEVEL: "Declared",
              P.PARALLEL_REGISTRATIONS: "нет", P.PROJECT_LOG: FULL_LOG}
    r = engine.start("project_fill", fill_preset({"id": "recP", "name": "X"}, fields, "notified"))
    text = r.screens[-1].text
    assert text.startswith("<b>Пришли материалы по проекту — всё, что есть:</b>")
    assert "Последний вопрос" not in text                         # единственный вопрос — без подсказки
    r = engine.handle_callback(r.state, engine.CB_SKIP)
    assert r.state.awaiting == "confirm"


def test_past_month_rejected_and_hidden():
    from datetime import date
    from bot.scenarios.base import PAST_MONTH, ParseError, parse_answer
    from bot.scenarios.new_project import QUESTIONS
    q = next(x for x in QUESTIONS if x.key == "Order Month")
    try:
        parse_answer(q, "01.2020")
        raise AssertionError("прошлый месяц принят")
    except ParseError as e:
        assert str(e) == PAST_MONTH
    state = engine.DialogState(scenario="new_project", pick_year=date.today().year)
    labels = [b[0] for row in engine.month_screen(state).buttons for b in row]
    assert labels[0] == engine.MONTHS[date.today().month - 1]


def test_fill_summary_is_flat():
    from bot.airtable_ids import Projects as P
    from bot.scenarios.update_project import fill_preset
    fields = {P.END_CLIENT: ["c"], P.OBJECT_NAME: "o", P.OBJECT_PART: "p", P.COUNTRY: "Russia", P.CITY: "Moscow",
              P.PROJECT_STAGE: "Design", P.SPECIFIER: ["s"], P.EXW_POTENTIAL: 1, P.AW_ALTERNATIVES: "No — AW only",
              P.EXPECTED_ORDER_DATE: "2027-01-01", P.CONTACT_LEVEL: "Declared", P.DOSSIER_FILES: [{"id": "a"}],
              P.PROJECT_LOG: FULL_LOG}
    r = engine.start("project_fill", fill_preset({"id": "recP", "name": "X"}, fields, "notified"))
    r = engine.handle_text(r.state, "ул. Лесная, 9")
    r = engine.handle_callback(r.state, "ch:0")
    text = r.screens[-1].text
    assert "1. Адрес: ул. Лесная, 9\n2. Заявлен другим производителям: Нет" in text
    assert "<b>Уведомление</b>" not in text and "<b>Предрегистрация</b>" not in text


def test_fill_asks_aw_origin_when_missing_in_log():
    from bot.airtable_ids import Projects as P
    from bot.scenarios.update_project import fill_preset
    fields = {P.END_CLIENT: ["c"], P.OBJECT_NAME: "o", P.OBJECT_PART: "p", P.COUNTRY: "Russia", P.CITY: "Moscow",
              P.OBJECT_ADDRESS: "a", P.PROJECT_STAGE: "Design", P.SPECIFIER: ["s"], P.EXW_POTENTIAL: 1,
              P.AW_ALTERNATIVES: "No — AW only", P.EXPECTED_ORDER_DATE: "2027-01-01", P.CONTACT_LEVEL: "Declared",
              P.PARALLEL_REGISTRATIONS: "нет", P.DOSSIER_FILES: [{"id": "a"}],
              P.PROJECT_LOG: FULL_LOG.replace("AW в проекте бюро: Да, заложен бюро\n", "")}
    preset = fill_preset({"id": "recP", "name": "X"}, fields, "notified")
    assert preset["_ask"] == ["AW Origin"]
    r = engine.start("project_fill", preset)
    r = engine.handle_callback(r.state, CH + "1")                 # нет — AW предлагаем мы взамен
    assert r.state.awaiting == "confirm"                          # бюро уже в проекте, ГАП не нужен
    assert engine.build_payload(r.state)["Project Log"] == "AW в проекте бюро: Нет — AW предлагаем мы взамен"


def test_quote_number_for_simplified_project():
    r = engine.start("project_quote", {"_target": {"id": "recP", "name": "MedallClinic SPB"}})
    assert "<b>Номер квотации?</b>" in r.screens[-1].text
    r = engine.handle_text(r.state, "QT-2026-77")
    assert r.submit
    assert engine.stage_of(r.state)[0] == "Quotation — Simplified"
    p = engine.build_payload(r.state)
    assert p["Quotation / Proforma №"] == "QT-2026-77"
    assert p["Project Log"] == "Квотация по упрощённой схеме: № QT-2026-77"
