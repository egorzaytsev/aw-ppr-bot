"""Проверяющему — сами файлы дилера перед карточкой, в карточке — понятные имена."""
import asyncio

from bot import admin
from bot.scenarios.base import describe_items, file_label


class Bot:
    def __init__(self):
        self.sent = []

    async def send_photo(self, chat_id, photo, caption=""):
        self.sent.append(("photo", photo, caption))

    async def send_document(self, chat_id, document, caption=""):
        self.sent.append(("doc", document, caption))


def test_files_forwarded_with_question_title():
    payload = {"Contact Level": "Verifiable (email/contact)",
               "Contact Evidence": [{"kind": "file", "file_id": "F1", "name": "photo_5445391960484028718_y.jpg",
                                     "mime": "image/jpeg"},
                                    {"kind": "link", "url": "https://example.com"}],
               "Extra Materials": [{"kind": "file", "file_id": "F2", "name": "Ведомость.pdf",
                                    "mime": "application/pdf"}]}
    bot = Bot()
    line = asyncio.run(admin.send_files(bot, 1, payload, "SHANSGK"))
    assert bot.sent == [("photo", "F1", "📎 Подтверждение контакта · SHANSGK"),
                        ("doc", "F2", "📎 Материалы · SHANSGK")]
    assert "Файлы дилера (2) — сообщениями выше" in line


def test_no_files_no_line():
    assert asyncio.run(admin.send_files(Bot(), 1, {"Dealer News": "x"})) == ""


def test_file_labels():
    assert file_label("photo_5445391960484028718_y.jpg") == "фото"
    assert file_label("file_AgADx") == "файл"
    assert file_label("Планировка.pdf") == "Планировка.pdf"
    assert describe_items([{"kind": "file", "name": "photo_1_y.jpg"}, {"kind": "link", "url": "u"}]) == "фото; u"


def test_image_sent_as_file_falls_back_to_document():
    class PickyBot(Bot):
        async def send_photo(self, chat_id, photo, caption=""):
            raise RuntimeError("Can't use file of type Document as Photo")
    payload = {"Contact Evidence": [{"kind": "file", "file_id": "F1", "name": "photo_544_y.jpg", "mime": "image/jpeg"}]}
    bot = PickyBot()
    line = asyncio.run(admin.send_files(bot, 1, payload, "SHANSGK"))
    assert bot.sent == [("doc", "F1", "📎 Подтверждение контакта · SHANSGK")]
    assert "Файлы дилера (1)" in line and "не получилось" not in line
