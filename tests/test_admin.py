import json

from bot.admin import _search_words, request_card
from bot.airtable_ids import Registrations as R


def test_request_card_and_search_words():
    reg = {R.DEALER_PROJECT_NAME: "Пао — Символ", R.REQUEST_TYPE: "New — Notification",
           R.PAYLOAD: json.dumps({"End Client": "ПАО Сбербанк", "Object Name": "Штаб-квартира",
                                  "Object Address": "ул. Лесная, 9", "PL Volume": 31000}, ensure_ascii=False)}
    card = request_card(reg, "recX")
    assert "<b>Пао — Символ</b>" in card and "PL Volume: 31000" in card and "<code>recX</code>" in card
    words = _search_words(reg)
    assert "штаб-квартира" in words and "лесная" in words and "символ" in words
    assert all(len(w) >= 4 for w in words)
