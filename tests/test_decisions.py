from bot.airtable_ids import Projects as P, Registrations as R
from bot.decisions import plan

DEALER, OTHER1, OTHER2 = "recDEALER", "recOTHER1", "recOTHER2"
FULL = {P.NAME: "Petrovitch Office", P.EXW_POTENTIAL: 45000, P.EXPECTED_ORDER_DATE: "2027-03-01",
        P.OBJECT_ADDRESS: "Lesnaya 9", P.SPECIFIER: ["recB"], P.CONTACT_LEVEL: "Declared",
        P.AW_ALTERNATIVES: "No — AW only", P.PARALLEL_REGISTRATIONS: "нет", P.DOSSIER_FILES: [{"id": "a"}]}


def reg(decision="Accept", rtype="New — Notification", note=None, status="PENDING", processed=None):
    # DEALER — и компания (поле Dealer), и менеджер (Submitted By): статусы теперь у менеджера
    r = {R.DECISION: decision, R.REQUEST_TYPE: rtype, R.REG_STATUS: status, R.DEALER: [DEALER], R.SUBMITTED_BY: [DEALER],
         R.DEALER_PROJECT_NAME: "Петрович — офис", R.TARGET_PROJECT: ["recP"]}
    if note:
        r[R.DECISION_NOTE] = note
    if processed:
        r[R.BOT_PROCESSED] = processed
    return r


def test_notification_reports_registered_without_names():
    pl = plan(reg(), {**FULL, P.REGISTERED: [OTHER1], P.PRE_REGISTERED: [OTHER2]})
    assert pl.project_updates == {P.NOTIFIED: [DEALER]}
    assert ("По запросу «Петрович — офис» (в базе — Petrovitch Office) установлен статус: <b>уведомление</b>."
            in pl.dealer_text)
    assert "есть зарегистрированный партнёр" in pl.dealer_text and "recOTHER" not in pl.dealer_text
    assert pl.registration_updates[R.REG_STATUS] == "Notification"
    assert pl.registration_updates[R.BOT_PROCESSED] == "done:Accept"


def test_notification_reports_pre_registration():
    pl = plan(reg(), {**FULL, P.PRE_REGISTERED: [OTHER1]})
    assert "есть предрегистрация другого партнёра" in pl.dealer_text


def test_pre_registration_position_and_broadcast():
    project = {**FULL, P.NOTIFIED: [OTHER2, DEALER], P.PRE_REGISTERED: [OTHER1]}
    pl = plan(reg(rtype="Upgrade — Pre-registration"), project)
    assert pl.project_updates[P.PRE_REGISTERED] == [OTHER1, DEALER]
    assert pl.project_updates[P.NOTIFIED] == [OTHER2]               # перешёл из уведомления
    assert pl.project_updates[P.STATUS_CONFIRMED] == "__today__"
    assert "Ты на 2-м месте из 2 в предрегистрации." in pl.dealer_text
    assert sorted(pl.broadcast_people) == [OTHER1, OTHER2]
    assert pl.broadcast_text.splitlines() == [
        "🔎 По объекту «Petrovitch Office» появилась предрегистрация другого партнёра.",
        "Чтобы не потерять приоритет — дополни данные по проекту или запроси предрегистрацию.",
    ]


def test_pre_registration_blocked_by_missing_fields_admin_once():
    project = {**FULL}
    del project[P.CONTACT_LEVEL]
    del project[P.DOSSIER_FILES]
    pl = plan(reg(rtype="New — Pre-registration"), project)
    assert pl.blocked and not pl.project_updates and not pl.dealer_text
    assert "нельзя поставить Pre-registered" in pl.admin_text
    assert "Contact Level; Dossier Files" in pl.admin_text
    assert pl.registration_updates[R.BOT_PROCESSED] == "blocked:Accept"
    again = plan(reg(rtype="New — Pre-registration", processed="blocked:Accept"), project)
    assert again.blocked and again.admin_text is None                 # админу — один раз


def test_registration_needs_quotation_and_single_slot():
    pl = plan(reg(rtype="Upgrade — Registration"), dict(FULL))
    assert pl.blocked and "Quotation / Proforma №" in pl.admin_text
    with_q = {**FULL, P.QUOTATION: "Q-1", P.PRE_REGISTERED: [DEALER, OTHER1]}
    pl = plan(reg(rtype="Upgrade — Registration"), with_q)
    assert pl.project_updates[P.REGISTERED] == [DEALER]
    assert pl.project_updates[P.PRE_REGISTERED] == [OTHER1]
    assert "установлен статус: <b>регистрация</b>." in pl.dealer_text
    taken = plan(reg(rtype="Upgrade — Registration"), {**with_q, P.REGISTERED: [OTHER2]})
    assert taken.blocked and "уже есть Registered" in taken.admin_text


def test_reg_status_overrides_request_type():
    pl = plan(reg(rtype="New — Pre-registration", status="Notification"), dict(FULL))
    assert P.NOTIFIED in pl.project_updates and P.PRE_REGISTERED not in pl.project_updates


def test_accept_without_target_project_blocked():
    pl = plan(reg(), None)
    assert pl.blocked and "Target Project" in pl.admin_text


def test_decline_and_need_info_need_note():
    assert plan(reg("Decline"), None).blocked
    pl = plan(reg("Decline", note="Объект уже зарегистрирован"), None)
    assert pl.registration_updates[R.REG_STATUS] == "Declined"
    assert "По запросу «Петрович — офис»: отказ." in pl.dealer_text and "Объект уже зарегистрирован" in pl.dealer_text
    pl = plan(reg("Need more info", note="Пришлите планировки"), None)
    assert "нужна дополнительная информация" in pl.dealer_text and R.REG_STATUS not in pl.registration_updates


def test_news_accept_no_status_change():
    pl = plan(reg(rtype="Update — Lost"), dict(FULL))
    assert not pl.project_updates and "информацию приняли" in pl.dealer_text


def test_position_from_actual_order_when_already_listed():
    # Егор заранее поставил дилера первым — пишем фактическое место, не «последний»
    project = {**FULL, P.PRE_REGISTERED: [DEALER, OTHER1, OTHER2]}
    pl = plan(reg(rtype="New — Pre-registration"), project)
    assert P.PRE_REGISTERED not in pl.project_updates and not pl.broadcast_people
    assert "Ты на 1-м месте из 3 в предрегистрации." in pl.dealer_text


import json


def _with_payload(r: dict, payload: dict, files=None) -> dict:
    r[R.PAYLOAD] = json.dumps(payload, ensure_ascii=False)
    if files:
        r[R.ATTACHMENTS] = files
    return r


def test_check_counts_dealer_answers_and_moves_them_into_empty_fields():
    project = {P.NAME: "JTI SPb", P.EXW_POTENTIAL: 40000, P.SPECIFIER: ["recB"], P.AW_ALTERNATIVES: "No — AW only",
               P.OBJECT_ADDRESS: "Уже в проекте", P.DOSSIER_FILES: [{"id": "attOLD", "url": "x"}]}
    r = _with_payload(reg(rtype="New — Pre-registration"), {
        "Object Address": "Из заявки", "Order Month": "2027-03-01", "Contact Level": "Declared",
        "Parallel Registrations": "Нет", "Visualizations": [{"kind": "link", "url": "https://disk/x"}],
    }, files=[{"id": "attNEW", "url": "https://airtable/new.pdf", "filename": "plan.pdf"}])
    pl = plan(r, project)
    assert not pl.blocked
    u = pl.project_updates
    assert P.OBJECT_ADDRESS not in u                       # заполненное не трогаем
    assert u[P.EXPECTED_ORDER_DATE] == "2027-03-01" and u[P.CONTACT_LEVEL] == "Declared"
    assert u[P.PARALLEL_REGISTRATIONS] == "Нет"
    assert u[P.DOSSIER_FILES] == [{"id": "attOLD"}, {"url": "https://airtable/new.pdf", "filename": "plan.pdf"}]
    assert u[P.PRE_REGISTERED] == [DEALER]
    assert "Contact Level" in pl.transferred and "Dossier Files" in pl.transferred


def test_specifier_text_and_links_count_as_provided():
    project = {P.NAME: "X", P.EXW_POTENTIAL: 40000, P.EXPECTED_ORDER_DATE: "2027-01-01", P.OBJECT_ADDRESS: "a",
               P.CONTACT_LEVEL: "Declared", P.AW_ALTERNATIVES: "No — AW only", P.PARALLEL_REGISTRATIONS: "нет"}
    r = _with_payload(reg(rtype="New — Pre-registration"),
                      {"Specifier": "UNK Project", "Layouts": [{"kind": "link", "url": "https://d"}]})
    pl = plan(r, project)
    assert not pl.blocked and P.SPECIFIER not in pl.project_updates


def test_really_missing_suggests_need_more_info():
    pl = plan(_with_payload(reg(rtype="New — Pre-registration"), {}), {P.NAME: "X"})
    assert pl.blocked and "ни в проекте, ни в заявке" in pl.admin_text and "кнопкой ниже" in pl.admin_text
    assert pl.ask_text.startswith("Чтобы дать предрегистрацию, не хватает:\n• уточнённый объём\n")
    assert pl.ask_text.endswith("«Дополнить материалы» или ответь здесь.")


def test_need_more_info_has_reply_button():
    pl = plan(reg("Need more info", note="Пришлите планировки"), None)
    assert pl.reply_button


def test_info_update_moves_fields_without_status():
    for rtype in ("Update — info only", "Update — fields", "Update — news"):
        r = _with_payload(reg(rtype=rtype), {"Contact Level": "Joint meeting with AW"})
        pl = plan(r, {P.NAME: "X"})
        assert pl.project_updates == {P.CONTACT_LEVEL: "Joint meeting with AW"} and not pl.blocked


def test_won_lost_do_not_touch_project():
    r = _with_payload(reg(rtype="Update — Won"), {"Contact Level": "Declared"})
    assert plan(r, {P.NAME: "X"}).project_updates == {}



def test_accepted_reply_is_silent_for_dealer():
    r = _with_payload(reg(rtype="Update — info only"), {"Dealer News": "Именно так", "In Reply To": "recORIG"})
    pl = plan(r, {P.NAME: "PAO Simvol"})
    assert pl.dealer_text is None and pl.registration_updates[R.BOT_PROCESSED] == "done:Accept"


def test_company_links_client_specifier_dealer():
    r = _with_payload(reg(), {"End Client": "ПАО Сбербанк", "Specifier": "Петр Иванов",
                              "Specifier Type": "Частный архитектор"})
    pl = plan(r, dict(FULL, **{P.SPECIFIER: []}))
    by_field = {l["field"]: l for l in pl.links}
    assert by_field[P.END_CLIENT] == {"field": P.END_CLIENT, "name": "ПАО Сбербанк", "type": "End Client"}
    assert by_field[P.SPECIFIER]["type"] == "Architect" and by_field[P.SPECIFIER]["name"] == "Петр Иванов"
    assert by_field[P.DEALER] == {"field": P.DEALER, "company_id": DEALER, "append": True}


def test_own_design_links_dealer_as_specifier_and_keeps_filled():
    r = _with_payload(reg(), {"End Client": "X", "Specifier": "Собственная проработка"})
    pl = plan(r, dict(FULL, **{P.SPECIFIER: [], P.END_CLIENT: ["recC"], P.DEALER: [DEALER]}))
    assert pl.links == [{"field": P.SPECIFIER, "company_id": DEALER}]


def test_quote_request_accept():
    r = _with_payload(reg(rtype="Quotation — Simplified"), {"Project Log": "Запрос квотации по упрощённой схеме"},
                      files=[{"id": "a1", "url": "https://air/spec.pdf", "filename": "spec.pdf"}])
    pl = plan(r, {P.NAME: "IkonTyres VIP"})
    assert pl.quote and not pl.blocked and pl.status_label is None
    assert "спецификацию передали на квотацию" in pl.dealer_text
    assert pl.project_updates[P.DOSSIER_FILES] == [{"url": "https://air/spec.pdf", "filename": "spec.pdf"}]
    assert pl.log_lines == ["Запрос квотации по упрощённой схеме"]


def test_ppr_track_and_log_lines_transferred():
    r = _with_payload(reg(), {"PPR Track": "Simplified (<30k)", "Project Log": "Площадь: 100 м²\nОбъём: 1 €"})
    pl = plan(r, {P.NAME: "X"})
    assert pl.project_updates[P.PPR_TRACK] == "Simplified (<30k)"
    assert pl.log_lines == ["Площадь: 100 м²", "Объём: 1 €"] and pl.status_label == "уведомление"


def test_declined_preregistration_records_notification():
    r = reg("Decline", rtype="New — Pre-registration", note="Нет документов от бюро")
    pl = plan(r, {**FULL, P.NOTIFIED: []})
    assert pl.declined_upgrade and not pl.blocked
    assert pl.project_updates[P.NOTIFIED] == [DEALER]
    assert P.PRE_REGISTERED not in pl.project_updates
    assert pl.registration_updates[R.REG_STATUS] == "Notification"
    assert pl.registration_updates[R.BOT_PROCESSED] == "done:Decline"
    assert plan(r, None).blocked                      # без проекта — сначала выбрать проект
    plain_decline = plan(reg("Decline", note="Дубль"), None)
    assert not plain_decline.declined_upgrade and plain_decline.registration_updates[R.REG_STATUS] == "Declined"


def test_volume_and_stage_overwrite_with_change_lines():
    r = _with_payload(reg(rtype="Update — fields"), {"EXW Potential": 54000, "Project Stage": "Tender",
                                                     "PPR Track": "Full"})
    pl = plan(r, {**FULL, P.PROJECT_STAGE: "Design", P.PPR_TRACK: "Simplified (<30k)"})
    assert pl.project_updates[P.EXW_POTENTIAL] == 54000 and pl.project_updates[P.PROJECT_STAGE] == "Tender"
    assert pl.project_updates[P.PPR_TRACK] == "Full"
    assert pl.changes == ["💶 Объём EXW: 45 000 € → 54 000 €", "🏗 Стадия: проектирование → тендер",
                          "📈 Трек: Simplified → Full"]
    assert "Объём EXW: 45 000 € → 54 000 €" in pl.log_lines


def test_track_never_goes_down():
    r = _with_payload(reg(rtype="Update — fields"), {"PPR Track": "Simplified (<30k)"})
    assert P.PPR_TRACK not in plan(r, {**FULL, P.PPR_TRACK: "Full"}).project_updates


def test_dossier_links_go_to_field_and_log_and_count_as_dossier():
    link = {"kind": "link", "url": "https://disk.yandex.ru/d/x"}
    r = _with_payload(reg(rtype="Upgrade — Pre-registration"), {"Visualizations": [link]})
    project = {**FULL, P.DOSSIER_FILES: [], P.DOSSIER_LINKS: "https://old"}
    pl = plan(r, project)
    assert not pl.blocked
    assert pl.project_updates[P.DOSSIER_LINKS] == "https://old\nhttps://disk.yandex.ru/d/x"
    assert "Материалы (ссылка): https://disk.yandex.ru/d/x" in pl.log_lines


def test_fourth_pre_registration_blocked():
    project = {**FULL, P.PRE_REGISTERED: [OTHER1, OTHER2, "recOTHER3"]}
    pl = plan(reg(rtype="Upgrade — Pre-registration"), project)
    assert pl.blocked and "не больше трёх" in pl.admin_text


def test_simplified_quote_number_notifies_dealer_and_records_number():
    from bot.airtable_ids import Registrations as R
    r = _with_payload(reg(rtype="Quotation — Simplified"), {"Quotation / Proforma №": "QT-1", "PPR Track": "Simplified (<30k)"})
    pl = plan(r, {P.NAME: "X"})
    assert pl.project_updates[P.NOTIFIED] == [DEALER] and pl.project_updates[P.QUOTATION] == "QT-1"
    assert pl.registration_updates[R.QUOTE_NO] == "QT-1" and pl.registration_updates[R.REG_STATUS] == "Notification"
    assert "квотацию № QT-1 зафиксировали" in pl.dealer_text


def test_status_goes_to_manager_not_company():
    r = reg()
    r[R.SUBMITTED_BY] = ["recMARINA"]
    pl = plan(r, {P.NAME: "X"})
    assert pl.project_updates[P.NOTIFIED] == ["recMARINA"]        # Notified (Manager)
    assert P.NOTIFIED == "fldJRkBhuJzyg7QIX"
    pre = plan(dict(r, **{R.REQUEST_TYPE: "Upgrade — Pre-registration"}), {**FULL, P.NOTIFIED: ["recMARINA"]})
    assert pre.project_updates[P.PRE_REGISTERED] == ["recMARINA"] and pre.project_updates[P.NOTIFIED] == []


def test_order_month_final_overwrites_expected_order_date():
    from bot.decisions import change_lines, transfers
    reg = {R.PAYLOAD: '{"Order Month Final": "2027-06-01"}'}
    project = {P.NAME: "X", P.EXPECTED_ORDER_DATE: "2027-03-01"}
    u, _ = transfers(reg, project)
    assert u[P.EXPECTED_ORDER_DATE] == "2027-06-01"
    assert "📅 Размещение заказа: 03.2027 → 06.2027" in change_lines(project, u)
