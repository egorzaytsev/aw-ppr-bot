"""Описание файлов дилера: только картинки/PDF, кэш, без ключа — выключено."""
import asyncio

from bot import file_notes


class TgFile:
    async def download_as_bytearray(self):
        return bytearray(b"%PDF-1.4 test")


class Bot:
    def __init__(self):
        self.sent = []

    async def get_file(self, file_id):
        return TgFile()

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)


PAYLOAD = {"Object Name": "Бизнес центр I-City",
           "Contact Evidence": [{"kind": "file", "file_id": "F1", "file_unique_id": "U1", "name": "photo_1_y.jpg",
                                 "mime": "image/jpeg", "size": 1000}],
           "Materials": [{"kind": "file", "file_id": "F2", "file_unique_id": "U2", "name": "КП.pdf",
                          "mime": "application/pdf", "size": 2000},
                         {"kind": "file", "file_id": "F3", "file_unique_id": "U3", "name": "смета.xlsx",
                          "mime": "application/vnd.ms-excel", "size": 10}]}


def test_off_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    bot = Bot()
    asyncio.run(file_notes.send_notes({}, bot, 1, PAYLOAD, "SHANSGK"))
    assert bot.sent == []


def test_describes_images_and_pdf_once(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    calls = []

    async def fake(data, mime, context):
        calls.append((mime, context))
        return f"Описание {mime}"
    monkeypatch.setattr(file_notes, "describe_one", fake)
    bot_data = {"memo": {}}
    bot = Bot()
    asyncio.run(file_notes.send_notes(bot_data, bot, 1, PAYLOAD, "SHANSGK"))
    assert sorted(m for m, _ in calls) == ["application/pdf", "image/jpeg"]   # xlsx не описываем
    assert "Бизнес центр I-City" in calls[0][1]
    text = bot.sent[0]
    assert "Что в файлах дилера" in text and "Подтверждение контакта" in text and "(фото)" in text
    # Повтор (/inbox) — из кэша, без новых вызовов
    asyncio.run(file_notes.send_notes(bot_data, bot, 1, PAYLOAD, "SHANSGK"))
    assert len(calls) == 2 and len(bot.sent) == 2
