"""13.11: «Принять» → «Отправить без комментария» по дополнению зависало — метка ожидания не снималась."""
import time

from bot import admin
from bot.airtable_ids import Registrations as R
from bot.airtable_repo import AirtableRepo


class Table:
    def __init__(self, recs=()):
        self.recs, self.updates = list(recs), []

    def update(self, rid, fields):
        self.updates.append((rid, fields))

    def all(self, **kw):
        return self.recs


def _repo(recs=()):
    repo = AirtableRepo.__new__(AirtableRepo)
    repo._registrations = Table(recs)
    return repo


def test_set_decision_clears_await_marker():
    repo = _repo()
    repo.set_decision("recA", "Accept — merge into Target Project", "")
    rid, fields = repo._registrations.updates[0]
    assert fields[R.BOT_PROCESSED] == ""   # иначе decisions_to_process пропускает заявку


def test_stuck_only_older_than_five_minutes():
    old = int(time.time()) - 600
    new = int(time.time()) - 30
    recs = [{"id": "recOld", "fields": {R.BOT_PROCESSED: f"await:accn:-:{old}:1", R.DECISION: "Accept"}},
            {"id": "recNew", "fields": {R.BOT_PROCESSED: f"await:accn:-:{new}:1", R.DECISION: "Accept"}}]
    assert [r["id"] for r in _repo(recs).stuck_decisions()] == ["recOld"]


def test_stuck_line_and_button():
    ts = int(time.time()) - 600
    reg = {R.DEALER_PROJECT_NAME: "Saratov Opera and Ballet Theatre",
           R.DECISION: "Accept — merge into Target Project", R.BOT_PROCESSED: f"await:accn:-:{ts}:1"}
    line = admin.stuck_line(reg)
    assert "Saratov Opera and Ballet Theatre" in line and "висит с" in line and "await" not in line
    btn = admin.stuck_markup([{"id": "recX", "fields": reg}]).inline_keyboard[0][0]
    assert btn.callback_data == "ad:fin:recX" and btn.text.startswith("▶️ Довести до конца")


def test_accept_phrase_by_type():
    assert admin.accept_phrase("Update — fields", "X") == "Принимаем дополнение по «X»"
    assert admin.accept_phrase("Update — news", "X") == "Принимаем новость по «X»"
    assert admin.accept_phrase("New — Notification", "X") == "Принимаем новый проект «X» как уведомление"
    assert admin.accept_phrase("New — Registration", "X") == "Принимаем новый проект «X» как регистрацию"
    assert admin.accept_phrase("Upgrade — Registration", "X") == "Принимаем переход на регистрацию по «X»"
    assert admin.accept_phrase("Update — Won", "X") == "Принимаем: проект «X» выигран"
    assert "№ Q1" in admin.accept_phrase("Quotation — Simplified", "X", {"Quotation / Proforma №": "Q1"})


import asyncio

import pytest

from bot import preview
from bot.airtable_ids import RequestType as T

# Типы заявок с проектом: «Принять» → «Отправить без комментария» должен довести решение до конца
_WITH_PROJECT = [T.UPDATE_FIELDS, T.UPDATE_NEWS, T.UPDATE_INFO, T.UPDATE_WON, T.UPDATE_LOST,
                 T.UPGRADE_PRE_REGISTRATION, T.QUOTE_SIMPLIFIED]


@pytest.mark.parametrize("rtype", _WITH_PROJECT)
def test_accept_without_comment_is_processed(rtype):
    async def go():
        repo, bot, ctx, app, update, dealer = preview._world()
        payload = {"Dealer Project Name": "TESTBANK Office FL3", "Dealer News": "Клиент выбрал Gala",
                   "Quotation / Proforma №": "20095822", "Lost Reason": "цена"}
        rid = repo.create_registration(dealer, rtype, payload, "TESTBANK Office FL3", preview.PROJECT_ID)
        up = update(preview.ADMIN)
        await admin.on_admin_callback(up, ctx, f"ad:accn:{rid}")
        assert repo.regs[rid][R.BOT_PROCESSED].startswith("await:")
        await admin.on_admin_callback(up, ctx, f"ad:acc:{rid}")
        mark = str(repo.regs[rid].get(R.BOT_PROCESSED) or "")
        assert not mark.startswith("await:"), mark
        assert repo.regs[rid].get(R.DECISION)
        assert mark.startswith(("done:", "blocked:")), mark
        assert not ctx.bot_data.get("admin_pending", {}).get(preview.ADMIN)
    asyncio.run(go())
