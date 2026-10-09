from bot.airtable_ids import Projects as P, Registrations as R
from bot.decisions import plan

DEALER, OTHER1, OTHER2 = "recDEALER", "recOTHER1", "recOTHER2"
FULL = {P.NAME: "Petrovitch Office", P.EXW_POTENTIAL: 45000, P.EXPECTED_ORDER_DATE: "2027-03-01",
        P.OBJECT_ADDRESS: "Lesnaya 9", P.SPECIFIER: ["recB"], P.CONTACT_LEVEL: "Declared",
        P.AW_ALTERNATIVES: "No — AW only", P.PARALLEL_REGISTRATIONS: "нет", P.DOSSIER_FILES: [{"id": "a"}]}


def reg(decision="Accept", rtype="New — Notification", note=None, status="PENDING", processed=None):
    r = {R.DECISION: decision, R.REQUEST_TYPE: rtype, R.REG_STATUS: status, R.DEALER: [DEALER],
         R.DEALER_PROJECT_NAME: "Петрович — офис", R.TARGET_PROJECT: ["recP"]}
    if note:
        r[R.DECISION_NOTE] = note
    if processed:
        r[R.BOT_PROCESSED] = processed
    return r


def test_notification_reports_registered_without_names():
    pl = plan(reg(), {**FULL, P.REGISTERED: [OTHER1], P.PRE_REGISTERED: [OTHER2]})
    assert pl.project_updates == {P.NOTIFIED: [DEALER]}
    assert "Уведомление по объекту «Petrovitch Office» принято." in pl.dealer_text
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
    assert "Вы 2-й из 2 в предрегистрации." in pl.dealer_text
    assert sorted(pl.broadcast_companies) == [OTHER1, OTHER2]
    assert pl.broadcast_text == "По объекту «Petrovitch Office» появилась предрегистрация другого партнёра."


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
    assert "Регистрация по объекту «Petrovitch Office» подтверждена." in pl.dealer_text
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
    assert "решение отрицательное" in pl.dealer_text and "Объект уже зарегистрирован" in pl.dealer_text
    pl = plan(reg("Need more info", note="Пришлите планировки"), None)
    assert "нужна дополнительная информация" in pl.dealer_text and R.REG_STATUS not in pl.registration_updates


def test_news_accept_no_status_change():
    pl = plan(reg(rtype="Update — Lost"), dict(FULL))
    assert not pl.project_updates and "информацию приняли" in pl.dealer_text
