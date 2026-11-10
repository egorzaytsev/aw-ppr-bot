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


def test_send_failure_text():
    from bot import main
    t = main._send_failure("<b>✅ Заявка принята</b> · SHANSGK", RuntimeError("Forbidden: bot was blocked by the user"))
    assert t.startswith("Не дошло сообщение «✅ Заявка принята · SHANSGK» — получатель заблокировал бота")


def test_unreachable_named_once_then_skipped(monkeypatch):
    import logging
    from types import SimpleNamespace
    from bot import main

    class Repo:
        def find_by_chat_id(self, chat_id):
            return SimpleNamespace(name="Andrey Kochemazov", company_ids=["recAOF"])

        def company_name(self, cid):
            return "AOF"

    class Bot:
        def __init__(self):
            self.calls = 0

        async def send_message(self, chat_id, text, **kw):
            self.calls += 1
            raise RuntimeError("Forbidden: bot can't initiate conversation with a user")

    alerts = []

    class H(logging.Handler):
        def emit(self, record):
            alerts.append(getattr(record, "alert", None))
    h = H()
    logging.getLogger("aw-ppr-bot").addHandler(h)
    monkeypatch.setattr(main, "_BOT_DATA", {"repo": Repo(), "memo": {}})
    bot = Bot()
    try:
        asyncio.run(main._send_html(bot, 81346775, "✅ Статус изменён"))
        asyncio.run(main._send_html(bot, 81346775, "✅ Ещё одно"))
    finally:
        logging.getLogger("aw-ppr-bot").removeHandler(h)
    assert bot.calls == 1
    named = [a for a in alerts if a]
    assert len(named) == 1 and named[0].startswith("Andrey Kochemazov (AOF) не получает сообщения бота — ни разу не открывал")
    asyncio.run(main.forget_unreachable(81346775))
    assert asyncio.run(main._unreachable()) == {}
