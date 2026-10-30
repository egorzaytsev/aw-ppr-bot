from bot.airtable_ids import Projects as P
from bot.main import LOWER_STATUS, rollback_dealer_text
from bot.airtable_repo import PPR_NOTIFIED, PPR_PRE_REGISTERED, PPR_REGISTERED


def test_lower_status_chain():
    assert LOWER_STATUS[PPR_REGISTERED] == PPR_PRE_REGISTERED
    assert LOWER_STATUS[PPR_PRE_REGISTERED] == PPR_NOTIFIED
    assert LOWER_STATUS[PPR_NOTIFIED] is None


def test_rollback_text_has_reason_and_way_back():
    project = {P.NAME: "K5 Moscow Kremlin Museums", P.PPR_REF: "AW-26-0247"}
    t = rollback_dealer_text(project, PPR_REGISTERED, PPR_PRE_REGISTERED, "нет ЛПР")
    assert "регистрация → предрегистрация" in t and "Причина: нет ЛПР" in t and "«Дополнить»" in t
    t = rollback_dealer_text(project, PPR_NOTIFIED, None, "проект закрыт")
    assert "статус снят" in t and "«Новый проект»" in t
