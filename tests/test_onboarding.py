import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bot import main
from bot.airtable_ids import Registrations as R


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((str(chat_id), text))


class Repo:
    def __init__(self, recs, person=None):
        self.recs, self.person, self.marks, self.bound = recs, person, [], []

    def open_unknown_users(self, chat_id=None):
        return self.recs

    def find_by_chat_id(self, chat_id):
        return None

    def find_by_username(self, username):
        return self.person

    def bind_chat_id(self, pid, chat_id):
        self.bound.append((pid, chat_id))

    def mark_processed(self, reg_id, mark, closed=True):
        self.marks.append((reg_id, mark, closed))

    def company_name(self, cid):
        return "STATIO"


def _rec(days_ago):
    created = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    payload = json.dumps({"chat_id": 555, "username": "newbie", "name": "Нью Би"}, ensure_ascii=False, indent=2)
    return {"id": "recU", "fields": {R.PAYLOAD: payload, R.SUBMITTED_AT: created}}


def _app(repo):
    return SimpleNamespace(bot=Bot(), bot_data={"repo": repo, "admin_chat_id": "1"})


def test_unknown_user_onboarded_when_added_to_people():
    person = SimpleNamespace(record_id="recP", name="Нью Би", company_ids=["recC"], chat_id=None)
    repo = Repo([_rec(1)], person)
    app = _app(repo)
    asyncio.run(main._check_unknown_users(app))
    assert repo.bound == [("recP", 555)] and repo.marks == [("recU", "done:onboarded", True)]
    to_user = [t for c, t in app.bot.sent if c == "555"]
    to_admin = [t for c, t in app.bot.sent if c == "1"]
    assert to_user == [main.ONBOARDED_TEXT]
    assert to_admin and "@newbie теперь в боте" in to_admin[0] and "STATIO" in to_admin[0]


def test_unknown_user_waits_then_expires():
    repo = Repo([_rec(2)])
    app = _app(repo)
    asyncio.run(main._check_unknown_users(app))
    assert repo.marks == [] and app.bot.sent == []
    repo.recs = [_rec(8)]
    asyncio.run(main._check_unknown_users(app))
    assert repo.marks == [("recU", "expired", False)]
    assert "ждал 7 дней" in app.bot.sent[0][1]


def test_lola_is_reviewer_only_after_known():
    from bot.admin import is_admin
    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=2))
    ctx = SimpleNamespace(bot_data={"admin_chat_id": "1"})
    assert not is_admin(upd, ctx)
    ctx.bot_data["lola_chat_id"] = "2"
    assert is_admin(upd, ctx)


def test_awaiting_is_per_reviewer():
    import time
    from bot.airtable_repo import AirtableRepo
    repo = AirtableRepo.__new__(AirtableRepo)
    now = int(time.time())
    rows = [{"id": "recE", "fields": {R.BOT_PROCESSED: f"await:decline:-:{now - 5}:1"}},
            {"id": "recL", "fields": {R.BOT_PROCESSED: f"await:info:-:{now}:2"}},
            {"id": "recOld", "fields": {R.BOT_PROCESSED: f"await:new:-:{now - 60}"}}]
    repo._registrations = SimpleNamespace(all=lambda **kw: rows)
    assert repo.find_awaiting(1) == ("decline", "recE", "")
    assert repo.find_awaiting(2) == ("info", "recL", "")
    assert repo.find_awaiting() == ("info", "recL", "")
