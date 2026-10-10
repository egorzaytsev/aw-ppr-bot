import json

from bot.admin import request_card
from bot.airtable_ids import Registrations as R


def test_request_card_and_search_words():
    reg = {R.DEALER_PROJECT_NAME: "Пао — Символ", R.REQUEST_TYPE: "New — Notification",
           R.PAYLOAD: json.dumps({"End Client": "ПАО Сбербанк", "Object Name": "Штаб-квартира",
                                  "Object Address": "ул. Лесная, 9", "PL Volume": 31000}, ensure_ascii=False)}
    card = request_card(reg, "recX")
    assert "🛠 <b>📥 Новая заявка · уведомление</b>" in card and "🏗 <b>Пао — Символ</b>" in card
    assert "Объём по прайсу: 31 000 €" in card
    assert "<code>recX</code>" in card


def test_reply_card_shows_answer():
    reg = {R.DEALER_PROJECT_NAME: "Пао — Символ", R.REQUEST_TYPE: "Update — info only",
           R.PAYLOAD: json.dumps({"Dealer News": "Зеленый в крапинку", "In Reply To": "recREG"}, ensure_ascii=False)}
    card = request_card(reg, "recY", "Lola Sapaeva")
    assert card.startswith("🛠 <b>💬 Ответ на твой запрос</b>")
    assert "«Зеленый в крапинку»" in card and "👤 Lola Sapaeva" in card
    card2 = request_card(reg, "recY", "Lola Sapaeva", dealer="STATIO")
    assert "👤 <b>STATIO</b> · Lola Sapaeva" in card2


def test_admin_summary_is_short_and_clear():
    from bot.decisions import Plan
    from bot.main import _admin_summary
    reg = {R.DECISION: "Accept", R.DEALER_PROJECT_NAME: "ООО «Айкон Тайерс» — VIP этаж"}
    pl = Plan(status_label="уведомление", transferred=["Object Name"], project_name="IkonTyres VIP")
    text = _admin_summary(reg, pl, "IkonTyres VIP", True, "STATIO", True,
                          ["ООО «Айкон Тайерс» (клиент, новая)", "HUBARCH (бюро)"], None)
    assert text.splitlines() == [
        "🛠 <b>✅ Принято · IkonTyres VIP</b>",
        "• STATIO: статус <b>уведомление</b>",
        "• Проект создан",
        "• Компании: ООО «Айкон Тайерс» (клиент, новая), HUBARCH (бюро)",
        "• Дилеру отправлено",
    ]
