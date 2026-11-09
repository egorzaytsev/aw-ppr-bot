"""Подключение нового человека к боту (дилеры и менеджеры партнёров).

Человека нет в People:
1. Бот задаёт короткую анкету: имя и фамилия, компания, должность, страна, город, телефон, email.
   Ход анкеты — в памяти бота (memo «join»), переживает перезапуск.
2. После «Отправить» — запись «Unknown user» в Registrations (с ответами) и карточка Егору с кнопками:
   «✅ Подключить к <компания>» (похожие компании из базы), «🔗 Это <человек из People без Telegram>»,
   «🏢 Другая компания» (Егор пишет название), «❌ Отклонить» (с причиной или без).
3. По кнопке Егора бот сам создаёт человека в People (или привязывает Telegram к найденному), пишет
   человеку «можно начинать», Лоле — короткое «подключился», если это её дилер.
Запись в People — только по кнопке Егора (его одобрение).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from . import dedup, memo, routing
from .airtable_ids import People, Registrations

log = logging.getLogger(__name__)

CB_USER = "jn:"    # кнопки анкеты у незнакомого человека
CB_ADMIN = "jd:"   # кнопки карточки заявки у Егора: jd:<действие>:<заявка>[:<id>]
ACTION_REASON = "jrej"    # ждём причину отказа
ACTION_COMPANY = "jco"    # ждём название компании
ADMIN_ACTIONS = {ACTION_REASON, ACTION_COMPANY}
_MEMO = "join"            # {chat_id: {"step": i, "a": {...}}}
_NEW_COMPANY = "join_company"   # {заявка: название компании, которую Егор написал}

COUNTRIES = (("Russia", "Россия"), ("Uzbekistan", "Узбекистан"), ("Kazakhstan", "Казахстан"),
             ("Armenia", "Армения"), ("Georgia", "Грузия"), ("Azerbaijan", "Азербайджан"),
             ("Poland", "Польша"), ("Spain", "Испания"), ("Italy", "Италия"))
COUNTRY_RU = dict(COUNTRIES)
# Город из ответа → значение списка People.City (другие значения в People не пишем — список не меняем)
CITIES = {
    "Moscow": ("москва", "moscow", "moskva"),
    "Tashkent": ("ташкент", "tashkent", "toshkent"),
    "Krasnoyarsk": ("красноярск", "krasnoyarsk"),
    "Valencia": ("валенсия", "valencia"),
    "Yerevan": ("ереван", "yerevan"),
    "Ekaterinburg": ("екатеринбург", "ekaterinburg", "yekaterinburg"),
    "Tbilisi": ("тбилиси", "tbilisi"),
    "Saint-Petersburg": ("санкт-петербург", "петербург", "спб", "питер", "saint-petersburg",
                         "saint petersburg", "st. petersburg", "st petersburg"),
    "Almaty": ("алматы", "алма-ата", "almaty"),
    "Baku": ("баку", "baku"),
}

INTRO = ("Ола! Я Пепе, бот Andreu World для регистрации плейсмента мебели в проектах. "
         "Тебя пока нет в списке партнёров. Ответь на 7 коротких вопросов — передам Егору, "
         "он подтвердит подключение, и я сам напишу сюда.")
SENT_TEXT = "Готово, передал Егору. Как только он подтвердит — напишу сюда сам."
WAITING_TEXT = "Твоя заявка у Егора. Как только он подтвердит подключение — напишу сюда сам."
CANCEL_TEXT = "Хорошо, ничего не отправил. Захочешь подключиться — напиши мне любое сообщение."
DECLINED_TEXT = "Егор пока не подтвердил подключение к боту."

# (ключ, вопрос, подсказка при ошибке)
QUESTIONS = (
    ("name", "Как тебя зовут? Напиши имя и фамилию.", "Напиши имя и фамилию — два слова."),
    ("company", "В какой компании ты работаешь? Напиши название.", "Напиши название компании."),
    ("role", "Твоя должность?", "Напиши должность."),
    ("country", "Из какой ты страны? Выбери кнопкой или напиши.", "Выбери страну кнопкой или напиши."),
    ("city", "Город?", "Напиши город."),
    ("phone", "Телефон для связи — с кодом страны, например +998 90 123 45 67.",
     "Не похоже на номер телефона. Напиши с кодом страны, например +7 912 345 67 89."),
    ("email", "Email?", "Не похоже на email. Напиши адрес вида name@company.com."),
)
TITLES = {"name": "Имя", "company": "Компания", "role": "Должность", "country": "Страна", "city": "Город",
          "phone": "Телефон", "email": "Email"}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _repo(ctx):
    return ctx.bot_data["repo"]


def _cancel_btn() -> InlineKeyboardButton:
    return InlineKeyboardButton("Отмена", callback_data=f"{CB_USER}x")


def city_option(city: str) -> str | None:
    key = (city or "").strip().lower().replace("ё", "е")
    return next((opt for opt, names in CITIES.items() if key in names), None)


def country_option(country: str) -> str | None:
    key = (country or "").strip().lower()
    return next((en for en, ru in COUNTRIES if key in (en.lower(), ru.lower())), None)


def valid(key: str, value: str) -> bool:
    value = value.strip()
    if key == "name":
        return len(value.split()) >= 2
    if key == "phone":
        return len(re.sub(r"\D", "", value)) >= 7
    if key == "email":
        return bool(_EMAIL.match(value))
    return bool(value)


def _question(step: int, tg_name: str = "") -> tuple[str, InlineKeyboardMarkup]:
    key, text, _ = QUESTIONS[step]
    rows = []
    if key == "name" and len(tg_name.split()) >= 2:
        rows.append([InlineKeyboardButton(f"Да, {tg_name}"[:60], callback_data=f"{CB_USER}tg")])
    if key == "country":
        btns = [InlineKeyboardButton(ru, callback_data=f"{CB_USER}c:{en}") for en, ru in COUNTRIES]
        rows += [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([_cancel_btn()])
    return f"{step + 1}/{len(QUESTIONS)}. {text}", InlineKeyboardMarkup(rows)


def answers_lines(a: dict) -> list[str]:
    out = []
    for key, _, _ in QUESTIONS:
        value = a.get(key) or ""
        if key == "country":
            value = COUNTRY_RU.get(value, value)
        out.append(f"{TITLES[key]}: {escape(value) or '—'}")
    return out


async def _state(ctx, chat_id) -> dict | None:
    return (await memo.get(ctx.bot_data, _MEMO, {})).get(str(chat_id))


async def _ask(chat, ctx, st: dict) -> None:
    await memo.set_item(ctx.bot_data, _MEMO, str(chat.id), st)
    if st["step"] >= len(QUESTIONS):
        rows = [[InlineKeyboardButton("✅ Отправить Егору", callback_data=f"{CB_USER}ok")],
                [InlineKeyboardButton("✏️ Заполнить заново", callback_data=f"{CB_USER}redo"), _cancel_btn()]]
        await chat.send_message("Проверь, всё верно:\n" + "\n".join(answers_lines(st["a"])),
                                parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        return
    text, markup = _question(st["step"], st.get("tg_name", ""))
    await chat.send_message(text, reply_markup=markup)


async def _answer(chat, ctx, st: dict, value: str) -> None:
    key, _, hint = QUESTIONS[st["step"]]
    value = value.strip()
    if key == "country":
        value = country_option(value) or value
    if not valid(key, value):
        _, markup = _question(st["step"], st.get("tg_name", ""))
        await chat.send_message(hint, reply_markup=markup)
        return
    st["a"][key] = value
    st["step"] += 1
    await _ask(chat, ctx, st)


async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Любое сообщение или кнопка от человека, которого нет в People."""
    from .main import START_KEYBOARD
    chat = update.effective_chat
    user = update.effective_user
    repo = _repo(ctx)
    query = update.callback_query
    data = (query.data or "") if query else ""
    msg = update.effective_message if not query else None
    text = (msg.text or "").strip() if msg is not None and msg.text else None
    st = await _state(ctx, chat.id)

    if st is None:
        open_recs = await asyncio.to_thread(repo.open_unknown_users, chat.id)
        if any(_payload(r["fields"]).get("company") for r in open_recs):
            await chat.send_message(WAITING_TEXT, reply_markup=START_KEYBOARD)
            return
        st = {"step": 0, "a": {}, "tg_name": (user.full_name if user else "") or "",
              "reg": open_recs[0]["id"] if open_recs else None}   # старая запись без анкеты — дописываем её
        await chat.send_message(INTRO, reply_markup=START_KEYBOARD)
        await _ask(chat, ctx, st)
        return

    if data.startswith(CB_USER):
        cmd = data[len(CB_USER):]
        if cmd == "x":
            await memo.pop(ctx.bot_data, _MEMO, str(chat.id))
            await chat.send_message(CANCEL_TEXT, reply_markup=START_KEYBOARD)
        elif cmd == "redo":
            st.update(step=0, a={})
            await _ask(chat, ctx, st)
        elif cmd == "ok" and st["step"] >= len(QUESTIONS):
            await _submit(update, ctx, st)
        elif cmd == "tg" and st["step"] < len(QUESTIONS) and QUESTIONS[st["step"]][0] == "name":
            await _answer(chat, ctx, st, st.get("tg_name", ""))
        elif cmd.startswith("c:") and st["step"] < len(QUESTIONS) and QUESTIONS[st["step"]][0] == "country":
            await _answer(chat, ctx, st, cmd[2:])
        else:
            await _ask(chat, ctx, st)
        return

    if text and st["step"] < len(QUESTIONS) and not text.startswith("/") and text != "Старт":
        await _answer(chat, ctx, st, text)
        return
    if text is None and msg is not None:
        await chat.send_message("Ответь, пожалуйста, текстом.")
    await _ask(chat, ctx, st)


def _payload(fields: dict) -> dict:
    try:
        return json.loads(fields.get(Registrations.PAYLOAD) or "{}")
    except ValueError:
        return {}


async def _submit(update: Update, ctx: ContextTypes.DEFAULT_TYPE, st: dict) -> None:
    from .main import START_KEYBOARD
    chat = update.effective_chat
    user = update.effective_user
    repo = _repo(ctx)
    username = user.username if user else None
    reg_id = await asyncio.to_thread(repo.save_join_request, chat.id, username, st.get("tg_name", ""),
                                     st["a"], st.get("reg"))
    await memo.pop(ctx.bot_data, _MEMO, str(chat.id))
    await chat.send_message(SENT_TEXT, reply_markup=START_KEYBOARD)
    admin_chat = ctx.bot_data.get("admin_chat_id")
    if admin_chat:
        await send_card(ctx.bot, repo, admin_chat, reg_id)


# ---------- карточка Егору ----------

def _company_matches(repo, name: str) -> list[tuple[str, str]]:
    names = repo._company_names()
    exact = dedup.match_company(name, names)
    out = [(exact, names[exact])] if exact else []
    out += [c for c in repo.company_candidates(name) if c[0] != exact]
    return out[:3]


def _people_matches(repo, name: str) -> list[tuple[str, str]]:
    people = repo.people_without_chat()
    hits = []
    for pid, (pname, companies) in people.items():
        if pname and (dedup.match_company(name, {pid: pname}) or dedup.overlap(name, pname)):
            comp = ", ".join(repo.company_name(c) for c in companies)
            hits.append((pid, f"{pname} ({comp})" if comp else pname))
    return hits[:3]


def card_text(p: dict) -> str:
    tg = f"@{escape(p['username'])}" if p.get("username") else "без ника"
    lines = ["👋 <b>Заявка на подключение к боту</b>", *answers_lines(p), f"Telegram: {tg}"]
    if p.get("city") and not city_option(p["city"]):
        lines.append("Такого города нет в списке базы — в карточку человека не запишу, внеси руками, если нужен.")
    return "\n".join(lines)


def card_markup(reg_id: str, companies: list, people: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"✅ Подключить к «{name}»"[:60], callback_data=f"{CB_ADMIN}co:{reg_id}:{cid}")]
            for cid, name in companies]
    rows += [[InlineKeyboardButton(f"🔗 Это {label}"[:60], callback_data=f"{CB_ADMIN}pp:{reg_id}:{pid}")]
             for pid, label in people]
    rows.append([InlineKeyboardButton("🏢 Другая компания", callback_data=f"{CB_ADMIN}oc:{reg_id}"),
                 InlineKeyboardButton("❌ Отклонить", callback_data=f"{CB_ADMIN}no:{reg_id}")])
    return InlineKeyboardMarkup(rows)


async def send_card(bot, repo, chat_id, reg_id: str, fields: dict | None = None) -> None:
    fields = fields or await asyncio.to_thread(repo.registration_fields, reg_id)
    p = _payload(fields)
    companies = await asyncio.to_thread(_company_matches, repo, p.get("company", "")) if p.get("company") else []
    people = await asyncio.to_thread(_people_matches, repo, p.get("name", "")) if p.get("name") else []
    text = card_text(p)
    if p.get("company") and not companies:
        text += "\nКомпании с похожим названием в базе нет — нажми «Другая компания» и напиши, к какой подключить."
    try:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                               reply_markup=card_markup(reg_id, companies, people))
    except Exception:
        log.exception("Не удалось отправить заявку на подключение")


async def resend_open(bot, repo, chat_id) -> int:
    """/join — снова прислать карточки по заявкам без ответа."""
    recs = [r for r in await asyncio.to_thread(repo.open_unknown_users) if _payload(r["fields"]).get("company")]
    for r in recs:
        await send_card(bot, repo, chat_id, r["id"])
    return len(recs)


# ---------- решения Егора ----------

def _person_fields(p: dict, company_id: str | None, company_type: str | None) -> dict:
    fields = {People.NAME: p.get("name", "").strip(), People.ROLE: p.get("role", ""),
              People.EMAIL: p.get("email", "").strip(), People.PHONE: p.get("phone", "").strip(),
              People.TELEGRAM_CHAT_ID: str(p["chat_id"])}
    if company_id:
        fields[People.COMPANY] = [company_id]
    if p.get("username"):
        fields[People.TELEGRAM_USERNAME] = p["username"]
    if p.get("country") in COUNTRY_RU:
        fields[People.COUNTRY] = p["country"]
    city = city_option(p.get("city", ""))
    if city:
        fields[People.CITY] = city
    if company_type == "Dealer":
        fields[People.CATEGORY] = ["Dealer"]
    elif company_type in ("Partner", "Dealer / Partner", "Distributor"):
        fields[People.CATEGORY] = ["Partner"]
    return {k: v for k, v in fields.items() if v not in ("", None, [])}


async def notify_joined(bot_data: dict, bot, name: str, company_ids: list, how: str, to_egor: bool = True) -> None:
    """«Подключился» — Егору и Лоле (если это её дилер)."""
    repo = bot_data["repo"]
    companies = ", ".join(repo.company_name(c) for c in company_ids) or "без компании"
    text = f"✅ {escape(name)} ({escape(companies)}) {how}"
    targets = []
    if to_egor and bot_data.get("admin_chat_id"):
        targets.append(bot_data["admin_chat_id"])
    if routing.route(repo.company_types(company_ids)) == routing.LOLA and bot_data.get("lola_chat_id"):
        targets.append(bot_data["lola_chat_id"])
    for chat_id in targets:
        try:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        except Exception:
            log.exception("Не удалось отправить «подключился»")


async def _closed(chat, fields: dict) -> bool:
    mark = fields.get(Registrations.BOT_PROCESSED) or ""
    if mark.startswith("done") or mark == "declined":
        what = "уже отклонена" if mark == "declined" else "уже выполнена — человек подключён"
        await chat.send_message(f"🛠 Эта заявка {what}.")
        return True
    return False


async def _onboard(ctx, chat, reg_id: str, p: dict, person_id: str, company_ids: list, how: str) -> None:
    from .main import ONBOARDED_TEXT, START_KEYBOARD
    repo = _repo(ctx)
    await asyncio.to_thread(repo.mark_processed, reg_id, "done:onboarded")
    try:
        await ctx.bot.send_message(p["chat_id"], ONBOARDED_TEXT, reply_markup=START_KEYBOARD)
    except Exception:
        log.exception("Не удалось написать подключённому человеку")
    await chat.send_message(f"🛠 {how} Ему написал, что можно начинать.")
    await notify_joined(ctx.bot_data, ctx.bot, p.get("name", ""), company_ids,
                        "подключён к боту.", to_egor=False)


async def connect(ctx, chat, reg_id: str, company_id: str) -> None:
    repo = _repo(ctx)
    fields = await asyncio.to_thread(repo.registration_fields, reg_id)
    if await _closed(chat, fields):
        return
    p = _payload(fields)
    existing = await asyncio.to_thread(repo.find_by_chat_id, p["chat_id"])
    if existing:
        await _onboard(ctx, chat, reg_id, p, existing.record_id, existing.company_ids,
                       f"{escape(existing.name)} уже есть в базе с этим Telegram — новую запись не создавал.")
        return
    ctype = (await asyncio.to_thread(repo.company_types, [company_id]))[0]
    pid = await asyncio.to_thread(repo.create_person, _person_fields(p, company_id, ctype))
    company = repo.company_name(company_id)
    await _onboard(ctx, chat, reg_id, p, pid, [company_id],
                   f"Подключил: {escape(p.get('name', ''))} ({escape(company)}) — внёс в базу.")


async def link(ctx, chat, reg_id: str, person_id: str) -> None:
    """Человек уже есть в People без Telegram — привязать и дописать пустые поля."""
    repo = _repo(ctx)
    fields = await asyncio.to_thread(repo.registration_fields, reg_id)
    if await _closed(chat, fields):
        return
    p = _payload(fields)
    current = await asyncio.to_thread(repo.person_fields, person_id)
    new = _person_fields(p, None, None)
    new.pop(People.NAME, None)
    update = {k: v for k, v in new.items() if not current.get(k) or k == People.TELEGRAM_CHAT_ID}
    await asyncio.to_thread(repo.update_person, person_id, update)
    name = current.get(People.NAME, "")
    await _onboard(ctx, chat, reg_id, p, person_id, current.get(People.COMPANY, []),
                   f"Привязал Telegram к {escape(name)} в базе и дописал пустые поля.")


async def decline(ctx, chat, reg_id: str, reason: str = "") -> None:
    repo = _repo(ctx)
    fields = await asyncio.to_thread(repo.registration_fields, reg_id)
    if await _closed(chat, fields):
        return
    p = _payload(fields)
    await asyncio.to_thread(repo.mark_processed, reg_id, "declined")
    text = DECLINED_TEXT + (f"\nКомментарий Егора: {reason}" if reason else "") + \
        "\nЕсли есть вопросы — напиши Егору напрямую."
    try:
        await ctx.bot.send_message(p["chat_id"], text)
    except Exception:
        log.exception("Не удалось написать об отказе в подключении")
    await chat.send_message(f"🛠 Отказ отправлен: {escape(p.get('name', ''))}.")


async def on_admin_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    from . import admin
    if not admin.is_admin(update, ctx):
        return
    chat = update.effective_chat
    action, _, rest = data[len(CB_ADMIN):].partition(":")
    reg_id, _, extra = rest.partition(":")
    if action == "co":
        await connect(ctx, chat, reg_id, extra)
    elif action == "pp":
        await link(ctx, chat, reg_id, extra)
    elif action == "nw":   # создать компанию, которую Егор написал: extra — тип D|P
        name = await memo.pop(ctx.bot_data, _NEW_COMPANY, reg_id)
        if not name:
            await chat.send_message("🛠 Название компании потерялось — нажми «Другая компания» ещё раз.")
            return
        ctype = "Dealer" if extra == "D" else "Partner"
        cid, _ = await asyncio.to_thread(_repo(ctx).find_or_create_company, name, ctype, None, True)
        await connect(ctx, chat, reg_id, cid)
    elif action == "oc":
        await admin._wait(ctx, chat.id, (ACTION_COMPANY, reg_id, ""))
        await chat.send_message("🛠 Напиши название компании — найду её в базе.",
                                reply_markup=InlineKeyboardMarkup([admin.cancel_row()]))
    elif action == "no":
        await admin._wait(ctx, chat.id, (ACTION_REASON, reg_id, ""))
        await chat.send_message(
            "🛠 Напиши причину — уйдёт человеку. Или отклони без причины.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Отклонить без причины", callback_data=f"{CB_ADMIN}nb:{reg_id}")],
                admin.cancel_row()]))
    elif action == "nb":
        await admin.drop_pending(update, ctx)
        await decline(ctx, chat, reg_id)


async def on_admin_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, item: tuple, text: str) -> bool:
    from . import admin
    chat = update.effective_chat
    action, reg_id, _ = item
    text = text.strip()
    if not text:
        await admin._wait(ctx, chat.id, item)
        await chat.send_message("🛠 Пустой текст — напиши ещё раз.",
                                reply_markup=InlineKeyboardMarkup([admin.cancel_row()]))
        return True
    if action == ACTION_REASON:
        await decline(ctx, chat, reg_id, text)
        return True
    repo = _repo(ctx)
    companies = await asyncio.to_thread(_company_matches, repo, text)
    await memo.set_item(ctx.bot_data, _NEW_COMPANY, reg_id, text)
    rows = [[InlineKeyboardButton(f"✅ Подключить к «{name}»"[:60], callback_data=f"{CB_ADMIN}co:{reg_id}:{cid}")]
            for cid, name in companies]
    rows.append([InlineKeyboardButton(f"➕ Создать «{text}» — дилер"[:60], callback_data=f"{CB_ADMIN}nw:{reg_id}:D")])
    rows.append([InlineKeyboardButton(f"➕ Создать «{text}» — партнёр"[:60], callback_data=f"{CB_ADMIN}nw:{reg_id}:P")])
    rows.append(admin.cancel_row())
    head = "🛠 Нашёл в базе:" if companies else f"🛠 Компании «{escape(text)}» в базе нет. Создать?"
    await chat.send_message(head, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
    return True
