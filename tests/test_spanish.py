import asyncio
import json
from datetime import datetime

import httpx

from bot.slots import VALENCIA
from bot.spanish import SpanishCards, card_text, import_queue, next_send, parse_cards


def test_parse_cards_separators_and_skips():
    text = "# 12.10\nel perro — собака\nla casa\tдом\ntener ganas de  хотеть\n1. Conjuga el verbo\nhablar\n"
    cards, skipped = parse_cards(text)
    assert cards == [{"es": "el perro", "ru": "собака"}, {"es": "la casa", "ru": "дом"},
                     {"es": "tener ganas de", "ru": "хотеть"}]
    assert skipped == ["1. Conjuga el verbo", "hablar"]


def test_parse_keeps_hyphenated_words():
    cards, _ = parse_cards("medio-día - полдень")
    assert cards == [{"es": "medio-día", "ru": "полдень"}]


def test_import_queue_formats():
    cards, sent = import_queue({"cards": [{"es": "a", "ru": "б"}, ["c", "д"]], "sent_count": 1,
                                "processed_sections": ["01.10"]})
    assert cards == [{"es": "a", "ru": "б"}, {"es": "c", "ru": "д"}] and sent == 1
    cards, sent = import_queue([{"front": "x", "back": "у"}])
    assert cards == [{"es": "x", "ru": "у"}] and sent == 0


def test_card_text_escapes_and_spoilers():
    out = card_text([{"es": "a<b", "ru": "в"}], 6, 20)
    assert "Карточки 7–7" in out and "a&lt;b" in out and "<tg-spoiler>в</tg-spoiler>" in out


def test_next_send():
    before = datetime(2026, 11, 2, 9, 0, tzinfo=VALENCIA)
    after = datetime(2026, 11, 2, 11, 0, tzinfo=VALENCIA)
    assert next_send(before).day == 2 and next_send(after).day == 3


class FakeDbx:
    def __init__(self):
        self.files = {}

    async def download(self, path):
        if path not in self.files:
            raise httpx.HTTPStatusError("nf", request=httpx.Request("POST", "http://x"),
                                        response=httpx.Response(409))
        return self.files[path]

    async def upload_overwrite(self, path, content):
        self.files[path] = content


def _bot():
    bot = SpanishCards("T", FakeDbx(), owner_id=1)
    bot.sent = []

    async def call(method, **params):
        bot.sent.append(params)
        return {}
    bot._call = call
    return bot


def test_add_and_send_portion_persists():
    bot = _bot()
    msg = {"from": {"id": 1}, "chat": {"type": "private"},
           "text": "\n".join(f"palabra{i} — слово{i}" for i in range(8))}
    asyncio.run(bot._on_message(msg))
    assert "Добавил 8" in bot.sent[-1]["text"]
    assert asyncio.run(bot.send_portion("2026-11-02")) == 6
    state = json.loads(bot._dbx.files["/Spanish/cards.json"])
    assert state["sent"] == 6 and state["last_day"] == "2026-11-02"
    assert asyncio.run(bot.send_portion()) == 2


def test_strangers_ignored():
    bot = _bot()
    asyncio.run(bot._on_message({"from": {"id": 2}, "chat": {"type": "private"}, "text": "/left"}))
    assert bot.sent == []
