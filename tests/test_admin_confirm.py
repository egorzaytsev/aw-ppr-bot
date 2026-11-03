from bot import dedup
from bot.admin import weak_answers


K5 = {
    "End Client": "Музей Московского Кремля", "Lead Architect": "нет архиектора",
    "AW Alternatives": "No — AW only", "Competing Dealers": "пока не получили данные, кто именно",
    "Parallel Registrations": "Нет", "Decision Maker": "пока неизвестно", "Third-party Statement": "нет",
    "Quotation / Proforma №": "20101422", "Area Source": "со слов",
}


def test_weak_answers_lists_vague_and_skipped():
    weak = weak_answers(K5)
    text = "\n".join(weak)
    assert "нет архиектора" in text and "пока неизвестно" in text and "пока не получили" in text
    assert any("«нет»" in w for w in weak)                 # документы не присланы
    assert "Нет»" not in text                              # «Нет» по параллельным регистрациям — ответ
    assert not any("20101422" in w for w in weak)


def test_name_rules():
    assert dedup.name_problem("K5 Музей Московского Кремля")
    assert dedup.name_problem("A" * 81)
    assert dedup.name_problem("K5 Moscow Kremlin Museums") is None
    assert dedup.to_latin("K5 Музей Московского Кремля") == "K5 Museum Moskovskogo Kremlya"


def test_latin_name_translates_common_words():
    assert dedup.latin_name({"End Client": "PV Smart", "Object Name": "Бизнес центр Инсайт · 3 этаж"}) \
        == "PV Smart BC Insait Floor 3"
    assert dedup.to_latin("ЖК Центральный") == "Residential Tsentralnyi"   # прилагательное — транслит


import asyncio
import json
from types import SimpleNamespace

from bot import admin
from bot.airtable_ids import Registrations

PV = {"End Client": "PV Smart", "Object Name": "Бизнес центр Инсайт · 3 этаж",
      "Decision Maker": "пока никто", "Quotation / Proforma №": "пока нет"}


class Chat:
    id = 1

    def __init__(self):
        self.sent = []

    async def send_message(self, text, reply_markup=None, **kw):
        self.sent.append((text, reply_markup))


class Repo:
    def __init__(self):
        self.reg = {Registrations.REQUEST_TYPE: "New — Registration", Registrations.PAYLOAD: json.dumps(PV),
                    Registrations.DEALER_PROJECT_NAME: "PV Smart — Бизнес центр Инсайт — 3 этаж"}
        self.created = []

    def registration_fields(self, reg_id):
        return dict(self.reg)

    def update_registration(self, reg_id, fields):
        self.reg.update(fields)

    def create_project(self, name):
        self.created.append(name)
        return "recP"

    def set_decision(self, *a):
        self.decision = a

    def set_awaiting(self, *a):
        pass

    def clear_awaiting(self, *a):
        pass


def test_new_registration_accepted_stepwise_as_notification_first(monkeypatch):
    repo, chat = Repo(), Chat()
    ctx = SimpleNamespace(bot_data={"repo": repo})
    update = SimpleNamespace(effective_chat=chat)
    monkeypatch.setattr(admin, "is_admin", lambda u, c: True)

    async def no_apply(ctx):
        pass
    monkeypatch.setattr(admin, "_apply_now", no_apply)
    run = lambda data: asyncio.run(admin.on_admin_callback(update, ctx, data))

    run("ad:new:recR:-")                                   # шаг 1 — название, без переспроса
    assert "название нового проекта" in chat.sent[-1][0]
    assert "PV Smart BC Insait Floor 3" in chat.sent[-1][1].inline_keyboard[0][0].text
    run("ad:name:recR:new")
    assert repo.reg[Registrations.REQUEST_TYPE] == "New — Notification"
    assert repo.created == ["PV Smart BC Insait Floor 3"]
    assert ctx.bot_data["stepwise"]["recR"]["final"] == "reg"


def test_step_offer_lists_phase_gaps():
    payload = {"Influencers": "", "Decision Maker Company": "пока никто", "Quotation / Proforma №": "пока нет"}
    two = admin.step_offer("AW-26-0251", "PV Smart BC Insait Floor 3", "pre", "reg", payload)
    assert "Шаг 2 из 3" in two and "предрегистрацию?" in two and "пока никто" in two and "пока нет" not in two
    three = admin.step_offer("AW-26-0251", "X", "reg", "reg", payload)
    assert "Шаг 3 из 3" in three and "пока нет" in three
