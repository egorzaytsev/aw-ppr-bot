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
    assert f[B.SOURCE] == "Telegram" and f[B.STATUS] == "Новое" and f[B.KEY].startswith("tg:1:1761650000:")
    assert f[B.WHAT].startswith("Telegram · Александр Стороженко (@storozh): По Райфу")
    assert "переслано от: Марина" in f[B.SOURCE_REF] and "через Telegram Business" in f[B.SOURCE_REF]
    edited = message_record("edited_message", dict(msg, edit_date=1761650100))
    assert edited[B.KEY].endswith(":e1761650100")


def test_poll_writes_once_and_confirms_offset():
    added = []
    repo = SimpleNamespace(buffer_has=lambda key: key in [a[B.KEY] for a in added],
                           buffer_add=lambda f: added.append(f) or "recB", buffer_attach=lambda *a: None,
                           outbox_drafts=lambda: [])
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


def _repo(added, attached=None, updated=None):
    return SimpleNamespace(
        buffer_has=lambda key: key in [a[B.KEY] for a in added],
        buffer_add=lambda f: added.append(f) or f"rec{len(added)}",
        buffer_attach=lambda rid, name, content, mime=None: (attached if attached is not None else []).append(
            (rid, name, len(content))),
        buffer_update=lambda rid, f: (updated if updated is not None else []).append((rid, f)),
        outbox_drafts=lambda: [])


class _Tr:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0

    async def transcribe(self, audio, filename, language=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("400 Bad Request")
        return "Встреча со STATIO по Райффайзену"


def _collector(repo, tr=None, content=b"x" * 1000):
    c = TelegramCollector("T", repo, tr)

    async def fake_download(file_id):
        return content, "file_1.oga"
    c._download = fake_download

    async def no_sleep(*a):
        return None
    c._pause = no_sleep
    return c


def test_owner_voice_becomes_audio_record_with_file():
    added, attached = [], []
    tr = _Tr()
    c = _collector(_repo(added, attached), tr)
    msg = {"message_id": 5, "date": 1761650000, "chat": {"id": 6412409625, "type": "private"},
           "from": {"id": 6412409625, "first_name": "Egor"}, "caption": "после встречи",
           "voice": {"file_id": "F1", "file_unique_id": "U1", "file_size": 1000, "duration": 10}}
    asyncio.run(c._handle("message", msg))
    f = added[0]
    assert f[B.SOURCE] == "Аудио" and f[B.WHAT].startswith("Аудио · ")
    assert f[B.ESSENCE].startswith("после встречи") and "🎙 Расшифровка: Встреча со STATIO" in f[B.ESSENCE]
    assert "file_id: F1" in f[B.SOURCE_REF] and "file_unique_id: U1" in f[B.SOURCE_REF]
    assert attached and attached[0][2] == 1000


def test_big_file_not_downloaded_with_explicit_note():
    added = []
    c = _collector(_repo(added), _Tr())
    msg = {"message_id": 9, "date": 1761650000, "chat": {"id": 42, "type": "private"}, "from": {"id": 42},
           "video": {"file_id": "V", "file_unique_id": "UV", "file_size": 30 * 1024 * 1024, "duration": 90},
           "business_connection_id": None}
    asyncio.run(c._handle("business_message", msg))
    assert "⚠ Файл > 20 МБ" in added[0][B.ESSENCE] and "чат 42, сообщение 9" in added[0][B.ESSENCE]


def test_failed_transcription_says_why():
    added = []
    tr = _Tr(fail=True)
    c = _collector(_repo(added), tr)
    msg = {"message_id": 3, "date": 1761650000, "chat": {"id": 42, "type": "private"}, "from": {"id": 42},
           "voice": {"file_id": "F", "file_unique_id": "U", "file_size": 500, "duration": 7}}
    asyncio.run(c._handle("business_message", msg))
    e = added[0][B.ESSENCE]
    assert tr.calls == 3 and "🎙 Расшифровка не удалась: RuntimeError: 400 Bad Request; длительность 7 с; файл: во вложениях" in e


def test_document_name_size_and_file_id():
    added, attached = [], []
    c = _collector(_repo(added, attached), None, content=b"y" * 2048)
    msg = {"message_id": 4, "date": 1761650000, "chat": {"id": 42, "type": "private"}, "from": {"id": 42},
           "document": {"file_id": "D", "file_unique_id": "UD", "file_size": 2048, "file_name": "spec.xlsx",
                        "mime_type": "application/vnd.ms-excel"}}
    asyncio.run(c._handle("business_message", msg))
    assert "📎 spec.xlsx · 0,0 МБ" in added[0][B.ESSENCE] and "file_id: D" in added[0][B.SOURCE_REF]
    assert attached[0][1] == "spec.xlsx"


def test_secrets_are_not_saved():
    from bot.tg_collector import is_secret
    assert is_secret({"text": "Код подтверждения: 482913"})
    assert is_secret({"text": "карта 4276 1234 5678 9012"})
    assert is_secret({"from": {"id": 777000}, "text": "Login code"})
    assert not is_secret({"text": "Квотация 20101624 ушла клиенту"})


def test_same_message_from_two_connections_is_one_record():
    from bot.tg_collector import content_key
    a = {"message_id": 10, "date": 1761650000, "chat": {"id": 7753161469}, "from": {"id": 6412409625}, "text": "Привет"}
    b = dict(a, message_id=99, chat={"id": 6412409625})
    assert content_key("business_message", a) == content_key("business_message", b)


def test_outbox_draft_preview_send_and_status():
    from bot.tg_collector import CB_SEND, OUT_SENT, OUT_WAITING
    rec = {"id": "recO", "fields": {B.SOURCE: "Исходящее", B.STATUS: "Черновик", B.WHAT: "Исходящее · Лоле",
                                    B.ESSENCE: "Лола, привет!", B.SOURCE_REF: "Кому: 7753161469\nОт кого: Бот"}}
    updates = []
    repo = SimpleNamespace(outbox_drafts=lambda: [rec], buffer_update=lambda rid, f: updates.append(f),
                           buffer_get=lambda rid: {"fields": dict(rec["fields"], **{B.STATUS: OUT_WAITING})})
    c = TelegramCollector("T", repo, None)
    calls = []

    async def fake_call(method, **params):
        calls.append((method, params))
        return {"message_id": 77}
    c._call = fake_call
    asyncio.run(c.check_outbox())
    assert calls[0][1]["chat_id"] == 6412409625 and "Лола, привет!" in calls[0][1]["text"]
    assert updates[-1] == {B.STATUS: OUT_WAITING}
    assert not any(m == "sendMessage" and p.get("chat_id") == "7753161469" for m, p in calls)  # без кнопки — нет
    asyncio.run(c._on_callback({"id": "q", "from": {"id": 42}, "data": CB_SEND + "recO"}))   # чужой — нет
    assert not any(p.get("chat_id") == "7753161469" for _, p in calls)
    asyncio.run(c._on_callback({"id": "q", "from": {"id": 6412409625}, "data": CB_SEND + "recO"}))
    assert any(p.get("chat_id") == "7753161469" and p["text"] == "Лола, привет!" for _, p in calls)
    assert updates[-1][B.STATUS] == OUT_SENT and "message_id 77" in updates[-1][B.ANALYSIS]


def test_split_audio_into_chunks():
    import subprocess
    import tempfile
    from pathlib import Path
    import imageio_ffmpeg
    from bot.audio_split import split_audio
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "a.m4a"
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=1300", "-c:a", "aac", str(src)], check=True)
        parts = asyncio.run(split_audio(src.read_bytes(), ".m4a"))
    assert len(parts) == 3 and all(len(b) > 0 for _, b in parts)


def test_records_inbox_transcribes_moves_and_writes_buffer():
    from bot.records_inbox import PROCESSED, RecordsInbox
    added, moves, uploads = [], [], []

    class Dbx:
        async def list_files(self, path):
            return [{"name": "Запись 120.m4a", "path_display": "/ecoCRM/records/inbox/Запись 120.m4a",
                     "client_modified": "2026-10-30T09:00:00Z"}]

        async def download(self, path):
            return b"audio"

        async def move(self, src, dst):
            moves.append((src, dst))
            return dst

        async def upload(self, path, content):
            uploads.append((path, content))
            return path

    inbox = RecordsInbox(Dbx(), _Tr(), _repo(added))

    async def fake_transcribe(content, name):
        return "Встреча с Office Solutions по Алматы"
    inbox._transcribe = fake_transcribe
    asyncio.run(inbox.check_once())
    assert moves == [("/ecoCRM/records/inbox/Запись 120.m4a", f"{PROCESSED}/Запись 120.m4a")]
    assert uploads[0][0] == f"{PROCESSED}/Запись 120.txt"
    f = added[0]
    assert f[B.SOURCE] == "Аудио" and f[B.WHAT].startswith("Аудио · 30.10 10:00 · Встреча с Office")
    assert f[B.ESSENCE] == "Встреча с Office Solutions по Алматы"
