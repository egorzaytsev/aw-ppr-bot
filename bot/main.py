"""Точка входа: python -m bot.main (long polling, Railway worker)."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from html import escape

from telegram import (
    BotCommand, BotCommandScopeChat, BotCommandScopeDefault, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, Update,
)
from telegram import LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import Conflict, NetworkError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    Defaults,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from . import admin, alerts, cards, dedup, engine, labels, memo, phrases, preview as preview_mod, reminders, routing, slots
from .airtable_ids import Decision, Partners, People, Projects, Registrations, RegStatus, RequestType
from .decisions import payload_of, plan as decision_plan
from .airtable_repo import (
    PPR_NOTIFIED, PPR_PRE_REGISTERED, PPR_REGISTERED, AirtableRepo, DealerProject, Person,
)
from .config import load_settings
from .scenarios import FILL, INFO_REPLY, LOST, NEW_PROJECT, NEWS, QUOTE, WON
from .scenarios.new_project import REVIEWER_WITH, SOLO_ARCHITECT
from .scenarios.update_project import CONTESTED, EXTRA_FILES_KEY, fill_preset
from .slots import VALENCIA
from .dropbox_store import DropboxStore
from .records_inbox import RecordsInbox
from .tg_collector import TelegramCollector
from .spanish import SpanishCards
from .voice import Transcriber

log = logging.getLogger("aw-ppr-bot")

CB_MENU_RESUME = "menu:resume"
CB_MENU_RESTART = "menu:restart"
CB_DROP_AND_GO = "rx:"  # + отложенное действие: отменить незаконченную заявку и выполнить его

MENU_NEW = "Новый проект"
MENU_UPDATE = "Обновить проект"
MENU_MY = "Мои проекты"
MENU_KEYBOARD = ReplyKeyboardMarkup(
    [[MENU_NEW], [MENU_UPDATE, MENU_MY]], resize_keyboard=True, is_persistent=True
)

GREETING_TEXT = (
    "Привет, это Пепе — бот регистрации продакт плейсмента Andreu World. "
    "Через меня ты подаёшь уведомление, предрегистрацию и регистрацию плейсмента. Выбери действие внизу."
)
NOT_FOUND_TEXT = ("Ола! Я Пепе, бот Andreu World для регистрации плейсмента. Тебя пока нет в списке "
                  "партнёров — я передал Егору. Как только он добавит, напишу сюда сам.")
NOT_FOUND_AGAIN = "Тебя пока нет в списке партнёров — Егор уже знает. Как только добавит, напишу сюда сам."
ONBOARDED_TEXT = "Ола! Егор добавил тебя в список партнёров — можно начинать. Нажми «Старт»."
UNKNOWN_EXPIRY_DAYS = 7
MENU_START = "Старт"
# После отказа — одна кнопка, чтобы повторить вход, когда Егор добавит человека
START_KEYBOARD = ReplyKeyboardMarkup([[MENU_START]], resize_keyboard=True, is_persistent=True)
# Airtable принимает загрузку вложений до 5 МБ; больше — просим ссылку
MAX_FILE_BYTES = 5 * 1024 * 1024
CB_LIST = "pl:"        # + режим:страница — список проектов
CB_PICK = "pc:"        # + режим:id проекта — выбор проекта
CB_CARD = "cd:"        # + действие:id проекта — кнопка в карточке
CB_UPDATE_MODE = "um:"  # + режим — «Обновить проект»: что делаем
CB_REMINDER = "rs:"    # + индекс слота напоминаний
CB_REPLY = "rp:"       # + id заявки — ответить на запрос доп. информации
CB_LOOSE_FILE = "lf:"  # + id проекта — файл, присланный вне анкеты, — к этому проекту
MODE_MY, MODE_FILL, MODE_NEWS, MODE_NEXT = "my", "fill", "news", "next"
PAGE_SIZE = 8
EXPIRY_WARNING_DAYS = 14
PPR_LABELS = {
    PPR_REGISTERED: "Регистрация",
    PPR_PRE_REGISTERED: "Предрегистрация",
    PPR_NOTIFIED: "Уведомление",
}
STAGE_LABELS = {
    "Architect selection": "выбор архитектора", "Concept": "концепция", "Design": "проектирование",
    "Tender": "тендер", "Procurement": "закупка",
}
CARD_ACTIONS = {"fill": FILL, "news": NEWS, "won": WON, "lost": LOST, "quote": QUOTE}
# Следующий этап по текущему статусу дилера
NEXT_FORMS = {PPR_NOTIFIED: ("предрегистрацию", "предрегистрации"), PPR_PRE_REGISTERED: ("регистрацию", "регистрации")}
NEXT_REQUEST = {PPR_NOTIFIED: RequestType.UPGRADE_PRE_REGISTRATION, PPR_PRE_REGISTERED: RequestType.UPGRADE_REGISTRATION}
NEXT_LABEL = {PPR_NOTIFIED: "предрегистрацию", PPR_PRE_REGISTERED: "регистрацию"}
SIMPLIFIED = "Simplified (<30k)"
REMINDER_SLOTS = ("08:00–11:00", "11:00–14:00", "14:00–17:00", "17:00–20:00")
WELCOME_TEXT = (
    "Привет! Я — Пепе, бот программы регистрации продакт плейсмента Andreu World. Пепе — это PP, product placement.\n\n"
    "Через меня ты регистрируешь плейсмент мебели Andreu World в своих проектах: уведомление, "
    "затем предрегистрация и регистрация. Пара минут на проект, можно голосом.\n\n"
    "• <b>Новый проект</b> — подать уведомление о плейсменте (сразу или потом — предрегистрацию и регистрацию)\n"
    "• <b>Мои проекты</b> — статусы и сроки твоих плейсментов\n"
    "• <b>Обновить проект</b> — дополнить материалы, сообщить новость, запросить предрегистрацию / регистрацию\n\n"
    "Чем раньше и полнее ты заявляешь плейсмент, тем выше приоритет при совпадении с другим партнёром."
)
REMINDER_QUESTION = (
    "Иногда я буду напоминать о проектах, где подходит срок или не хватает данных. "
    "В какое время дня тебе удобнее получать напоминания? Время — местное, {zone}."
)
# Дательный падеж имени проверяющего: «передал Егору / Лоле»
DATIVE = {routing.EGOR: "Егору", routing.LOLA: "Лоле"}


def _markup(buttons: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text, callback_data=data) for text, data in row] for row in buttons]
    )


async def _send(update: Update, screens: list[engine.Screen]) -> None:
    chat = update.effective_chat
    for s in screens:
        await chat.send_message(s.text, parse_mode=ParseMode.HTML, reply_markup=_markup(s.buttons))


def _repo(ctx: ContextTypes.DEFAULT_TYPE) -> AirtableRepo:
    return ctx.bot_data["repo"]


async def _identify(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Person | None:
    """Человек по Chat ID; при первом контакте — по username, с записью Chat ID."""
    repo = _repo(ctx)
    chat_id = update.effective_chat.id
    person = await asyncio.to_thread(repo.find_by_chat_id, chat_id)
    if person:
        return person
    username = update.effective_user.username if update.effective_user else None
    if not username:
        return None
    person = await asyncio.to_thread(repo.find_by_username, username)
    if person:
        await asyncio.to_thread(repo.bind_chat_id, person.record_id, chat_id)
        person.chat_id = str(chat_id)
        log.info("Привязал chat_id %s к %s", chat_id, person.record_id)
    return person


async def _unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Человека нет в People: запись «Unknown user» в Inbox (одна на человека), ответ ему, сигнал Егору."""
    chat_id = update.effective_chat.id
    repo = _repo(ctx)
    if await asyncio.to_thread(repo.open_unknown_users, chat_id):
        await update.effective_chat.send_message(NOT_FOUND_AGAIN, reply_markup=START_KEYBOARD)
        return
    user = update.effective_user
    username = user.username if user else None
    full_name = (user.full_name if user else "") or ""
    reg_id = await asyncio.to_thread(repo.create_unknown_user, chat_id, username, full_name)
    await update.effective_chat.send_message(NOT_FOUND_TEXT, reply_markup=START_KEYBOARD)
    admin_chat = ctx.bot_data.get("admin_chat_id")
    if admin_chat:
        who = f", @{escape(username)}" if username else ""
        await _send_html(ctx.bot, admin_chat,
                         f"👋 <b>Новый человек в боте</b>: {escape(full_name) or 'без имени'}{who}.\n"
                         "Чтобы подключить — попроси Claude внести его и назови компанию. "
                         f"Когда он появится в базе, я сам ему напишу. Жду {UNKNOWN_EXPIRY_DAYS} дней.")


async def _check_unknown_users(app: Application) -> None:
    """Раз в минуту: человек появился в People — привязать, написать ему и Егору; 7 дней — закрыть."""
    repo: AirtableRepo = app.bot_data["repo"]
    admin_chat = app.bot_data.get("admin_chat_id")
    for rec in await asyncio.to_thread(repo.open_unknown_users):
        f = rec["fields"]
        try:
            data = json.loads(f.get(Registrations.PAYLOAD) or "{}")
        except ValueError:
            data = {}
        chat_id, username = data.get("chat_id"), data.get("username")
        if not chat_id:
            continue
        label = f"@{username}" if username else (data.get("name") or "человек без имени и username")
        person = await asyncio.to_thread(repo.find_by_chat_id, chat_id)
        if not person and username:
            person = await asyncio.to_thread(repo.find_by_username, username)
        if person:
            if str(person.chat_id or "") != str(chat_id):
                await asyncio.to_thread(repo.bind_chat_id, person.record_id, chat_id)
            await asyncio.to_thread(repo.mark_processed, rec["id"], "done:onboarded")
            await _send_html(app.bot, chat_id, ONBOARDED_TEXT, START_KEYBOARD)
            if admin_chat:
                companies = ", ".join(repo.company_name(c) for c in person.company_ids) or "без компании"
                await _send_html(app.bot, admin_chat,
                                 f"✅ {escape(person.name)} ({escape(companies)}) подключён к боту — "
                                 "приглашение отправлено. Делать ничего не нужно.")
            continue
        created = f.get(Registrations.SUBMITTED_AT)
        if created and datetime.now(timezone.utc) - datetime.fromisoformat(created.replace("Z", "+00:00")) \
                > timedelta(days=UNKNOWN_EXPIRY_DAYS):
            await asyncio.to_thread(repo.mark_processed, rec["id"], "expired", closed=False)
            # Обещание «напишу сам» не должно висеть — честно сообщаем человеку
            await _send_html(app.bot, chat_id, "Не получилось подключить тебя к программе. Если вопрос актуален — "
                                               "напиши Егору напрямую.")
            if admin_chat:
                await _send_html(app.bot, admin_chat,
                                 f"⏳ {escape(label)} ждал подключения {UNKNOWN_EXPIRY_DAYS} дней, в базу так и не "
                                 "добавлен — ожидание закрыто, ему написал, что подключить не получилось. "
                                 "Напишет боту снова — придёт новое сообщение.")


async def _save(ctx: ContextTypes.DEFAULT_TYPE, person: Person, state) -> None:
    await asyncio.to_thread(_repo(ctx).save_state, person.record_id, state)
    person.state = state


def _hello(person: Person) -> str:
    """«Ола, Лола!» / «Привет, Лола!» — по имени, если оно есть."""
    first = phrases.first_name(person.name)
    word = phrases.pick(phrases.HELLO)
    return f"{word}, {escape(first)}!" if first else f"{word}!"


def _has_answers(state) -> bool:
    """Партнёр что-то ввёл сам. В «Дополнить» ответы, подставленные из проекта, не считаются."""
    a = state.answers
    if "_ask" in a or a.get("_files_only"):
        asked = set(a.get("_ask") or ()) | {EXTRA_FILES_KEY}
        return any(a.get(k) not in (None, "", []) for k in asked)
    return any(not k.startswith("_") for k in a)


def _dialog_name(state) -> str:
    name = engine.scenario_of(state).title
    target = state.answers.get("_target")
    return f"{name} · {target['name']}" if target else name


async def _blocked(update: Update, ctx: ContextTypes.DEFAULT_TYPE, person: Person, pending: str) -> bool:
    """Открыт другой диалог с ответами — спросить, закрыть ли его без отправки (Да — сразу новое действие).
    Без ответов — закрываем молча. True — спросили, новое действие ждёт ответа."""
    if not person.state:
        return False
    if _has_answers(person.state):
        await _send(update, [engine.Screen(
            f"Закрыть «{escape(_dialog_name(person.state))}» без отправки? Ответы этой анкеты пропадут.",
            [[("Да, закрыть", f"{CB_DROP_AND_GO}{pending}")], [("Нет, вернуться к анкете", CB_MENU_RESUME)]],
        )])
        return True
    await _save(ctx, person, None)
    return False


# ---------- команды ----------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    person = await _identify(update, ctx)
    if not person:
        await _unknown(update, ctx)
        return
    if not person.reminder_slot:
        # Первое знакомство: рассказ о боте и время напоминаний
        welcome = WELCOME_TEXT.replace("Привет!", _hello(person), 1)
        await update.effective_chat.send_message(welcome, parse_mode=ParseMode.HTML, reply_markup=MENU_KEYBOARD)
        await _send(update, [_reminder_question(await _person_country(ctx, person))])
    else:
        greeting = GREETING_TEXT.replace("Привет, это", f"{_hello(person)} Это", 1)
        await update.effective_chat.send_message(greeting, reply_markup=MENU_KEYBOARD)
    if person.state:
        if _has_answers(person.state):
            await _send(update, [engine.Screen(
                f"Анкета «{escape(_dialog_name(person.state))}» ещё не отправлена. Вернуться к ней?",
                [[("↩️ Вернуться к ней", CB_MENU_RESUME)], [("🗑 Не отправлять", CB_DROP_AND_GO)]],
            )])
        else:
            await _save(ctx, person, None)


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    person = await _identify(update, ctx)
    if not person:
        await _unknown(update, ctx)
        return
    if await _blocked(update, ctx, person, CB_MENU_RESTART):
        return
    await _start_scenario(update, ctx, person)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    person = await _identify(update, ctx)
    if not person:
        await _unknown(update, ctx)
        return
    if person.state:
        await _save(ctx, person, None)
        await update.effective_chat.send_message("Отменено. Ничего не отправлено.", reply_markup=MENU_KEYBOARD)
    else:
        await update.effective_chat.send_message("Отменять нечего. Выбери действие внизу.", reply_markup=MENU_KEYBOARD)


async def _start_scenario(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, person: Person,
    code: str = NEW_PROJECT.code, preset: dict | None = None,
) -> None:
    if code == NEW_PROJECT.code:
        # Вопрос о звонке называет проверяющего по маршруту (Р8)
        reviewer = await _reviewer_of(ctx, person)
        preset = {**(preset or {}), REVIEWER_WITH: {routing.LOLA: "Лолой"}.get(reviewer, "Егором")}
    result = engine.start(code, preset)
    await _save(ctx, person, result.state)
    await _send(update, result.screens)


# ---------- ответы ----------

async def _apply(update: Update, ctx: ContextTypes.DEFAULT_TYPE, person: Person, result: engine.Result) -> None:
    if result.submit:
        await _submit(update, ctx, person)
        return
    await _save(ctx, person, result.state)
    await _send(update, result.screens)


async def _handle_answer_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    person = await _identify(update, ctx)
    if not person:
        await _unknown(update, ctx)
        return
    if not person.state:
        if await admin.dealer_call_text(update, ctx, text):
            return
        if admin.is_admin(update, ctx):
            await update.effective_chat.send_message(
                "🛠 Сейчас я ничего не жду от тебя. Заявки на проверке с кнопками решения — /inbox.",
                reply_markup=MENU_KEYBOARD,
            )
            return
        await update.effective_chat.send_message(DO_SOMETHING, reply_markup=MENU_KEYBOARD)
        return
    await _apply(update, ctx, person, engine.handle_text(person.state, text))


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or msg.text is None:
        return
    text = msg.text.strip()
    if text in (MENU_NEW, MENU_UPDATE, MENU_MY, MENU_START):
        await admin.drop_pending(update, ctx)  # нажал меню — ввод для админа отменён
    elif await admin.on_admin_text(update, ctx, text, resume=False):
        return
    elif admin.is_admin(update, ctx):
        # Ожидание из Airtable подхватываем, только если у админа нет своего диалога «как дилер»
        me = await _identify(update, ctx)
        if me and not me.state and await admin.on_admin_text(update, ctx, text, resume=True):
            return
    if text.lower() in (MENU_START.lower(), "start"):
        await cmd_start(update, ctx)
        return
    if text == MENU_NEW:
        await cmd_new(update, ctx)
        return
    if text in (MENU_UPDATE, MENU_MY):
        person = await _identify(update, ctx)
        if not person:
            await _unknown(update, ctx)
        elif text == MENU_MY:
            await _show_project_list(update, ctx, person, MODE_MY, 0)
        else:
            await _send(update, [engine.Screen("Что делаем?", [
                [("Дополнить материалы", f"{CB_UPDATE_MODE}{MODE_FILL}")],
                [("Сообщить новость", f"{CB_UPDATE_MODE}{MODE_NEWS}")],
                [("⏭ Запросить предрегистрацию / регистрацию", f"{CB_UPDATE_MODE}{MODE_NEXT}")],
            ])])
        return
    await _handle_answer_text(update, ctx, text)


DO_SOMETHING = "Выбери действие внизу: «Новый проект», «Обновить проект» или «Мои проекты»."
CLOSED_TEXT = "Эта заявка уже закрыта — продолжать нечего. Выбери действие внизу."


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    media = (msg.voice or msg.audio) if msg is not None else None
    if media is None:
        return
    if not await _identify(update, ctx):   # голосовые незнакомых не расшифровываем
        await _unknown(update, ctx)
        return
    tg_file = await media.get_file()
    audio = bytes(await tg_file.download_as_bytearray())
    try:
        text = await ctx.bot_data["transcriber"].transcribe(audio)
    except Exception:
        log.exception("Groq: ошибка распознавания")
        await update.effective_chat.send_message("Не удалось распознать голосовое. Напиши текстом, пожалуйста.")
        return
    if not text:
        await update.effective_chat.send_message("Голосовое пустое — я ничего не расслышал. Повтори или напиши текстом.")
        return
    await update.effective_chat.send_message(f"🎙 <i>{escape(text)}</i>", parse_mode=ParseMode.HTML)
    await _keep_news_audio(update, ctx, media)
    await _handle_answer_text(update, ctx, text)


async def _keep_news_audio(update: Update, ctx: ContextTypes.DEFAULT_TYPE, media) -> None:
    """Новость голосом: кроме расшифровки сохраняем сам файл — он уйдёт во вложения заявки."""
    person = await _identify(update, ctx)
    if not person or not person.state or person.state.scenario not in (NEWS.code, INFO_REPLY.code):
        return
    if (media.file_size or 0) > MAX_FILE_BYTES:
        return  # больше лимита Airtable — останется только расшифровка
    ext = "ogg" if update.effective_message and update.effective_message.voice else "mp3"
    person.state.answers["Dealer News Audio"] = [{
        "kind": "file", "file_id": media.file_id, "file_unique_id": media.file_unique_id,
        "name": f"voice_{media.file_unique_id}.{ext}", "size": media.file_size,
        "mime": getattr(media, "mime_type", None) or "audio/ogg",
    }]
    await _save(ctx, person, person.state)


# Нажатия, после которых показывать «✅ выбранное» нет смысла (навигация, служебное)
_NO_MARK_PREFIXES = ("pl:", "am:", "menu:", "rx:")


async def _mark_choice(query) -> None:
    """Вместо кнопок — строка «✅ <что нажали>», чтобы в переписке был виден ответ."""
    label = None
    markup = query.message.reply_markup if query.message else None
    for row in (markup.inline_keyboard if markup else []):
        for button in row:
            if button.callback_data == query.data:
                label = button.text
    text_html = query.message.text_html if query.message and query.message.text else None
    if not label or not text_html or (query.data or "").startswith(_NO_MARK_PREFIXES):
        await query.edit_message_reply_markup(None)
        return
    await query.edit_message_text(f"{text_html}\n✅ <i>{escape(label)}</i>", parse_mode=ParseMode.HTML,
                                  reply_markup=None)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    # Убираем кнопки у нажатого сообщения (защита от двойных нажатий) и оставляем выбранный ответ
    try:
        await _mark_choice(query)
    except Exception:
        try:
            await query.edit_message_reply_markup(None)
        except Exception:
            pass

    person = await _identify(update, ctx)
    if not person:
        await _unknown(update, ctx)
        return

    await _route(update, ctx, person, query.data or "")


async def _route(update: Update, ctx: ContextTypes.DEFAULT_TYPE, person: Person, data: str) -> None:
    if data.startswith(preview_mod.CB_PREVIEW):
        return   # кнопки в /preview — только для вида
    if data.startswith(admin.CB_ADMIN):
        await admin.on_admin_callback(update, ctx, data)
        return
    if data.startswith(CB_ADMIN_MENU):
        await _admin_menu_action(update, ctx, data[len(CB_ADMIN_MENU):])
        return
    if data.startswith(CB_DROP_AND_GO):
        if person.state:
            await _save(ctx, person, None)
        pending = data[len(CB_DROP_AND_GO):]
        if pending:
            await _route(update, ctx, person, pending)
        else:
            await update.effective_chat.send_message("Отменено.", reply_markup=MENU_KEYBOARD)
        return
    if data == CB_MENU_RESTART:
        await _start_scenario(update, ctx, person)
        return
    if data == CB_MENU_RESUME:
        if person.state:
            await _send(update, [engine.current_screen(person.state)])
        else:
            await update.effective_chat.send_message(CLOSED_TEXT, reply_markup=MENU_KEYBOARD)
        return
    if data.startswith(admin.CB_DEALER_CALL):
        await admin.dealer_call_callback(update, ctx, data)
        return
    if data.startswith(CB_LOOSE_FILE):
        await _loose_file_to_project(update, ctx, person, data[len(CB_LOOSE_FILE):])
        return

    if data.startswith(CB_REPLY):
        await _start_reply(update, ctx, person, data[len(CB_REPLY):])
        return
    if data.startswith(CB_REMINDER):
        await _save_reminder(update, ctx, person, data[len(CB_REMINDER):])
        return
    if data.startswith(CB_UPDATE_MODE):
        await _show_project_list(update, ctx, person, data[len(CB_UPDATE_MODE):], 0)
        return
    if data.startswith(CB_LIST):
        mode, _, page = data[len(CB_LIST):].partition(":")
        await _show_project_list(update, ctx, person, mode, int(page) if page.isdigit() else 0)
        return
    if data.startswith(CB_PICK):
        mode, _, project_id = data[len(CB_PICK):].partition(":")
        if mode == MODE_MY:
            await _show_card(update, ctx, person, project_id)
        else:
            await _start_action(update, ctx, person, mode, project_id)
        return
    if data.startswith(CB_CARD):
        action, _, project_id = data[len(CB_CARD):].partition(":")
        await _start_action(update, ctx, person, action, project_id)
        return

    if not person.state:
        await update.effective_chat.send_message(CLOSED_TEXT, reply_markup=MENU_KEYBOARD)
        return
    await _apply(update, ctx, person, engine.handle_callback(person.state, data))


# ---------- мои проекты / обновить проект ----------

def _date(iso: str | None) -> str:
    if not iso:
        return ""
    y, m, d = iso[:10].split("-")
    return f"{d}.{m}.{y}"


def _days_left(p: DealerProject) -> int | None:
    """Дней до Status Expiry — только для предрегистрации и регистрации."""
    if p.ppr_status == PPR_NOTIFIED or not p.status_expiry:
        return None
    today = datetime.now(VALENCIA).date()
    return (date.fromisoformat(p.status_expiry[:10]) - today).days


def _sorted(projects: list[DealerProject]) -> list[DealerProject]:
    """Срочные сверху; без счётчика (уведомления) — в конце."""
    def key(p: DealerProject):
        days = _days_left(p)
        return (days is None, days if days is not None else 0, p.name.lower())
    return sorted(projects, key=key)


def _button_label(p: DealerProject, mode: str = "") -> str:
    label = f"{cards.name_ref(p.name[:40], p.fields.get(Projects.PPR_REF) or '')} · {PPR_LABELS[p.ppr_status].lower()}"
    if mode == MODE_NEXT and _next_request_type(p):
        return f"{label} → ✔️ {NEXT_LABEL[p.ppr_status]}"
    days = _days_left(p)
    if days is not None:
        label += f" · {days} дн." if days >= 0 else " · срок истёк"
    return label


async def _projects(ctx: ContextTypes.DEFAULT_TYPE, person: Person) -> list[DealerProject]:
    return _sorted(await asyncio.to_thread(_repo(ctx).dealer_projects, person.record_id))


async def _show_project_list(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, person: Person, mode: str, page: int
) -> None:
    if mode != MODE_MY and await _blocked(update, ctx, person, f"{CB_LIST}{mode}:{page}"):
        return
    projects = await _projects(ctx, person)
    pending = await asyncio.to_thread(_repo(ctx).pending_requests, person.record_id) if mode == MODE_MY else []
    if not projects:
        text = "У тебя пока нет проектов в программе. Нажми «Новый проект»."
        if pending:
            text = pending_text(pending) + "\n\n" + text
        await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=MENU_KEYBOARD)
        return
    pages = (len(projects) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, pages - 1))
    chunk = projects[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    buttons = [[(_button_label(p, mode), f"{CB_PICK}{mode}:{p.record_id}")] for p in chunk]
    nav = []
    if page > 0:
        nav.append(("← Назад", f"{CB_LIST}{mode}:{page - 1}"))
    if page < pages - 1:
        nav.append(("Дальше →", f"{CB_LIST}{mode}:{page + 1}"))
    if nav:
        buttons.append(nav)
    title = {
        MODE_MY: "Твои проекты",
        MODE_FILL: "Какой проект дополнить?",
        MODE_NEWS: "По какому проекту новость?",
        MODE_NEXT: "По какому проекту запросить? ✔️ — условия выполнены",
    }.get(mode, "Твои проекты")
    if pages > 1:
        title += f" ({page + 1}/{pages})"
    if pending and page == 0:
        title = pending_text(pending) + "\n\n" + title
    await _send(update, [engine.Screen(title, buttons)])


PENDING_LABELS = {
    RequestType.NEW_NOTIFICATION: "уведомление", RequestType.NEW_PRE_REGISTRATION: "предрегистрация",
    RequestType.NEW_REGISTRATION: "регистрация", RequestType.UPGRADE_PRE_REGISTRATION: "переход на предрегистрацию",
    RequestType.UPGRADE_REGISTRATION: "переход на регистрацию", RequestType.UPDATE_FIELDS: "дополнение",
    RequestType.UPDATE_NEWS: "новость", RequestType.UPDATE_INFO: "обновление", RequestType.UPDATE_WON: "выигран",
    RequestType.UPDATE_LOST: "проигран", RequestType.QUOTE_SIMPLIFIED: "квотация",
}


def pending_text(pending: list) -> str:
    """«⏳ На проверке: 2» и по строке на заявку — дилер видит, что заявка дошла."""
    lines = [f"⏳ <b>На проверке: {len(pending)}</b>"]
    for r in pending[:PAGE_SIZE]:
        kind = PENDING_LABELS.get(r.request_type or "", "заявка")
        since = f", с {r.submitted_at[8:10]}.{r.submitted_at[5:7]}" if r.submitted_at else ""
        lines.append(f"• {escape(r.name)} — {kind}{since}")
    if len(pending) > PAGE_SIZE:
        lines.append(f"• и ещё {len(pending) - PAGE_SIZE}")
    return "\n".join(lines)


async def _own_project(ctx: ContextTypes.DEFAULT_TYPE, person: Person, project_id: str) -> DealerProject | None:
    """Проект из списка дилера — кнопки не дают открыть чужой проект."""
    projects = await asyncio.to_thread(_repo(ctx).dealer_projects, person.record_id)
    return next((p for p in projects if p.record_id == project_id), None)


async def _show_card(update: Update, ctx: ContextTypes.DEFAULT_TYPE, person: Person, project_id: str) -> None:
    p = await _own_project(ctx, person, project_id)
    if not p:
        await update.effective_chat.send_message("Проект не найден. Открой «Мои проекты» заново.")
        return
    # Только своё: имя, статус дилера, срок, стадия, квотация. Других дилеров, лог, разбор, баллы — никогда.
    lines = [f"🏗 {cards.project_line(p.fields)}"]
    lines.append(f"Твой статус: {PPR_LABELS[p.ppr_status].lower()}")
    days = _days_left(p)
    if days is not None:
        left = f"осталось {days} дн." if days >= 0 else "срок истёк"
        lines.append(f"Действует до {_date(p.status_expiry)}, {left}")
    stage = p.fields.get(Projects.PROJECT_STAGE)
    if stage:
        lines.append(f"Стадия: {STAGE_LABELS.get(stage, stage)}")
    quotation = p.fields.get(Projects.QUOTATION)
    if quotation:
        lines.append(f"Квотация: {escape(str(quotation))}")
    if p.track == SIMPLIFIED:
        lines.append("Схема: регистрация по упрощённой схеме — объём до 30 000 € по прайсу")
        q = (await asyncio.to_thread(_repo(ctx).dealer_quotes, person.record_id)).get(p.record_id)
        if q and q.get("valid"):
            no = f"№ {escape(str(q['no']))}, " if q.get("no") else ""
            lines.append(f"Квотация: {no}действует до {_date(q['valid'])}")
        elif q:
            lines.append(f"Квотация: запрошена {_date(q['at'])}" + ("" if q.get("accepted") else ", на проверке"))
    if days is not None and days <= EXPIRY_WARNING_DAYS:
        lines += ["", "Срок подходит к концу. Если по проекту есть прогресс — нажми «Сообщить новость», "
                      "чтобы запросить продление."]
    pid = p.record_id
    ready = NEXT_FORMS[p.ppr_status] if _next_request_type(p) else None
    if ready:
        lines += ["", f"✔️ Условия для {ready[1]} выполнены"]
    buttons = [
        *([[(f"⏭ Запросить {ready[0]}", f"{CB_CARD}next:{pid}")]] if ready else []),
        # До 30 000 €: регистрация = номер квотации бэк-офиса
        *([[("🧾 Прислать номер квотации", f"{CB_CARD}quote:{pid}")]] if p.track == SIMPLIFIED else []),
        [("Дополнить материалы", f"{CB_CARD}fill:{pid}")],
        [("Сообщить новость", f"{CB_CARD}news:{pid}")],
        [("Выигран", f"{CB_CARD}won:{pid}"), ("Проиграл", f"{CB_CARD}lost:{pid}")],
        [("← Назад", f"{CB_LIST}{MODE_MY}:0")],
    ]
    await _send(update, [engine.Screen("\n".join(lines), buttons)])


async def _start_action(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, person: Person, action: str, project_id: str
) -> None:
    if action in ("next", "nextok"):
        await _request_next_stage(update, ctx, person, project_id, confirmed=action == "nextok")
        return
    materials_only = action == "fillmat"   # «Добавить материалы» с экрана «всё есть»
    if materials_only:
        action = "fill"
    if action not in CARD_ACTIONS:
        return
    if await _blocked(update, ctx, person, f"{CB_CARD}{action}:{project_id}"):
        return
    p = await _own_project(ctx, person, project_id)
    if not p:
        await update.effective_chat.send_message("Проект не найден. Открой список заново.")
        return
    target = {"id": p.record_id, "name": p.name}
    preset = fill_preset(target, p.fields, p.ppr_status) if action == "fill" else {"_target": target}
    if preset.get("_files_only") and not materials_only and _next_request_type(p):
        # Дополнять нечего, а для следующего этапа всё есть — предложить запросить его
        what = NEXT_FORMS[p.ppr_status]
        await _send(update, [engine.Screen(
            f"🏗 {cards.project_line(p.fields)}\nОбязательные поля заполнены — для {what[1]} по проекту есть всё. "
            f"Запроси {what[0]}, {await _reviewer_name(ctx, person)} проверит.",
            [[(f"⏭ Запросить {what[0]}", f"{CB_CARD}next:{p.record_id}")],
             [("📎 Добавить материалы", f"{CB_CARD}fillmat:{p.record_id}")]])])
        return
    await _start_scenario(update, ctx, person, CARD_ACTIONS[action].code, preset)


async def _start_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE, person: Person, reg_id: str) -> None:
    if await _blocked(update, ctx, person, f"{CB_REPLY}{reg_id}"):
        return
    try:
        reg = await asyncio.to_thread(_repo(ctx).registration_fields, reg_id)
    except Exception:
        await update.effective_chat.send_message(CLOSED_TEXT, reply_markup=MENU_KEYBOARD)
        return
    # Отвечать может только компания, от которой заявка
    if not set(reg.get(Registrations.DEALER) or []) & set(person.company_ids):
        await update.effective_chat.send_message(CLOSED_TEXT, reply_markup=MENU_KEYBOARD)
        return
    target = reg.get(Registrations.TARGET_PROJECT) or []
    preset = {
        "_target": {"id": target[0] if target else None,
                    "name": reg.get(Registrations.DEALER_PROJECT_NAME) or "заявка"},
        "_reply_to": reg_id,
    }
    await _start_scenario(update, ctx, person, INFO_REPLY.code, preset)


def _next_request_type(p: DealerProject) -> str | None:
    """Можно ли просить следующий этап: Full, данные для этапа есть, регистрации на объекте нет."""
    if p.track == SIMPLIFIED or p.fields.get(Projects.PPR_NEXT_MISSING) or p.fields.get(Projects.REGISTERED):
        return None
    return NEXT_REQUEST.get(p.ppr_status)


async def _request_next_stage(update: Update, ctx: ContextTypes.DEFAULT_TYPE, person: Person,
                              project_id: str, confirmed: bool) -> None:
    p = await _own_project(ctx, person, project_id)
    if not p:
        await update.effective_chat.send_message("Проект не найден. Открой список заново.")
        return
    request_type = _next_request_type(p)
    if not request_type:
        if p.track == SIMPLIFIED:
            # До 30 000 € следующий этап — номер квотации
            await _start_action(update, ctx, person, "quote", project_id)
            return
        if p.fields.get(Projects.REGISTERED):
            why = (f"🏗 {cards.project_line(p.fields)}\nРегистрация на объекте уже у другого партнёра — "
                   "следующий этап сейчас недоступен. Делать ничего не нужно.")
        else:
            why = f"🏗 {cards.project_line(p.fields)}\n" + "\n".join(cards.next_step_body(dict(p.fields), person.record_id))
        await update.effective_chat.send_message(why, parse_mode=ParseMode.HTML, reply_markup=MENU_KEYBOARD)
        return
    what = NEXT_LABEL[p.ppr_status]
    rivals = set(p.fields.get(Projects.PRE_REGISTERED) or []) - {person.record_id}
    if request_type == RequestType.UPGRADE_REGISTRATION and rivals:
        # Спор за регистрацию: запрос уходит только вместе с материалами
        preset = {"_target": {"id": p.record_id, "name": p.name}, "_files_only": True, CONTESTED: True}
        await _start_scenario(update, ctx, person, FILL.code, preset)
        return
    reviewer = await _reviewer_name(ctx, person)
    if not confirmed:
        await _send(update, [engine.Screen(
            f"Запросить {what} по {cards.project_line(p.fields)}? Все данные для этапа есть — "
            f"{reviewer} посмотрит и ответит.",
            [[("Да, запросить", f"{CB_CARD}nextok:{p.record_id}")], [("← Назад", f"{CB_LIST}{MODE_MY}:0")]],
        )])
        return
    payload = {"Dealer Project Name": p.name}
    reg_id = await asyncio.to_thread(_repo(ctx).create_registration, person, request_type, payload, p.name, p.record_id)
    await update.effective_chat.send_message(
        f"{phrases.pick(phrases.THANKS)} Запрос на {what} по {cards.project_line(p.fields)} передал "
        f"{DATIVE.get(await _reviewer_of(ctx, person), 'Егору')}. Ответ — в течение 72 часов, придёт сюда.",
        parse_mode=ParseMode.HTML, reply_markup=MENU_KEYBOARD,
    )
    await _notify_admin_new(ctx, person, reg_id, request_type, payload, p.name)


# ---------- напоминания ----------

def _reminder_question(country: str | None = None) -> engine.Screen:
    items = [(slot, f"{CB_REMINDER}{i}") for i, slot in enumerate(REMINDER_SLOTS)]
    zone = reminders.PERSON_TZ.get(country or "", "Europe/Moscow")
    zone_ru = {"Europe/Moscow": "по Москве"}.get(zone, f"по стране в базе ({slots.zone_name(country)})")
    return engine.Screen(REMINDER_QUESTION.format(zone=zone_ru), [items[:2], items[2:]])


async def _save_reminder(update: Update, ctx: ContextTypes.DEFAULT_TYPE, person: Person, raw: str) -> None:
    if not raw.isdigit() or int(raw) >= len(REMINDER_SLOTS):
        return
    slot = REMINDER_SLOTS[int(raw)]
    await asyncio.to_thread(_repo(ctx).set_reminder_slot, person.record_id, slot)
    await update.effective_chat.send_message(
        f"Готово: напоминания — с {slot.replace('–', ' до ')}. Изменить время — /time",
        reply_markup=MENU_KEYBOARD,
    )


async def cmd_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    person = await _identify(update, ctx)
    if not person:
        await _unknown(update, ctx)
        return
    await _send(update, [_reminder_question(await _person_country(ctx, person))])


async def _person_country(ctx: ContextTypes.DEFAULT_TYPE, person: Person) -> str | None:
    try:
        return await asyncio.to_thread(lambda: _repo(ctx)._person_fields(person.record_id).get(People.COUNTRY))
    except Exception:
        return None


async def _reviewer_of(ctx: ContextTypes.DEFAULT_TYPE, person: Person) -> str:
    """Кто разбирает заявки этого человека (с учётом того, есть ли Лола в боте)."""
    types = await asyncio.to_thread(_repo(ctx).company_types, person.company_ids)
    return routing.resolve(ctx.bot_data, routing.route(types))[0]


async def _reviewer_name(ctx: ContextTypes.DEFAULT_TYPE, person: Person) -> str:
    return routing.NAMES.get(await _reviewer_of(ctx, person), "Егор")


async def _submit(update: Update, ctx: ContextTypes.DEFAULT_TYPE, person: Person) -> None:
    state = person.state
    sc = engine.scenario_of(state)
    request_type, stage_label = engine.stage_of(state)
    payload = engine.build_payload(state)
    name = payload.get(sc.project_name_key) if sc.project_name_key else None
    files = engine.collected_files(state)
    if sc.code == FILL.code and not files and not _has_answers(state):
        # «Дополнить», где ничего не добавили, — пустую заявку проверяющему не шлём (DLR-24)
        await _save(ctx, person, None)
        pid = (state.answers.get("_target") or {}).get("id")
        await _send(update, [engine.Screen(engine.NOTHING_NEW, engine.nothing_new_buttons(pid))])
        return
    if files:
        await update.effective_chat.send_message("Сохраняю заявку и файлы…")
    try:
        reg_id = await asyncio.to_thread(
            _repo(ctx).create_registration, person, request_type, payload, name,
            (state.answers.get("_target") or {}).get("id"),
        )
    except Exception:
        log.exception("Airtable: не удалось создать заявку")
        await update.effective_chat.send_message(
            "Не получилось сохранить заявку — попробуй ещё раз через минуту. Ответы не потеряны.",
        )
        await _send(update, [engine.summary_screen(state)])
        return
    # Заявка создана — чистим состояние сразу, чтобы повторное нажатие не создало дубль
    await _save(ctx, person, None)
    failed = await _upload_files(ctx, reg_id, files)
    reviewer = await _reviewer_of(ctx, person)
    done = f"{phrases.pick(phrases.THANKS)} " + sc.done_text.format(
        reviewer=DATIVE.get(reviewer, "Егору"), who=routing.NAMES.get(reviewer, "Егор"))
    if failed:
        done += (f"\n\nНе загрузилось файлов: {len(failed)} ({', '.join(failed)}). Пришли их ссылкой: "
                 "«Обновить проект» → «Дополнить материалы».")
    await update.effective_chat.send_message(done, reply_markup=MENU_KEYBOARD)
    log.info("Заявка %s от %s (%s), файлов %d", reg_id, person.record_id, request_type, len(files))

    await _notify_admin_new(ctx, person, reg_id, request_type, payload, name, failed)


async def _notify_admin_new(ctx: ContextTypes.DEFAULT_TYPE, person: Person, reg_id: str,
                            request_type: str, payload: dict, name: str | None,
                            failed: list[str] | None = None) -> None:
    """Карточка новой заявки админу: поиск совпадений, Analysis, кнопки решения."""
    analysis = None
    candidates: list = []
    if request_type.startswith("New"):
        try:
            found = dedup.find(payload, name or "", await asyncio.to_thread(_repo(ctx).dedup_projects))
            analysis = dedup.analysis_text(found)
            candidates = [(c.project_id, c.name) for c in found]
            await asyncio.to_thread(_repo(ctx).set_analysis, reg_id, analysis)
        except Exception:
            log.exception("Поиск дублей для %s", reg_id)

    # Дилерские заявки — Лоле, остальные — Егору
    types = await asyncio.to_thread(_repo(ctx).company_types, person.company_ids)
    _, admin_chat = routing.resolve(ctx.bot_data, routing.route(types))
    if admin_chat:
        try:
            card_fields = {
                Registrations.DEALER_PROJECT_NAME: name,
                Registrations.REQUEST_TYPE: request_type,
                Registrations.PAYLOAD: json.dumps(payload, ensure_ascii=False),
            }
            extra = []
            if request_type in admin.UPGRADE_REQUESTS:
                reg = await asyncio.to_thread(_repo(ctx).registration_fields, reg_id)
                extra = await admin.upgrade_digest(_repo(ctx), reg)
            await admin.send_card(
                ctx.bot, admin_chat,
                admin.request_card(card_fields, reg_id, person.name, analysis, _sender_label(ctx, person, payload),
                                   extra, await admin.asked_of(_repo(ctx), card_fields), failed),
                admin.decision_markup(ctx, reg_id, candidates),
            )
            await admin.send_call_offer(ctx.bot, admin_chat, reg_id, card_fields, person.name)
        except Exception:
            log.exception("Не удалось уведомить проверяющего %s", admin_chat, extra={
                "alert": f"Не дошла карточка новой заявки «{name or 'без названия'}» от {person.name}. "
                         "Открой /inbox — она там.",
                "reviewer": routing.who_by_chat(ctx.bot_data, admin_chat) or routing.EGOR})


def _sender_label(ctx: ContextTypes.DEFAULT_TYPE, person, payload: dict) -> str | None:
    """Компания отправителя для карточки; с общего профиля AW — партнёр по стране объекта."""
    if person.record_id in Partners.SENDERS:
        partner = Partners.BY_COUNTRY.get(str(payload.get("Country") or ""))
        return f"партнёр {_repo(ctx).company_name(partner)}" if partner else "партнёр не определён (страна)"
    return ", ".join(_repo(ctx).company_name(c) for c in person.company_ids) or None


async def _upload_files(ctx: ContextTypes.DEFAULT_TYPE, reg_id: str, files: list) -> list[str]:
    """Файлы из Telegram → вложения заявки. Возвращает имена незагруженных."""
    failed = []
    for n, (key, item) in enumerate(files, 1):
        filename = f"{n:02d} {key} - {item.get('name') or 'file'}"
        try:
            tg_file = await ctx.bot.get_file(item["file_id"])
            content = bytes(await tg_file.download_as_bytearray())
            await asyncio.to_thread(
                _repo(ctx).upload_attachment, reg_id, filename, content, item.get("mime")
            )
        except Exception:
            log.warning("Не удалось загрузить файл %s в %s", item.get("name"), reg_id, exc_info=True)
            failed.append(item.get("name") or "file")
    return failed


# ---------- файлы ----------

async def on_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or not (msg.document or msg.photo):
        return
    if msg.document:
        doc = msg.document
        item = {"kind": "file", "file_id": doc.file_id, "file_unique_id": doc.file_unique_id,
                "name": doc.file_name or f"file_{doc.file_unique_id}", "size": doc.file_size,
                "mime": doc.mime_type}
    else:
        ph = msg.photo[-1]  # самое большое разрешение
        item = {"kind": "file", "file_id": ph.file_id, "file_unique_id": ph.file_unique_id,
                "name": f"photo_{ph.file_unique_id}.jpg", "size": ph.file_size, "mime": "image/jpeg"}

    person = await _identify(update, ctx)
    if not person:
        await _unknown(update, ctx)
        return
    if (item["size"] or 0) > MAX_FILE_BYTES:
        await update.effective_chat.send_message(
            f"Файл «{item['name']}» больше {MAX_FILE_BYTES // 1024 // 1024} МБ — пришли его ссылкой "
            "(Яндекс Диск, Google Drive, облако)."
        )
        return
    if not person.state:
        await _loose_file(update, ctx, person, item)
        return
    await _apply(update, ctx, person, engine.handle_file(person.state, item))


async def _loose_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE, person: Person, item: dict) -> None:
    """Файл вне анкеты не выбрасываем молча: спросить, к какому проекту он относится (DLR-04)."""
    projects = await _projects(ctx, person)
    if not projects:
        await update.effective_chat.send_message(
            "Файл получил, но проектов у тебя пока нет. Сначала подай проект — «Новый проект».",
            reply_markup=MENU_KEYBOARD)
        return
    ctx.bot_data.setdefault("loose_files", {}).setdefault(person.record_id, []).append(item)
    buttons = [[(_button_label(p), f"{CB_LOOSE_FILE}{p.record_id}")] for p in projects[:PAGE_SIZE]]
    buttons.append([("Не прикладывать", f"{CB_LOOSE_FILE}-")])
    await _send(update, [engine.Screen(f"Файл «{escape(item['name'])}» получил. К какому проекту он относится?",
                                       buttons)])


async def _loose_file_to_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE, person: Person, project_id: str) -> None:
    files = ctx.bot_data.setdefault("loose_files", {}).pop(person.record_id, [])
    if project_id == "-" or not files:
        await update.effective_chat.send_message("Хорошо, файл не прикладываю. " + DO_SOMETHING,
                                                 reply_markup=MENU_KEYBOARD)
        return
    p = await _own_project(ctx, person, project_id)
    if not p:
        await update.effective_chat.send_message("Проект не найден. Открой «Мои проекты» заново.")
        return
    if await _blocked(update, ctx, person, f"{CB_LOOSE_FILE}{project_id}"):
        ctx.bot_data["loose_files"][person.record_id] = files
        return
    preset = {"_target": {"id": p.record_id, "name": p.name}, "_files_only": True, EXTRA_FILES_KEY: files}
    await _start_scenario(update, ctx, person, FILL.code, preset)


async def log_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    kind = "callback" if update.callback_query else "message" if update.message else "other"
    log.info(
        "update %s: kind=%s chat=%s type=%s user=@%s",
        update.update_id, kind, chat.id if chat else None,
        chat.type if chat else None, user.username if user else None,
    )


# ---------- решения Егора по заявкам ----------

DECISIONS_INTERVAL = 60  # секунд между проверками Inbox


async def _send_html(ctx_bot, chat_id, text: str, markup=None) -> None:
    try:
        await ctx_bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except Exception:
        log.exception("Не удалось отправить сообщение в чат %s", chat_id)


async def _link_architect(repo: AirtableRepo, reg: dict, project: dict, project_id: str,
                          new_specifier: list | None, dealers: list) -> str | None:
    """ГАП из заявки → People (компания — бюро, роль «ГАП») и в участники проекта. Строка для итога."""
    payload = payload_of(reg)
    name = str(payload.get("Lead Architect") or "").strip()
    if not name or payload.get("Specifier Type") == SOLO_ARCHITECT:
        return None  # частный архитектор уже стал «бюро» своего имени
    bureau = (new_specifier or project.get(Projects.SPECIFIER) or [None])[0]
    if not bureau or bureau in dealers:
        return None
    person_id, created = await asyncio.to_thread(repo.find_or_create_person, name, bureau, "ГАП")
    people = list(project.get(Projects.PEOPLE) or [])
    if person_id not in people:
        await asyncio.to_thread(repo.update_project, project_id, {Projects.PEOPLE: people + [person_id]})
    return f"главный архитектор проекта {name} ({'добавлен в базу людей' if created else 'уже был в базе'})"


async def process_decisions(app: Application) -> None:
    # Минутный цикл и кнопки админа не должны обработать одну заявку дважды
    async with app.bot_data.setdefault("decisions_lock", asyncio.Lock()):
        await _process_decisions(app)


LINK_ROLE = {Projects.END_CLIENT: "клиент", Projects.SPECIFIER: "бюро", Projects.DEALER: "дилер",
             Projects.PARTNER: "партнёр"}
DECISION_HEAD = {
    "Accept": "✅ Принято", "Accept — merge into Target Project": "✅ Принято",
    "Decline": "❌ Отклонено", "Need more info": "❓ Запрошена информация",
}


def _admin_summary(reg: dict, pl, project_name: str, created: bool, dealer: str, sent: bool,
                   companies: list[str], terms: str | None) -> str:
    """Итог решения для админа: что сделано, без лишнего."""
    decision = reg.get(Registrations.DECISION) or ""
    head = DECISION_HEAD.get(decision, decision)
    title = project_name if decision.startswith("Accept") else (reg.get(Registrations.DEALER_PROJECT_NAME) or "")
    lines = [f"🛠 <b>{head} · {escape(title)}</b>"]
    note = (reg.get(Registrations.DECISION_NOTE) or "").strip()
    for change in pl.changes:
        lines.append(f"• {change}")
    if pl.status_label:
        lines.append(f"• {escape(dealer)}: статус <b>{pl.status_label}</b>")
    if pl.quote:
        lines.append("• Запрос квотации передан в работу")
        if terms:
            lines.append(f"• Условия: <b>{escape(terms)}</b>")
    if decision.startswith("Accept"):
        if created:
            lines.append("• Проект создан")
        elif (pl.transferred or pl.log_lines) and not pl.final_text:
            lines.append("• Проект дополнен данными из заявки")
        if companies:
            lines.append("• Компании: " + escape(", ".join(companies)))
    if pl.dealer_text:
        lines.append(("• Дилеру отправлено:" if sent else "• ⚠️ У дилера нет Telegram в базе — не отправлено:")
                     + f"\n<i>{pl.dealer_text}</i>")
    else:
        if note:
            lines.append(f"• Комментарий: {escape(note)}")
        lines.append("• Дилеру отправлено" if sent else "• ⚠️ У дилера нет Telegram в базе — не отправлено")
    return "\n".join(lines)


async def _process_decisions(app: Application) -> None:
    repo: AirtableRepo = app.bot_data["repo"]
    for rec in await asyncio.to_thread(repo.decisions_to_process):
        await _handle_registration(app, rec["id"], rec["fields"])
    for rec in await asyncio.to_thread(repo.reg_status_events):
        await _handle_reg_status_event(app, rec["id"], rec["fields"])


# Reg Status → (Request Type для плана, ключ статуса менеджера в проекте)
STATUS_EVENTS = {
    RegStatus.NOTIFICATION: (RequestType.NEW_NOTIFICATION, PPR_NOTIFIED),
    RegStatus.PRE_REGISTRATION: (RequestType.UPGRADE_PRE_REGISTRATION, PPR_PRE_REGISTERED),
    RegStatus.REGISTRATION: (RequestType.UPGRADE_REGISTRATION, PPR_REGISTERED),
}
UPGRADE_TARGETS = {"pre": RegStatus.PRE_REGISTRATION, "reg": RegStatus.REGISTRATION}


def _manager_status(project: dict, person: str) -> str | None:
    return cards.dealer_status(project, person)


async def _handle_reg_status_event(app: Application, rec_id: str, reg: dict) -> None:
    """Reg Status сменили руками (в Airtable) — это событие, независимо от Request Type и Processed."""
    repo: AirtableRepo = app.bot_data["repo"]
    status = reg.get(Registrations.REG_STATUS)
    consume = {Registrations.BOT_REG_STATUS: status}
    name = reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия"
    target = reg.get(Registrations.TARGET_PROJECT) or []
    managers = reg.get(Registrations.SUBMITTED_BY) or []
    admin_chat = app.bot_data.get("admin_chat_id")
    if status not in STATUS_EVENTS:
        await asyncio.to_thread(repo.update_registration, rec_id, consume)
        if status in (RegStatus.DECLINED, "Cancelled") and admin_chat and not reg.get(Registrations.DECISION):
            await _send_html(app.bot, admin_chat,
                             f"🛠 ℹ️ Заявка «{escape(name)}» помечена как {labels.REG_STATUS_RU.get(status, status)}. "
                             "Дилеру я не писал. Сообщить ему — кнопка «Отклонить» ниже.",
                             admin.blocked_markup(rec_id, "no_author", reg))
        return
    if not target or not managers:
        await asyncio.to_thread(repo.update_registration, rec_id, consume)
        if admin_chat:
            why = "не выбран проект" if not target else "в заявке нет менеджера дилера"
            status_ru = labels.REG_STATUS_RU.get(status, status)
            await _send_html(app.bot, admin_chat, admin.blocked_text(
                f"🛠 ⚠️ Статус «{status_ru}» по «{escape(name)}» не поставлен: {why}."),
                admin.blocked_markup(rec_id, "no_project" if not target else "no_author", reg,
                                     await admin.merge_candidates(repo, reg)))
        return
    request_type, key = STATUS_EVENTS[status]
    project = await asyncio.to_thread(repo.project_fields, target[0])
    if _manager_status(project, managers[0]) == key:
        await asyncio.to_thread(repo.update_registration, rec_id, consume)  # уже стоит — без повторной карточки
        return
    synthetic = dict(reg, **{Registrations.DECISION: Decision.ACCEPT_MERGE, Registrations.REQUEST_TYPE: request_type,
                             Registrations.DECISION_NOTE: ""})
    await _handle_registration(app, rec_id, synthetic, event=True)


async def upgrade_status(app: Application, project_id: str, person_id: str, target: str,
                         who: str | None = None) -> None:
    """Повысить менеджера в проекте без заявки дилера (кнопка «Повысить», /upgrade)."""
    repo: AirtableRepo = app.bot_data["repo"]
    project = await asyncio.to_thread(repo.project_fields, project_id)
    status = UPGRADE_TARGETS[target]
    synthetic = {
        Registrations.DECISION: Decision.ACCEPT_MERGE, Registrations.REQUEST_TYPE: STATUS_EVENTS[status][0],
        Registrations.REG_STATUS: status, Registrations.SUBMITTED_BY: [person_id],
        Registrations.DEALER: await asyncio.to_thread(repo.person_companies, person_id),
        Registrations.TARGET_PROJECT: [project_id],
        Registrations.DEALER_PROJECT_NAME: project.get(Projects.NAME) or "", Registrations.PAYLOAD: "{}",
    }
    async with app.bot_data.setdefault("decisions_lock", asyncio.Lock()):
        await _handle_registration(app, None, synthetic, event=True, who=who)
    # Пошаговая проверка: после предрегистрации — предложить регистрацию
    step = await memo.pop(app.bot_data, "stepwise_next", f"{project_id}:{person_id}")
    if step and target == "pre" and step["final"] == "reg":
        await offer_step(app, routing.resolve(app.bot_data, who or routing.EGOR)[1], project_id, person_id,
                         "reg", step)


async def offer_step(app: Application, admin_chat, project_id: str, person_id: str, step: str, info: dict) -> None:
    """Следующий шаг пошаговой проверки новой заявки — проверяющему, с кнопками «Повысить / Пока оставить»."""
    if not admin_chat:
        return
    repo = app.bot_data["repo"]
    fresh = await asyncio.to_thread(repo.project_fields, project_id)
    if step == "pre" and info["final"] == "reg":
        await memo.set_item(app.bot_data, "stepwise_next", f"{project_id}:{person_id}", info)
    manager = await asyncio.to_thread(repo.person_name, person_id)
    text = admin.step_offer(fresh.get(Projects.PPR_REF) or "", fresh.get(Projects.NAME) or "", step,
                            info["final"], info["payload"], fresh.get(Projects.PPR_NEXT_MISSING) or "", manager)
    await _send_html(app.bot, admin_chat, text, admin.upgrade_markup(project_id, person_id, step))


STATUS_FIELDS = {PPR_REGISTERED: Projects.REGISTERED, PPR_PRE_REGISTERED: Projects.PRE_REGISTERED,
                 PPR_NOTIFIED: Projects.NOTIFIED}
LOWER_STATUS = {PPR_REGISTERED: PPR_PRE_REGISTERED, PPR_PRE_REGISTERED: PPR_NOTIFIED, PPR_NOTIFIED: None}


_ACC_STATUS = {PPR_REGISTERED: "регистрацию", PPR_PRE_REGISTERED: "предрегистрацию", PPR_NOTIFIED: "уведомление"}


def rollback_dealer_text(project: dict, old: str, new: str | None, reason: str, reviewer: str = "Егору") -> str:
    """Менеджеру: статус снижен или снят — причина и как вернуть."""
    head = f"↩️ {cards.project_line(project)}"
    now = PPR_LABELS[new].lower() if new else "статус снят"
    what = (f"Чтобы вернуть {_ACC_STATUS[old]}, нажми «Обновить проект» → «Дополнить материалы» и запроси её снова."
            if new else "Если проект снова актуален — нажми «Новый проект» и подай его заново.")
    return (f"{head}\nСтатус изменён: {PPR_LABELS[old].lower()} → {now}.\n"
            f"Причина: {escape(reason)}\n\n{what} Вопросы — {reviewer} напрямую.")


async def rollback_status(app: Application, project_id: str, person_id: str, mode: str, reason: str,
                          who: str | None = None) -> None:
    """Откат менеджера на этап ниже (mode «down») или снятие статуса («cancel») — по кнопке проверяющего."""
    repo: AirtableRepo = app.bot_data["repo"]
    admin_chat = routing.resolve(app.bot_data, who or routing.EGOR)[1]
    project = await asyncio.to_thread(repo.project_fields, project_id)
    old = _manager_status(project, person_id)
    if not old:
        if admin_chat:
            name = await asyncio.to_thread(repo.person_name, person_id) or "менеджера"
            await _send_html(app.bot, admin_chat, f"🛠 У {escape(name)} в {cards.project_line(project)} уже нет "
                                                  "статуса — откатывать нечего.")
        return
    new = LOWER_STATUS[old] if mode == "down" else None
    updates = {STATUS_FIELDS[old]: [p for p in project.get(STATUS_FIELDS[old]) or [] if p != person_id]}
    if new:
        updates[STATUS_FIELDS[new]] = [p for p in project.get(STATUS_FIELDS[new]) or [] if p != person_id] + [person_id]
        updates[Projects.STATUS_CONFIRMED] = datetime.now(VALENCIA).date().isoformat()  # новый статус — новый срок
    await asyncio.to_thread(repo.update_project, project_id, updates)
    person = await asyncio.to_thread(repo.person_name, person_id) or "менеджер"
    line = (f"{person}: {PPR_LABELS[old].lower()} → {PPR_LABELS[new].lower() if new else 'статус снят'}. "
            f"Причина: {reason}")
    reviewer = routing.resolve(app.bot_data, who or routing.EGOR)[0]
    await asyncio.to_thread(repo.append_project_log, project_id, project.get(Projects.PROJECT_LOG),
                            f"{datetime.now(VALENCIA):%d.%m.%Y} · {routing.NAMES.get(reviewer, 'Егор')} (бот):", [line])
    # Как карточка решения: всем людям компании, автор — первым
    chats = await asyncio.to_thread(repo.chat_ids_for_people, [person_id])
    companies = await asyncio.to_thread(repo.person_companies, person_id)
    for c in await asyncio.to_thread(repo.chat_ids_for_companies, companies):
        if c not in chats:
            chats.append(c)
    text = rollback_dealer_text(project, old, new, reason, DATIVE.get(reviewer, "Егору"))
    for chat in chats:
        await _send_html(app.bot, chat, text)
    if admin_chat:
        await _send_html(app.bot, admin_chat,
                         f"🛠 ↩️ {cards.project_line(project)}\n{escape(line)}\n📨 "
                         + ("Дилеру отправлено." if chats else "⚠️ У дилера нет Telegram в базе — не отправлено."))


async def _route_person(app: Application, person_id: str) -> str:
    """Кто проверяет заявки этого менеджера: Лола (дилеры) или Егор — с учётом, есть ли Лола в боте."""
    repo: AirtableRepo = app.bot_data["repo"]
    companies = await asyncio.to_thread(repo.person_companies, person_id)
    types = await asyncio.to_thread(repo.company_types, companies)
    return routing.resolve(app.bot_data, routing.route(types))[0]


async def _route_reg(app: Application, reg: dict) -> str:
    types = await asyncio.to_thread(app.bot_data["repo"].company_types, reg.get(Registrations.DEALER) or [])
    return routing.resolve(app.bot_data, routing.route(types))[0]


async def _warn_expired(app: Application, reviewer: str, admin_chat) -> None:
    """Истёк срок статуса — каждый день, пока не решено (Р9, Р10): откатить или оставить как есть."""
    repo: AirtableRepo = app.bot_data["repo"]
    rows, lines = [], []
    names: dict = app.bot_data.setdefault("person_names", {})
    for r in await asyncio.to_thread(repo.expired_statuses):
        f = r["fields"]
        # Срок есть только у предрегистрации и регистрации — уведомление не истекает
        for field, status in ((Projects.REGISTERED, PPR_REGISTERED), (Projects.PRE_REGISTERED, PPR_PRE_REGISTERED)):
            for person in f.get(field) or []:
                if await _route_person(app, person) != reviewer:
                    continue
                if person not in names:
                    names[person] = await asyncio.to_thread(repo.person_name, person) or "менеджер без имени"
                label = f"{cards.name_ref((f.get(Projects.NAME) or '')[:30], f.get(Projects.PPR_REF) or '')} · {names[person]}"
                lines.append(f"• {escape(label)} — {PPR_LABELS[status].lower()}, срок истёк "
                             f"{labels.date_ru(f.get(Projects.STATUS_EXPIRY))}")
                rows.append([(f"↩️ Откатить {label}"[:60], f"{admin.CB_ADMIN}{admin.ACTION_RB}:{r['id']}:{person}")])
    if lines:
        rows = rows[:15] + [[("Оставить как есть", f"{admin.CB_ADMIN}{admin.ACTION_UP_NO}:")]]
        await _send_html(app.bot, admin_chat, "🛠 ⏳ <b>Истёк срок статуса.</b> Откати или оставь как есть "
                                              "(список придёт и завтра, пока срок не обновится):\n"
                         + "\n".join(lines[:15]), _markup(rows))


CALL_REMIND_BEFORE = timedelta(hours=1)


async def _calls(app: Application) -> list[tuple[datetime, str, str, str, str]]:
    """Подтверждённые звонки: (начало по Валенсии, кто, проект, строка слота, проверяющий)."""
    repo: AirtableRepo = app.bot_data["repo"]
    confirmed: dict = await memo.get(app.bot_data, "calls_confirmed", {})
    out = []
    for rec in await asyncio.to_thread(repo.recent_calls):
        f = rec["fields"]
        payload = admin._payload(f)
        people = f.get(Registrations.SUBMITTED_BY) or []
        who = await asyncio.to_thread(repo.person_name, people[0]) if people else "Дилер"
        reviewer = await _route_person(app, people[0]) if people else routing.EGOR
        for idx, (label, value) in enumerate(admin.call_slots(payload)):
            agreed = confirmed.get(f"{rec['id']}:{idx}")
            if agreed is None:
                continue   # неподтверждённые звонки не напоминаем (Р7)
            start = slots.valencia_start(agreed or value)
            if start:
                out.append((start, who, f.get(Registrations.DEALER_PROJECT_NAME) or "без названия",
                            f"{label}: {agreed or value}", reviewer))
    return sorted(out)


async def _remind_calls(app: Application, reviewer: str, admin_chat, morning: bool) -> None:
    """Утром — подтверждённые звонки на сегодня; в течение дня — за час до начала. Звонки Лолы — Лоле."""
    now = datetime.now(VALENCIA)
    reminded: list = await memo.get(app.bot_data, "calls_reminded", [])
    calls = [c for c in await _calls(app) if c[4] == reviewer]
    if morning:
        today = [c for c in calls if c[0].date() == now.date()]
        if today:
            await _send_html(app.bot, admin_chat, "📞 <b>Звонки сегодня</b>\n" + "\n".join(
                f"• {c[0]:%H:%M} по Валенсии · {escape(c[1])} · {escape(c[2])}\n  {escape(c[3])}" for c in today))
        return
    for start, who, name, line, _ in calls:
        key = f"{start.isoformat()}|{name}"
        if key in reminded or not (now < start <= now + CALL_REMIND_BEFORE):
            continue
        reminded.append(key)
        await memo.put(app.bot_data, "calls_reminded", reminded[-200:])
        await _send_html(app.bot, admin_chat, f"⏰ <b>Через {int((start - now).total_seconds() // 60)} мин — звонок</b>\n"
                                              f"{escape(who)} · {escape(name)}\n{escape(line)}")


READY_REPEAT_DAYS = 7   # «условия выполнены» по одному менеджеру — не чаще раза в неделю


async def _warn_ready(app: Application, reviewer: str, admin_chat, force: bool = False) -> int:
    """Утром: проекты, где для следующего этапа уже всё есть (в т.ч. после правок через Буфер), —
    предложить повысить, не дожидаясь заявки партнёра."""
    repo: AirtableRepo = app.bot_data["repo"]
    offered: dict = await memo.get(app.bot_data, "ready_offered", {})
    today = datetime.now(VALENCIA).date()
    changed = False
    for rec in await asyncio.to_thread(repo.ready_for_upgrade):
        f = rec["fields"]
        for person in dict.fromkeys((f.get(Projects.PRE_REGISTERED) or []) + (f.get(Projects.NOTIFIED) or [])):
            key = f"{rec['id']}:{person}"
            last = offered.get(key)
            if last and not force and (today - date.fromisoformat(last)).days < READY_REPEAT_DAYS:
                continue
            if await _route_person(app, person) != reviewer:
                continue
            await _offer_upgrade(app, admin_chat, f, rec["id"], person)
            offered[key] = today.isoformat()
            changed = True
    if changed:
        await memo.put(app.bot_data, "ready_offered", offered)
    return sum(1 for v in offered.values() if v == today.isoformat()) if changed else 0


async def cmd_ready(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/ready — прислать сейчас все предложения «Условия выполнены» по моим дилерам (без ожидания 9:00)."""
    if not admin.is_admin(update, ctx):
        return
    who = routing.who_by_chat(ctx.bot_data, update.effective_chat.id) or routing.EGOR
    if not await _warn_ready(ctx.application, who, update.effective_chat.id, force=True):
        await update.effective_chat.send_message("🛠 Сейчас нет проектов, где условия следующего этапа выполнены.")


async def _offer_upgrade(app: Application, admin_chat, fresh: dict, project_id: str, person_id: str) -> None:
    """После «Дополнить»: условия следующего этапа выполнены — предложить проверяющему повысить."""
    if not admin_chat or fresh.get(Projects.PPR_NEXT_MISSING) or fresh.get(Projects.REGISTERED) \
            or fresh.get(Projects.PPR_TRACK) == SIMPLIFIED:
        return
    status = _manager_status(fresh, person_id)
    target = {PPR_NOTIFIED: "pre", PPR_PRE_REGISTERED: "reg"}.get(status)
    if not target:
        return
    what = "предрегистрации" if target == "pre" else "регистрации"
    manager = await asyncio.to_thread(app.bot_data["repo"].person_name, person_id) or "менеджер без имени"
    text = (f"✔️ <b>Условия для {what} выполнены</b> · {cards.project_line(fresh)}\n"
            f"{escape(manager)} — сейчас {PPR_LABELS[status].lower()}. Повысить? «Пока оставить» и «Отказать» — "
            "дилеру уйдёт сообщение, сначала спрошу комментарий.")
    await _send_html(app.bot, admin_chat, text, admin.upgrade_markup(project_id, person_id, target))


async def _handle_registration(app: Application, rec_id: str | None, reg: dict, event: bool = False,
                               who: str | None = None) -> None:
    """Решение по заявке. event — смена Reg Status или повышение без заявки (rec_id None):
    Decision и Bot Processed не трогаем, отмечаем только Bot Reg Status."""
    repo: AirtableRepo = app.bot_data["repo"]
    created_projects: set = app.bot_data.setdefault("created_projects", set())
    decided_by: dict = app.bot_data.setdefault("decided_by", {})
    # Полный итог — тому, кто решал (по кнопке), иначе по маршруту; Егору при решении Лолы — строка
    routed = routing.resolve(app.bot_data, routing.route(
        await asyncio.to_thread(repo.company_types, reg.get(Registrations.DEALER) or [])))[0]
    decider, admin_chat = routing.resolve(app.bot_data, who or decided_by.get(rec_id) or routed)
    target = reg.get(Registrations.TARGET_PROJECT) or []
    project = await asyncio.to_thread(repo.project_fields, target[0]) if target else None
    pl = decision_plan(reg, project)
    if not pl.registration_updates:
        return
    if event:
        # Событие не закрывает заявку и не меняет её Decision: только «увидел этот Reg Status»
        for k in (Registrations.BOT_PROCESSED, Registrations.REG_STATUS):
            pl.registration_updates.pop(k, None)
        if pl.admin_text and not rec_id:
            # Повышение без заявки: запросить у дилера нечем — заявки нет
            pl.admin_text = (pl.admin_text.replace("🛠 ⚠️ Решение по", "🛠 ⚠️ Повышение по")
                             + "\nКогда данные появятся в проекте — повысь снова через /upgrade.")
            pl.block_kind = ""
    final_status = pl.registration_updates.get(Registrations.REG_STATUS, reg.get(Registrations.REG_STATUS))
    if final_status:
        pl.registration_updates[Registrations.BOT_REG_STATUS] = final_status
    dealers = reg.get(Registrations.DEALER) or []
    dealer_name = ", ".join([await asyncio.to_thread(repo.company_name, c) for c in dealers]) or "дилер"
    company_notes: list[str] = []
    new_specifier = None
    if pl.project_updates or pl.links:
        updates = {
            k: (datetime.now(VALENCIA).date().isoformat() if v == "__today__" else v)
            for k, v in pl.project_updates.items()
        }
        # Связи с Companies: найти или создать по названию из заявки
        country = (project or {}).get(Projects.COUNTRY) or updates.get(Projects.COUNTRY)
        for link in pl.links:
            if link.get("company_id"):
                cid, created = link["company_id"], False
            else:
                cid, created = await asyncio.to_thread(
                    repo.find_or_create_company, link["name"], link["type"], country, bool(link.get("new")))
            if link.get("append"):
                updates[link["field"]] = list((project or {}).get(link["field"]) or []) + [cid]
            else:
                updates[link["field"]] = [cid]
            cname = await asyncio.to_thread(repo.company_name, cid)
            role = "проект ведёт сам" if link["field"] == Projects.SPECIFIER and cid in dealers \
                else LINK_ROLE[link["field"]]
            company_notes.append(f"{cname} ({role}{', новая' if created else ''})")
        new_specifier = updates.get(Projects.SPECIFIER)
        await asyncio.to_thread(repo.update_project, target[0], updates)
    # ГАП — человек бюро в People и участник проекта
    if target and not pl.blocked and ((reg.get(Registrations.DECISION) or "").startswith("Accept")
                                      or pl.declined_upgrade):
        note = await _link_architect(repo, reg, project or {}, target[0], new_specifier, dealers)
        if note:
            company_notes.append(note)
    if pl.log_lines and target:
        header = f"{datetime.now(VALENCIA):%d.%m.%Y} · {dealer_name} — ответы из заявки партнёра:"
        await asyncio.to_thread(repo.append_project_log, target[0],
                                (project or {}).get(Projects.PROJECT_LOG), header, pl.log_lines)
    # Карточка дилеру — по свежему состоянию проекта после записи
    accepted = ((reg.get(Registrations.DECISION) or "").startswith("Accept") or pl.declined_upgrade) \
        and target and not pl.blocked
    card = None
    status_person = ((reg.get(Registrations.SUBMITTED_BY) or [""])[0]) if accepted else ""
    if accepted:
        fresh = await asyncio.to_thread(repo.project_fields, target[0])
        # Статус на проекте — у менеджера, отправившего заявку; в тексте — компания
        managers = reg.get(Registrations.SUBMITTED_BY) or []
        dealer_id = managers[0] if managers else ""
        before = set((project or {}).get(Projects.NOTIFIED) or []) | set(
            (project or {}).get(Projects.PRE_REGISTERED) or []) | set((project or {}).get(Projects.REGISTERED) or [])
        note = (reg.get(Registrations.DECISION_NOTE) or "").strip()
        if pl.declined_upgrade:
            asked = "регистрация" if "Registration" in (reg.get(Registrations.REQUEST_TYPE) or "") \
                and "Pre" not in (reg.get(Registrations.REQUEST_TYPE) or "") else "предрегистрация"
            card = cards.declined_upgrade_card(fresh, dealer_id, dealer_name, payload_of(reg), asked,
                                               note, dealer_id not in before)
        elif pl.status_label:
            # Статус поставили без заявки дилера (руками или /upgrade) — не «Заявка принята»
            title = "✅ Статус по проекту изменён" if event else "✅ Заявка принята"
            card = cards.accept_card(fresh, dealer_id, dealer_name, payload_of(reg), dealer_id not in before, note,
                                     title)
        elif not pl.final_text:
            rtype = reg.get(Registrations.REQUEST_TYPE)
            quote_no = payload_of(reg).get("Quotation / Proforma №")
            valid = labels.date_ru(reg.get(Registrations.QUOTE_VALID))
            entered = ", ".join(dict.fromkeys(pl.transferred))
            body = {
                RequestType.QUOTE_SIMPLIFIED: (
                    f"🧾 Квотация № {escape(str(quote_no))} зафиксирована — защита действует "
                    + (f"до {valid}." if valid else "на срок действия квотации.")) if quote_no else None,
                RequestType.UPDATE_NEWS: "📰 Новость записали в историю проекта.",
            }.get(rtype) or (f"📝 Внесли в проект: {escape(entered)}." if entered
                             else "📝 Информацию записали в историю проекта.")
            card = cards.update_card(fresh, dealer_id, dealer_name, body, note,
                                     next_step=rtype in RequestType.INFO_UPDATES)
        if pl.dealer_text and not pl.final_text:  # ответ на запрос принимается молча — у него dealer_text нет
            pl.dealer_text = card
        elif not pl.dealer_text and payload_of(reg).get("In Reply To") and not event:
            # Ответ на запрос принят вместе с исходной заявкой — подтверждение дилеру и проверяющему
            pl.dealer_text = (f"Твой ответ по проекту {cards.project_line(fresh)} получили и добавили в проект. "
                              "Делать ничего не нужно.")
            card = None   # проверяющему — короткий итог с этим текстом
    # Финальное решение закрывает заявку: Processed + время (запрос информации — ещё нет)
    mark = str(pl.registration_updates.get(Registrations.BOT_PROCESSED) or "")
    if not event and mark.startswith("done:") and mark != f"done:{Decision.NEED_INFO}":
        pl.registration_updates[Registrations.PROCESSED] = True
        pl.registration_updates[Registrations.PROCESSED_AT] = datetime.now(timezone.utc).isoformat()
    # Отмечаем до рассылки — чтобы сбой отправки не повторил изменения
    if rec_id:
        await asyncio.to_thread(repo.update_registration, rec_id, pl.registration_updates)
    log.info("Решение по %s (событие: %s): %s", rec_id, event, pl.registration_updates.get(Registrations.BOT_PROCESSED))

    # Карточку решения видят все люди компании дилера, автор — первым
    dealer_chats = await asyncio.to_thread(repo.chat_ids_for_people, reg.get(Registrations.SUBMITTED_BY) or [])
    for chat in await asyncio.to_thread(repo.chat_ids_for_companies, dealers):
        if chat not in dealer_chats:
            dealer_chats.append(chat)
    if pl.dealer_text:
        markup = _markup([[("Ответить", f"{CB_REPLY}{rec_id}")]]) if pl.reply_button and rec_id else None
        for chat in dealer_chats:
            await _send_html(app.bot, chat, pl.dealer_text, markup)
    broadcast_names: list[str] = []
    if pl.broadcast_people and pl.broadcast_text:
        open_btn = _markup([[("Открыть проект", f"{CB_PICK}{MODE_MY}:{target[0]}")]]) if target else None
        for person_id in pl.broadcast_people:
            for chat in await asyncio.to_thread(repo.chat_ids_for_people, [person_id]):
                if chat not in dealer_chats:
                    await _send_html(app.bot, chat, pl.broadcast_text, open_btn)
            broadcast_names.append(await asyncio.to_thread(repo.person_name, person_id) or "менеджер без имени")
    if admin_chat:
        if pl.admin_text:
            markup = None
            if rec_id and pl.block_kind:
                requested = reg.get(Registrations.REG_STATUS) if reg.get(Registrations.REG_STATUS) in STATUS_EVENTS \
                    else {RequestType.NEW_REGISTRATION: RegStatus.REGISTRATION,
                          RequestType.UPGRADE_REGISTRATION: RegStatus.REGISTRATION,
                          RequestType.NEW_PRE_REGISTRATION: RegStatus.PRE_REGISTRATION,
                          RequestType.UPGRADE_PRE_REGISTRATION: RegStatus.PRE_REGISTRATION}.get(
                        reg.get(Registrations.REQUEST_TYPE))
                markup = admin.blocked_markup(rec_id, pl.block_kind, reg, await admin.merge_candidates(repo, {
                    **reg, Registrations.REQUEST_TYPE: "New"}) if pl.block_kind == "no_project" else None, requested)
            text = admin.blocked_text(pl.admin_text, pl.ask_text) if markup else pl.admin_text
            await admin.send_card(app.bot, admin_chat, text, markup)
        elif card and pl.dealer_text:
            created = target[0] in created_projects
            created_projects.discard(target[0])
            extra = (["🆕 Проект создан"] if created else []) + [escape(c) for c in pl.changes] + (
                ["🏢 Компании: " + escape(", ".join(company_notes))] if company_notes else []) + (
                ["🔎 Разослано другим дилерам на объекте: " + escape(", ".join(broadcast_names))]
                if broadcast_names else [])
            rb = admin.rollback_markup(target[0], status_person) if pl.status_label and status_person else None
            await admin.send_card(app.bot, admin_chat, cards.admin_copy(card, bool(dealer_chats), extra), rb)
        elif pl.dealer_text and not pl.blocked:
            await _send_html(app.bot, admin_chat, _admin_summary(
                reg, pl, pl.project_name or "", False, dealer_name, bool(dealer_chats), [], None))
    # «Дополнить» закрыл пропуски — предложить проверяющему следующий этап (после копии карточки)
    if accepted and not event and reg.get(Registrations.REQUEST_TYPE) in RequestType.INFO_UPDATES and status_person:
        await _offer_upgrade(app, admin_chat, fresh, target[0], status_person)
    step = await memo.pop(app.bot_data, "stepwise", rec_id) if rec_id else None
    if step and accepted and status_person and not pl.blocked:
        await offer_step(app, admin_chat, target[0], status_person, "pre", step)
    if not pl.blocked:
        decided_by.pop(rec_id, None)
        other = routing.EGOR if decider != routing.EGOR else (routing.LOLA if routed == routing.LOLA else None)
        other_chat = routing.chats(app.bot_data).get(other) if other else None
        if other_chat and other_chat != admin_chat:
            fresh_ref = ((await asyncio.to_thread(repo.project_fields, target[0])) if target else {}) or {}
            await _send_html(app.bot, other_chat, routing.summary_line(
                decider, reg.get(Registrations.DECISION) or "", fresh_ref.get(Projects.PPR_REF) or "",
                fresh_ref.get(Projects.NAME) or reg.get(Registrations.DEALER_PROJECT_NAME) or "",
                pl.status_label or "", pl.declined_upgrade))


# ---------- напоминания ----------

REMINDERS_INTERVAL = 600  # секунд между проверками


async def _build_digest(repo: AirtableRepo, person_id: str, today: date, name: str = ""):
    """(текст, кнопки, пункты) напоминания по проектам менеджера; None — напоминать нечего."""
    projects = _sorted(await asyncio.to_thread(repo.dealer_projects, person_id))
    if not projects:
        return None
    news = await asyncio.to_thread(repo.last_news_by_project, person_id)
    quotes = await asyncio.to_thread(repo.dealer_quotes, person_id)
    items = [
        it for p in projects
        if (it := reminders.project_item(
            p, today, date.fromisoformat(news[p.record_id]) if p.record_id in news else None,
            (quotes.get(p.record_id) or {}).get("valid")))
    ]
    if not items:
        return None
    buttons = [[(f"Открыть: {it.name[:40]}", f"{CB_PICK}{MODE_MY}:{it.project_id}")] for it in items[:PAGE_SIZE]]
    # Без «Ола»: «Marina, напоминание по твоим плейсментам»
    first = phrases.first_name(name)
    intro = f"{first}, напоминание по твоим плейсментам" if first else "Напоминание по твоим плейсментам"
    return reminders.digest_text(items, intro), buttons, items


async def cmd_reminder(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Админ: /reminder — своё напоминание сейчас; /reminder username — что получил бы человек."""
    if not admin.is_admin(update, ctx):
        return
    await preview_reminder(update, ctx, ctx.args[0] if ctx.args else None)


async def preview_reminder(update: Update, ctx: ContextTypes.DEFAULT_TYPE, username: str | None) -> None:
    repo = _repo(ctx)
    if username:
        person = await asyncio.to_thread(repo.find_by_username, username)
        if not person:
            await update.effective_chat.send_message(
                f"🛠 Человека с Telegram @{escape(username.lstrip('@'))} в базе нет — проверь написание.")
            return
        header = f"🛠 Так выглядело бы напоминание для {escape(person.name)} (не отправлено):\n\n"
    else:
        person = await _identify(update, ctx)
        header = ""
    if not person:
        await update.effective_chat.send_message("🛠 Тебя самого нет в базе людей — своего напоминания у тебя нет. "
                                                 "Посмотреть чужое — /reminder username.")
        return
    digest = await _build_digest(repo, person.record_id, datetime.now(VALENCIA).date(), person.name)
    if not digest:
        await update.effective_chat.send_message(f"🛠 {'Этому человеку' if username else 'Тебе'} напоминать нечего: "
                                                 "сроки не подходят, данных хватает, новости свежие.")
        return
    text, buttons, _ = digest
    # В чужом предпросмотре кнопок нет: проекты дилера админ из его карточки не откроет
    markup = None if username else _markup(buttons)
    await update.effective_chat.send_message(header + text, parse_mode=ParseMode.HTML, reply_markup=markup)


# ---------- админское меню ----------

CB_ADMIN_MENU = "am:"


def _admin_menu() -> engine.Screen:
    return engine.Screen("🛠 <b>Админ</b> — что посмотреть?", [
        [("📥 Заявки на проверке", f"{CB_ADMIN_MENU}inbox")],
        [("🔔 Моё напоминание", f"{CB_ADMIN_MENU}remind"), ("👀 Напоминание дилера", f"{CB_ADMIN_MENU}remind_user")],
        [("📊 Сводка", f"{CB_ADMIN_MENU}stats")],
        [("👁 Просмотр сообщений бота", f"{CB_ADMIN_MENU}preview")],
    ])


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin.is_admin(update, ctx):
        return  # для всех остальных команды нет
    await _send(update, [_admin_menu()])


async def _admin_menu_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    if not admin.is_admin(update, ctx):
        return
    chat = update.effective_chat
    if action == "inbox":
        await admin.cmd_inbox(update, ctx)
    elif action == "remind":
        await preview_reminder(update, ctx, None)
    elif action == "remind_user":
        admin._pending(ctx)[chat.id] = (admin.ACTION_REMIND_USER, "", "")
        await chat.send_message("🛠 Напиши Telegram дилера без @ — например, ivan_petrov. "
                                "Пришлю, какое напоминание он получил бы сейчас.",
                                reply_markup=InlineKeyboardMarkup([admin.cancel_row()]))
    elif action == "blocked":
        await _resend_blocked(ctx)
    elif action.startswith("preview"):
        scenario = action.partition(":")[2]
        await preview(update, ctx, scenario)
    elif action == "stats":
        st = await asyncio.to_thread(_repo(ctx).admin_stats)
        lines = ["🛠 <b>Сводка</b>",
                 f"Без решения: {st['no_decision']}",
                 f"Ждём ответа дилера на твой запрос: {st['need_info']}"]
        if st["blocked"]:
            lines.append("Решения не прошли: " + escape(", ".join(st["blocked"])))
        lines.append(f"\nПодключено людей: {len(st['people'])}")
        for name, slot, last in st["people"]:
            lines.append(f"• {escape(name)} — напоминания {slot or 'время не выбрано'}, последнее {_date(last) or '—'}")
        markup = _markup([[("Показать решения, которые не прошли", f"{CB_ADMIN_MENU}blocked")]]) if st["blocked"] else None
        await admin.send_card(ctx.bot, chat.id, "\n".join(lines), markup)


async def cmd_preview(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/preview [сценарий] — все сообщения сценария с тестовыми данными (только проверяющему)."""
    if not admin.is_admin(update, ctx):
        return
    await preview(update, ctx, ctx.args[0] if ctx.args else "")


async def preview(update: Update, ctx: ContextTypes.DEFAULT_TYPE, scenario: str) -> None:
    await preview_mod.run(ctx.bot, update.effective_chat.id, scenario.strip().lower())


async def _resend_blocked(ctx) -> None:
    """«Показать» в сводке: застрявшие решения приходят снова — с причиной и кнопками (Р5)."""
    repo = _repo(ctx)
    for rec in await asyncio.to_thread(repo.blocked_registrations):
        await asyncio.to_thread(repo.update_registration, rec["id"], {Registrations.BOT_PROCESSED: ""})
    await process_decisions(ctx.application)


async def process_reminders(app: Application) -> None:
    repo: AirtableRepo = app.bot_data["repo"]
    checked: dict = app.bot_data.setdefault("reminders_checked", {})  # кого уже проверили сегодня
    for rec in await asyncio.to_thread(repo.people_for_reminders):
        f = rec["fields"]
        now = reminders.person_now(f.get(People.COUNTRY))
        today = now.date()
        if checked.get(rec["id"]) == today or not reminders.in_slot(now, f.get(People.REMINDER_SLOT)):
            continue
        checked[rec["id"]] = today
        if not reminders.due(f.get(People.LAST_REMINDER), today):
            continue
        digest = await _build_digest(repo, rec["id"], today, f.get(People.NAME, ""))
        if not digest:
            continue
        text, buttons, items = digest
        try:
            await app.bot.send_message(f[People.TELEGRAM_CHAT_ID], text,
                                       parse_mode=ParseMode.HTML, reply_markup=_markup(buttons))
        except Exception:
            log.exception("Не удалось отправить напоминание %s", rec["id"])
            continue
        await asyncio.to_thread(repo.set_last_reminder, rec["id"], today.isoformat())
        log.info("Напоминание %s: %d проектов", rec["id"], len(items))


ABANDONED_HOURS = 24
ABANDON_DAY = (9, 20)  # пишем только днём по местному времени человека


async def _check_abandoned(app: Application) -> None:
    """Начатая и не отправленная заявка: через 24 часа одно напоминание «Продолжить / Удалить»."""
    repo: AirtableRepo = app.bot_data["repo"]
    for person, country in await asyncio.to_thread(repo.people_with_dialogs):
        st = person.state
        if not st or st.nudged or not _has_answers(st):
            continue
        started = datetime.fromisoformat(st.started_at)
        if datetime.now(timezone.utc) - started < timedelta(hours=ABANDONED_HOURS):
            continue
        if not ABANDON_DAY[0] <= reminders.person_now(country).hour < ABANDON_DAY[1]:
            continue
        st.nudged = True
        await asyncio.to_thread(repo.save_state, person.record_id, st)
        await _send_html(app.bot, person.chat_id,
                         f"📝 У тебя незаконченная заявка: {escape(_dialog_name(st))}. Она ещё не отправлена. Продолжим?",
                         _markup([[("Продолжить", CB_MENU_RESUME)], [("Удалить", CB_DROP_AND_GO)]]))


REVIEW_REMIND_HOURS = 48   # напоминание проверяющему
DEALER_LATE_HOURS = 72     # обещанный дилеру срок ответа
LATE_TEXT = ("⏳ Заявка «{name}» на проверке дольше обычного — ответ задерживается. "
             "{who} в курсе, ответ придёт сюда. Делать ничего не нужно.")


async def _check_overdue(app: Application) -> None:
    """Заявки без решения: 48 ч — снова карточка проверяющему, 72 ч — дилеру о задержке. По разу."""
    repo: AirtableRepo = app.bot_data["repo"]
    ctx = SimpleNamespace(bot_data=app.bot_data, bot=app.bot, application=app)
    for rec in await asyncio.to_thread(repo.undecided):
        f = rec["fields"]
        hours = admin.age_hours(f.get(Registrations.SUBMITTED_AT))
        mark = f.get(Registrations.BOT_PROCESSED) or ""
        name = f.get(Registrations.DEALER_PROJECT_NAME) or "без названия"
        if hours >= DEALER_LATE_HOURS and mark != "late:72":
            who = routing.NAMES.get(await _route_reg(app, f), "Егор")
            for chat in await asyncio.to_thread(repo.chat_ids_for_people, f.get(Registrations.SUBMITTED_BY) or []):
                await _send_html(app.bot, chat, LATE_TEXT.format(name=escape(name), who=who))
            await asyncio.to_thread(repo.update_registration, rec["id"], {Registrations.BOT_PROCESSED: "late:72"})
        elif hours >= REVIEW_REMIND_HOURS and not mark:
            types = await asyncio.to_thread(repo.company_types, f.get(Registrations.DEALER) or [])
            _, chat = routing.resolve(app.bot_data, routing.route(types))
            if chat:
                try:
                    await admin.send_request(ctx, chat, rec["id"], f, "⏰ <b>Ждёт решения больше 48 часов</b>\n\n")
                except Exception:
                    log.warning("Не отправилась карточка 48 ч для %s", rec["id"], exc_info=True)
            await asyncio.to_thread(repo.update_registration, rec["id"], {Registrations.BOT_PROCESSED: "late:48"})


async def _reminders_loop(app: Application) -> None:
    await asyncio.sleep(30)  # дать боту подняться
    while True:
        try:
            await process_reminders(app)
        except Exception:
            log.exception("Ошибка напоминаний")
        try:
            await _check_overdue(app)
        except Exception:
            log.exception("Ошибка проверки сроков")
        try:
            await _check_abandoned(app)
        except Exception:
            log.exception("Ошибка проверки брошенных заявок")
        await asyncio.sleep(REMINDERS_INTERVAL)


async def _refresh_reviewers(app: Application) -> None:
    """Chat ID Лолы (@aw_eeu) из People: появляется, когда она впервые пишет боту."""
    person = await asyncio.to_thread(app.bot_data["repo"].find_by_username, routing.LOLA_USERNAME)
    chat = person.chat_id if person and person.chat_id else None
    if chat and chat != app.bot_data.get("lola_chat_id"):
        try:
            await app.bot.set_my_commands(ADMIN_COMMANDS + DEALER_COMMANDS, scope=BotCommandScopeChat(int(chat)))
        except Exception:
            log.exception("Не удалось задать команды Лоле")
    app.bot_data["lola_chat_id"] = chat


ALERTS_INTERVAL = 60


async def _alerts_loop(app: Application) -> None:
    """Ошибки — проверяющему пачкой раз в минуту; в 9 утра — сводка (Р12: Лоле — по её дилерам)."""
    handler: alerts.AlertHandler = app.bot_data["alerts"]
    while True:
        await asyncio.sleep(ALERTS_INTERVAL)
        reviewers = routing.chats(app.bot_data)
        if not reviewers:
            continue
        try:
            for who, text in handler.drain().items():
                chat = reviewers.get(who) or reviewers.get(routing.EGOR)
                if chat:
                    await _send_html(app.bot, chat, text)
            now = datetime.now(VALENCIA)
            if alerts.summary_due(now, app.bot_data.get("summary_day")):
                app.bot_data["summary_day"] = now.date().isoformat()
                for who, chat in reviewers.items():
                    await _morning(app, who, chat, handler, now)
                handler.day_count = 0
                handler.survived_count = 0
            last = app.bot_data.get("calls_checked")
            if not last or now - last >= timedelta(minutes=10):
                app.bot_data["calls_checked"] = now
                for who, chat in reviewers.items():
                    await _remind_calls(app, who, chat, morning=False)
        except Exception:
            # Не log.exception — иначе ошибка отправки сама станет «ошибкой бота» по кругу
            log.warning("Не удалось отправить оповещение проверяющему", exc_info=True)


async def _morning(app: Application, who: str, chat, handler, now: datetime) -> None:
    """Утренняя сводка одного проверяющего: его заявки, его истёкшие сроки, звонки, «условия выполнены»."""
    repo: AirtableRepo = app.bot_data["repo"]
    pending = [r for r in await asyncio.to_thread(repo.pending_without_decision, 100)
               if await _route_reg(app, r["fields"]) == who]
    blocked = [r for r in await asyncio.to_thread(repo.blocked_registrations)
               if await _route_reg(app, r["fields"]) == who]
    stats = {
        "no_decision": sum(1 for r in pending if not r["fields"].get(Registrations.DECISION)),
        "need_info": sum(1 for r in pending if r["fields"].get(Registrations.DECISION)),
        "blocked": [r["fields"].get(Registrations.DEALER_PROJECT_NAME) or "без названия" for r in blocked],
    }
    overdue = sum(1 for r in pending if not r["fields"].get(Registrations.DECISION)
                  and admin.age_hours(r["fields"].get(Registrations.SUBMITTED_AT)) >= DEALER_LATE_HOURS)
    egor = who == routing.EGOR
    unknown = len(await asyncio.to_thread(repo.open_unknown_users)) if egor else 0   # Р11: новые люди — Егору
    text = alerts.summary_text(now, handler.day_count if egor else 0, stats, overdue, unknown,
                               handler.survived_count if egor else 0)
    markup = _markup([[("Показать решения, которые не прошли", f"{CB_ADMIN_MENU}blocked")]]) if blocked else None
    await _send_html(app.bot, chat, text, markup)
    await _warn_expired(app, who, chat)
    await _warn_ready(app, who, chat)
    await _remind_calls(app, who, chat, morning=True)


async def _decisions_loop(app: Application) -> None:
    while True:
        try:
            await _refresh_reviewers(app)
        except Exception:
            log.exception("Не удалось обновить проверяющих")
        try:
            await _check_unknown_users(app)
        except Exception:
            log.exception("Ошибка проверки новых людей")
        try:
            await process_decisions(app)
        except Exception:
            log.exception("Ошибка обработки решений")
        await asyncio.sleep(DECISIONS_INTERVAL)


DEALER_COMMANDS = [BotCommand("start", "Начать"), BotCommand("new", "Новый проект"),
                   BotCommand("cancel", "Отменить заявку"), BotCommand("time", "Время напоминаний")]
ADMIN_COMMANDS = [BotCommand("admin", "Админ-меню"), BotCommand("inbox", "Заявки на проверке"),
                  BotCommand("reminder", "Напоминание сейчас"), BotCommand("upgrade", "Повысить или откатить статус"),
                  BotCommand("ready", "Готовы к повышению — прислать сейчас"),
                  BotCommand("preview", "Как выглядят сообщения бота")]


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # Сеть и Conflict при выкладке (старая и новая версии пару секунд опрашивают Telegram вместе) —
    # не ошибки бота: в лог предупреждением, Егору не шлём
    if isinstance(ctx.error, (Conflict, NetworkError)):
        log.warning("Telegram: %s", ctx.error)
        return
    who = ""
    if isinstance(update, Update) and update.effective_user:
        who = update.effective_user.full_name or ""
    log.error("Необработанная ошибка", exc_info=ctx.error, extra={
        "alert": f"У {who or 'пользователя'} сбой в диалоге — ему сказал нажать /start и продолжить. "
                 "Если повторится — передай Claude, подробности в логах Railway."})
    if isinstance(update, Update) and update.effective_chat:
        try:
            await update.effective_chat.send_message(
                "Что-то пошло не так на моей стороне. Ответы, которые ты уже дал, сохранены: нажми /start — "
                "я предложу продолжить. Начать заново — /cancel.", reply_markup=MENU_KEYBOARD)
        except Exception:
            pass


# ---------- запуск ----------

def build_app() -> Application:
    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    transcriber = Transcriber(settings.groq_api_key, settings.groq_model)

    async def post_init(app: Application) -> None:
        # Подсказки «/»: дилерам — только свои команды, админу — ещё и админские
        try:
            await app.bot.set_my_commands(DEALER_COMMANDS, scope=BotCommandScopeDefault())
            if settings.admin_chat_id:
                await app.bot.set_my_commands(ADMIN_COMMANDS + DEALER_COMMANDS,
                                              scope=BotCommandScopeChat(int(settings.admin_chat_id)))
        except Exception:
            log.exception("Не удалось задать список команд")
        app.bot_data["decisions_task"] = asyncio.create_task(_decisions_loop(app))
        app.bot_data["alerts_task"] = asyncio.create_task(_alerts_loop(app))
        dropbox = None
        if settings.dropbox_refresh_token and settings.dropbox_app_key and settings.dropbox_app_secret:
            dropbox = DropboxStore(settings.dropbox_app_key, settings.dropbox_app_secret,
                                   settings.dropbox_refresh_token)
            app.bot_data["dropbox"] = dropbox
            inbox = RecordsInbox(dropbox, transcriber, app.bot_data["repo"])
            app.bot_data["records_task"] = asyncio.create_task(inbox.run())
        if settings.collector_token:
            collector = TelegramCollector(settings.collector_token, app.bot_data["repo"], transcriber, dropbox,
                                          settings.collector_owner_id)
            app.bot_data["collector"] = collector
            app.bot_data["collector_task"] = asyncio.create_task(collector.run())
        if settings.spanish_token and dropbox:
            spanish = SpanishCards(settings.spanish_token, dropbox, settings.collector_owner_id, settings.groq_api_key,
                                   settings.spanish_llm_model)
            app.bot_data["spanish"] = spanish
            app.bot_data["spanish_task"] = asyncio.create_task(spanish.run())
        app.bot_data["reminders_task"] = asyncio.create_task(_reminders_loop(app))

    async def post_shutdown(app: Application) -> None:
        for key in ("decisions_task", "reminders_task", "alerts_task", "collector_task", "records_task",
                    "spanish_task"):
            task = app.bot_data.get(key)
            if task:
                task.cancel()
        for key in ("collector", "spanish"):
            if app.bot_data.get(key):
                await app.bot_data[key].aclose()
        if app.bot_data.get("dropbox"):
            await app.bot_data["dropbox"].aclose()
        await transcriber.aclose()

    app = (
        ApplicationBuilder().token(settings.telegram_token)
        # Ссылки дилеров (материалы) не разворачиваем в превью — сводка остаётся читаемой
        .defaults(Defaults(link_preview_options=LinkPreviewOptions(is_disabled=True)))
        .post_init(post_init).post_shutdown(post_shutdown).build()
    )
    app.bot_data["repo"] = AirtableRepo(settings.airtable_token, settings.airtable_base_id)
    app.bot_data["transcriber"] = transcriber
    app.bot_data["admin_chat_id"] = settings.admin_chat_id
    alert_handler = alerts.AlertHandler()
    logging.getLogger().addHandler(alert_handler)
    app.bot_data["alerts"] = alert_handler
    now = datetime.now(VALENCIA)
    if now.hour == alerts.SUMMARY_HOUR:
        # Запуск в час сводки (выкладка) — сегодняшняя, скорее всего, уже ушла: второй раз не шлём
        app.bot_data["summary_day"] = now.date().isoformat()
    app.bot_data["base_id"] = settings.airtable_base_id
    app.bot_data["process_decisions"] = process_decisions
    app.bot_data["upgrade_status"] = upgrade_status
    app.bot_data["rollback_status"] = rollback_status
    app.bot_data["preview_reminder"] = preview_reminder

    app.add_handler(TypeHandler(Update, log_update), group=-1)
    # Только новые сообщения: правку уже отправленного ответа бот не обрабатывает
    # (у edited_message нет update.message — раньше падало с AttributeError)
    private = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE
    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    app.add_handler(CommandHandler("new", cmd_new, filters=private))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=private))
    app.add_handler(CommandHandler("time", cmd_time, filters=private))
    app.add_handler(CommandHandler("inbox", admin.cmd_inbox, filters=private))
    app.add_handler(CommandHandler("reminder", cmd_reminder, filters=private))
    app.add_handler(CommandHandler("admin", cmd_admin, filters=private))
    app.add_handler(CommandHandler("upgrade", admin.cmd_upgrade, filters=private))
    app.add_handler(CommandHandler("preview", cmd_preview, filters=private))
    app.add_handler(CommandHandler("ready", cmd_ready, filters=private))
    app.add_handler(MessageHandler(private & (filters.VOICE | filters.AUDIO), on_voice))
    app.add_handler(MessageHandler(private & (filters.Document.ALL | filters.PHOTO), on_file))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    build_app().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
