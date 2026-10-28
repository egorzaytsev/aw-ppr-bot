import asyncio
from types import SimpleNamespace

from bot.airtable_ids import Buffer as B
from bot.tg_collector import TelegramCollector, message_record


def test_message_record_forward_and_business():
    msg = {"message_id": 7, "date": 1761650000, "chat": {"id": 42, "type": "private", "first_name": "Егор"},
           "from": {"id": 1, "first_name": "Александр", "last_name": "Стороженко", "username": "storozh"},
           "text": "По Райфу: клиент хочет +2 кресла Cividina в коже AW",
           "forward_origin": {"type": "user", "sender_user": {"id": 5, "first_name": "Марина"}},
           "business_connection_id": "abc"}
    f = message_record("business_message", msg)
    assert f[B.SOURCE] == "Telegram" and f[B.STATUS] == "Новое" and f[B.KEY] == "tg:42:7"
    assert f[B.WHAT].startswith("Telegram · Александр Стороженко (@storozh): По Райфу")
    assert "переслано от: Марина" in f[B.SOURCE_REF] and "через Telegram Business" in f[B.SOURCE_REF]
    edited = message_record("edited_message", dict(msg, edit_date=1761650100))
    assert edited[B.KEY] == "tg:42:7:e1761650100"


def test_poll_writes_once_and_confirms_offset():
    added = []
    repo = SimpleNamespace(buffer_has=lambda key: key in [a[B.KEY] for a in added],
                           buffer_add=lambda f: added.append(f) or "recB", buffer_attach=lambda *a: None)
    c = TelegramCollector("T", repo, None)
    calls = []
    upd = {"update_id": 100, "message": {"message_id": 1, "date": 1761650000, "chat": {"id": 9, "title": "AOF"},
                                         "text": "Квотация ушла клиенту"}}

    async def fake_call(method, **params):
        calls.append(params.get("offset"))
        return [upd] if len(calls) < 3 else []
    c._call = fake_call
    asyncio.run(c.poll_once())
    asyncio.run(c.poll_once())          # то же сообщение пришло снова (не подтверждено) — дубля нет
    assert len(added) == 1 and calls[:2] == [None, 101]


def test_groq_filename_renames_telegram_voice():
    from bot.voice import groq_filename
    assert groq_filename("file_12.oga") == "file_12.ogg"
    assert groq_filename("note.opus") == "note.ogg"
    assert groq_filename("voice.ogg") == "voice.ogg"
    assert groq_filename("track.MP3") == "track.MP3"
    assert groq_filename("blob") == "blob.ogg"
