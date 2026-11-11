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

    def company_types(self, ids):
        return ["Partner" for _ in ids]

    def bot_allowed(self, person_id, company_ids):
        return True


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
    assert to_admin and "Нью Би (STATIO) подключён к боту" in to_admin[0]


def test_unknown_user_waits_then_expires():
    repo = Repo([_rec(2)])
    app = _app(repo)
    asyncio.run(main._check_unknown_users(app))
    assert repo.marks == [] and app.bot.sent == []
    repo.recs = [_rec(8)]
    asyncio.run(main._check_unknown_users(app))
    assert repo.marks == [("recU", "expired", False)]
    # Р11: обещание «напишу сам» не висит — человеку честный ответ, проверяющему — итог
    assert app.bot.sent[0] == ("555", "Не получилось подключить тебя к программе. Если вопрос актуален — "
                                      "напиши Егору напрямую.")
    assert "ждал подключения 7 дней" in app.bot.sent[1][1]


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


# ---------- анкета подключения и решения Егора (bot/onboarding.py) ----------

def test_join_questionnaire_card_and_connect():
    from bot import onboarding, preview
    from bot.airtable_ids import People
    sent = asyncio.run(preview.record("unknown"))
    other = [t for c, t, _ in sent if c == str(preview.OTHER)]
    admin_msgs = [(t, m) for c, t, m in sent if c == str(preview.ADMIN)]
    assert other[0] == onboarding.INTRO
    assert any("Не похоже на номер телефона" in t for t in other)
    assert onboarding.SENT_TEXT in other and onboarding.WAITING_TEXT in other
    assert main.ONBOARDED_TEXT in other
    card, markup = next((t, m) for t, m in admin_msgs if "Заявка на подключение" in t)
    assert "Менеджер проектов" in card and "oleg@example.com" in card and "@oleg_new" in card
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "✅ Подключить к «Другая Мебель»" in labels and "🔗 Это Олег Новиков (Другая Мебель)" in labels
    assert any("Подключил: Олег Новиков (Другая Мебель)" in t for t, _ in admin_msgs)
    declined = [t for c, t, _ in sent if c == str(preview.OTHER + 1)]
    assert declined and "Работаем только с официальными дилерами" in declined[0]


def test_person_fields_only_known_options():
    from bot import onboarding
    from bot.airtable_ids import People
    p = {"chat_id": 5, "username": "x", "name": "Олег Новиков", "role": "Менеджер", "country": "Russia",
         "city": "Питер", "phone": "+7 912", "email": "a@b.co"}
    f = onboarding._person_fields(p, "recC", "Dealer")
    assert f[People.CITY] == "Saint-Petersburg" and f[People.COUNTRY] == "Russia"
    assert f[People.CATEGORY] == ["Dealer"] and f[People.TELEGRAM_CHAT_ID] == "5"
    p["city"], p["country"] = "Самарканд", "Uzbekistan"
    f = onboarding._person_fields(p, "recC", "Partner")
    assert People.CITY not in f and f[People.CATEGORY] == ["Partner"]


def test_validation():
    from bot import onboarding
    assert not onboarding.valid("name", "Олег") and onboarding.valid("name", "Олег Новиков")
    assert not onboarding.valid("phone", "12") and onboarding.valid("phone", "+998 90 123 45 67")
    assert not onboarding.valid("email", "oleg") and onboarding.valid("email", "o@b.uz")
    assert onboarding.country_option("узбекистан") == "Uzbekistan"
