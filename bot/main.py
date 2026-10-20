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

from . import admin, cards, dedup, engine, phrases, reminders, routing
from .airtable_ids import Decision, People, Projects, Registrations, RequestType
from .decisions import payload_of, plan as decision_plan
from .airtable_repo import (
    PPR_NOTIFIED, PPR_PRE_REGISTERED, PPR_REGISTERED, AirtableRepo, DealerProject, Person,
)
from .config import load_settings
from .scenarios import FILL, INFO_REPLY, LOST, NEW_PROJECT, NEWS, WON
from .scenarios.update_project import fill_preset
from .slots import VALENCIA
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
    "Через меня ты подаёшь уведомления и предрегистрацию. Выбери действие внизу."
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
CARD_ACTIONS = {"fill": FILL, "news": NEWS, "won": WON, "lost": LOST}
# Следующий этап по текущему статусу дилера
NEXT_REQUEST = {PPR_NOTIFIED: RequestType.UPGRADE_PRE_REGISTRATION, PPR_PRE_REGISTERED: RequestType.UPGRADE_REGISTRATION}
NEXT_LABEL = {PPR_NOTIFIED: "предрегистрацию", PPR_PRE_REGISTERED: "регистрацию"}
SIMPLIFIED = "Simplified (<30k)"
REMINDER_SLOTS = ("08:00–11:00", "11:00–14:00", "14:00–17:00", "17:00–20:00")
WELCOME_TEXT = (
    "Привет! Я — Пепе, бот программы регистрации продакт плейсмента Andreu World. Пепе — это PP, product placement.\n\n"
    "Через меня ты регистрируешь плейсмент мебели Andreu World в своих проектах: уведомление, "
    "затем предрегистрация и регистрация. Пара минут на проект, можно голосом.\n\n"
    "• <b>Новый проект</b> — подать уведомление о плейсменте\n"
    "• <b>Мои проекты</b> — статусы и сроки твоих плейсментов\n"
    "• <b>Обновить проект</b> — новости и материалы\n\n"
    "Чем раньше и полнее ты заявляешь плейсмент, тем выше приоритет при совпадении с другим партнёром."
)
REMINDER_QUESTION = (
    "Иногда я буду напоминать о проектах, где подходит срок или не хватает данных. "
    "В какое время дня тебе удобнее получать напоминания? Время — твоё местное."
)


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
        who = f"@{escape(username)}" if username else "без username"
        how = (f"Добавь в People с Telegram Username <code>{escape(username)}</code>" if username
               else f"Username нет — впиши в People Telegram Chat ID <code>{chat_id}</code>")
        await _send_html(ctx.bot, admin_chat,
                         f"👋 <b>Новый человек в боте</b>: {escape(full_name) or '—'} · {who}\n"
                         f"{how} — я сам замечу и напишу ему. Ждём {UNKNOWN_EXPIRY_DAYS} дней.",
                         InlineKeyboardMarkup([[InlineKeyboardButton("Открыть в Airtable",
                                                                     url=admin.record_url(ctx, reg_id))]]))


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
        label = f"@{username}" if username else (data.get("name") or str(chat_id))
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
                                 f"✅ {escape(label)} теперь в боте: {escape(person.name)} ({escape(companies)}). "
                                 "Ему отправлено приглашение.")
            continue
        created = f.get(Registrations.SUBMITTED_AT)
        if created and datetime.now(timezone.utc) - datetime.fromisoformat(created.replace("Z", "+00:00")) \
                > timedelta(days=UNKNOWN_EXPIRY_DAYS):
            await asyncio.to_thread(repo.mark_processed, rec["id"], "expired", closed=False)
            if admin_chat:
                await _send_html(app.bot, admin_chat,
                                 f"⏳ {escape(label)} ждал {UNKNOWN_EXPIRY_DAYS} дней, в People так и не появился — "
                                 "запись закрыта. Напишет снова — придёт новая.")


async def _save(ctx: ContextTypes.DEFAULT_TYPE, person: Person, state) -> None:
    await asyncio.to_thread(_repo(ctx).save_state, person.record_id, state)
    person.state = state


def _hello(person: Person) -> str:
    """«Ола, Лола!» / «Привет, Лола!» — по имени, если оно есть."""
    first = phrases.first_name(person.name)
    word = phrases.pick(phrases.HELLO)
    return f"{word}, {escape(first)}!" if first else f"{word}!"


def _has_answers(state) -> bool:
    return any(not k.startswith("_") for k in state.answers)


def _dialog_name(state) -> str:
    name = engine.scenario_of(state).title
    target = state.answers.get("_target")
    return f"{name} · {target['name']}" if target else name


async def _blocked(update: Update, ctx: ContextTypes.DEFAULT_TYPE, person: Person, pending: str) -> bool:
    """Открыт другой диалог. Пустой — тихо закрываем; с ответами — спрашиваем, что делать."""
    if not person.state:
        return False
    if not _has_answers(person.state):
        await _save(ctx, person, None)
        return False
    await _send(update, [engine.Screen(
        f"У тебя есть незаконченная заявка: {escape(_dialog_name(person.state))}.",
        [[("Продолжить её", CB_MENU_RESUME)], [("Отменить её и перейти", f"{CB_DROP_AND_GO}{pending}")]],
    )])
    return True


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
        await _send(update, [_reminder_question()])
    else:
        greeting = GREETING_TEXT.replace("Привет, это", f"{_hello(person)} Это", 1)
        await update.effective_chat.send_message(greeting, reply_markup=MENU_KEYBOARD)
    if person.state:
        if _has_answers(person.state):
            await _send(update, [engine.Screen(
                f"У тебя есть незаконченная заявка: {escape(_dialog_name(person.state))}.",
                [[("Продолжить её", CB_MENU_RESUME)], [("Отменить её", CB_DROP_AND_GO)]],
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


async def _start_scenario(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, person: Person,
    code: str = NEW_PROJECT.code, preset: dict | None = None,
) -> None:
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
        if admin.is_admin(update, ctx):
            await update.effective_chat.send_message(
                "🛠 Сейчас я ничего не жду. Если это название проекта — нажми «Принять» на заявке ещё раз "
                "(или открой /inbox) и выбери «Создать новый проект».",
                reply_markup=MENU_KEYBOARD,
            )
            return
        await update.effective_chat.send_message("Выбери действие внизу.", reply_markup=MENU_KEYBOARD)
        return
    await _apply(update, ctx, person, engine.handle_text(person.state, text))


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
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
                [("⏭ Запросить следующий этап", f"{CB_UPDATE_MODE}{MODE_NEXT}")],
            ])])
        return
    await _handle_answer_text(update, ctx, text)


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    media = update.message.voice or update.message.audio
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
    ext = "ogg" if update.message.voice else "mp3"
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


def _button_label(p: DealerProject) -> str:
    ref = p.fields.get(Projects.PPR_REF)
    label = (f"{ref} · " if ref else "") + f"{p.name[:28]} · {PPR_LABELS[p.ppr_status].lower()}"
    days = _days_left(p)
    if days is not None:
        label += f" · {days} дн." if days >= 0 else " · срок истёк"
    return label


async def _projects(ctx: ContextTypes.DEFAULT_TYPE, person: Person) -> list[DealerProject]:
    return _sorted(await asyncio.to_thread(_repo(ctx).dealer_projects, person.company_ids))


async def _show_project_list(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, person: Person, mode: str, page: int
) -> None:
    if mode != MODE_MY and await _blocked(update, ctx, person, f"{CB_LIST}{mode}:{page}"):
        return
    projects = await _projects(ctx, person)
    pending = await asyncio.to_thread(_repo(ctx).pending_requests, person.company_ids) if mode == MODE_MY else []
    if not projects:
        text = "Пока нет проектов с твоим плейсментом в программе. Подать новый — кнопка «Новый проект» внизу."
        if pending:
            text = pending_text(pending) + "\n\n" + text
        await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=MENU_KEYBOARD)
        return
    pages = (len(projects) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, pages - 1))
    chunk = projects[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    buttons = [[(_button_label(p), f"{CB_PICK}{mode}:{p.record_id}")] for p in chunk]
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
        MODE_NEXT: "По какому проекту запросить следующий этап?",
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
    projects = await asyncio.to_thread(_repo(ctx).dealer_projects, person.company_ids)
    return next((p for p in projects if p.record_id == project_id), None)


async def _show_card(update: Update, ctx: ContextTypes.DEFAULT_TYPE, person: Person, project_id: str) -> None:
    p = await _own_project(ctx, person, project_id)
    if not p:
        await update.effective_chat.send_message("Проект не найден. Открой «Мои проекты» заново.")
        return
    # Только своё: имя, статус дилера, срок, стадия, квотация. Других дилеров, лог, разбор, баллы — никогда.
    lines = [f"<b>{escape(p.name)}</b>"]
    if p.fields.get(Projects.PPR_REF):
        lines.append(f"🔖 Референс: {escape(p.fields[Projects.PPR_REF])}")
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
        lines.append("Схема: упрощённая — объём до 30 000 € по прайсу")
        q = (await asyncio.to_thread(_repo(ctx).dealer_quotes, person.company_ids)).get(p.record_id)
        if q and q.get("valid"):
            no = f"№ {escape(str(q['no']))}, " if q.get("no") else ""
            lines.append(f"Квотация: {no}действует до {_date(q['valid'])}")
        elif q:
            lines.append(f"Квотация: запрошена {_date(q['at'])}" + ("" if q.get("accepted") else ", на проверке"))
    if days is not None and days <= EXPIRY_WARNING_DAYS:
        lines += ["", "Срок подходит к концу. Если по проекту есть прогресс — нажми «Сообщить новость», "
                      "чтобы запросить продление."]
    pid = p.record_id
    buttons = [
        *([[("⏭ Запросить следующий этап", f"{CB_CARD}next:{pid}")]] if _next_request_type(p) else []),
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
    await _start_scenario(update, ctx, person, CARD_ACTIONS[action].code, preset)


async def _start_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE, person: Person, reg_id: str) -> None:
    if await _blocked(update, ctx, person, f"{CB_REPLY}{reg_id}"):
        return
    try:
        reg = await asyncio.to_thread(_repo(ctx).registration_fields, reg_id)
    except Exception:
        await update.effective_chat.send_message("Заявка не найдена.")
        return
    # Отвечать может только компания, от которой заявка
    if not set(reg.get(Registrations.DEALER) or []) & set(person.company_ids):
        await update.effective_chat.send_message("Заявка не найдена.")
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
            why = "Для проектов до 30 000 € по прайсу следующего этапа нет — квотацию запрашивай в бэк-офисе."
        elif p.fields.get(Projects.REGISTERED):
            why = "Регистрация на объекте уже у партнёра — следующий этап сейчас недоступен."
        else:
            why = "\n".join(cards.next_step_body(dict(p.fields), ""))
        await update.effective_chat.send_message(why, reply_markup=MENU_KEYBOARD)
        return
    what = NEXT_LABEL[p.ppr_status]
    if not confirmed:
        await _send(update, [engine.Screen(
            f"Запросить {what} по «{escape(p.name)}»? Все данные для этапа есть — Егор посмотрит и ответит.",
            [[("Да, запросить", f"{CB_CARD}nextok:{p.record_id}")], [("← Назад", f"{CB_LIST}{MODE_MY}:0")]],
        )])
        return
    payload = {"Dealer Project Name": p.name}
    reg_id = await asyncio.to_thread(_repo(ctx).create_registration, person, request_type, payload, p.name, p.record_id)
    await update.effective_chat.send_message(
        f"{phrases.pick(phrases.THANKS)} Запрос на {what} по «{p.name}» отправил Егору. Ответ — в течение 72 часов.",
        reply_markup=MENU_KEYBOARD,
    )
    await _notify_admin_new(ctx, person, reg_id, request_type, payload, p.name)


# ---------- напоминания ----------

def _reminder_question() -> engine.Screen:
    items = [(slot, f"{CB_REMINDER}{i}") for i, slot in enumerate(REMINDER_SLOTS)]
    return engine.Screen(REMINDER_QUESTION, [items[:2], items[2:]])


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
    await _send(update, [_reminder_question()])


async def _submit(update: Update, ctx: ContextTypes.DEFAULT_TYPE, person: Person) -> None:
    state = person.state
    sc = engine.scenario_of(state)
    request_type, stage_label = engine.stage_of(state)
    payload = engine.build_payload(state)
    name = payload.get(sc.project_name_key) if sc.project_name_key else None
    files = engine.collected_files(state)
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
    done = f"{phrases.pick(phrases.THANKS)} {sc.done_text}"
    egor = phrases.maybe_egor()
    if egor:
        done += f"\n{egor}"
    if failed:
        done += f"\n\nНе загрузилось файлов: {len(failed)} ({', '.join(failed)}). Пришли их Егору ссылкой."
    await update.effective_chat.send_message(done, reply_markup=MENU_KEYBOARD)
    log.info("Заявка %s от %s (%s), файлов %d", reg_id, person.record_id, request_type, len(files))

    await _notify_admin_new(ctx, person, reg_id, request_type, payload, name)


async def _notify_admin_new(ctx: ContextTypes.DEFAULT_TYPE, person: Person, reg_id: str,
                            request_type: str, payload: dict, name: str | None) -> None:
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
            await ctx.bot.send_message(
                admin_chat,
                admin.request_card(card_fields, reg_id, person.name, analysis,
                                   ", ".join(_repo(ctx).company_name(c) for c in person.company_ids) or None),
                parse_mode=ParseMode.HTML,
                reply_markup=admin.decision_markup(ctx, reg_id, candidates),
            )
        except Exception:
            log.exception("Не удалось уведомить проверяющего %s", admin_chat)


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
            log.exception("Не удалось загрузить файл %s в %s", item.get("name"), reg_id)
            failed.append(item.get("name") or "file")
    return failed


# ---------- файлы ----------

async def on_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
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
    if not person.state:
        await update.effective_chat.send_message("Выбери действие внизу.", reply_markup=MENU_KEYBOARD)
        return
    if (item["size"] or 0) > MAX_FILE_BYTES:
        await update.effective_chat.send_message(
            f"Файл «{item['name']}» больше {MAX_FILE_BYTES // 1024 // 1024} МБ — пришли его ссылкой "
            "(Яндекс Диск, Google Drive, облако)."
        )
        return
    await _apply(update, ctx, person, engine.handle_file(person.state, item))


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


async def process_decisions(app: Application) -> None:
    # Минутный цикл и кнопки админа не должны обработать одну заявку дважды
    async with app.bot_data.setdefault("decisions_lock", asyncio.Lock()):
        await _process_decisions(app)


LINK_ROLE = {Projects.END_CLIENT: "клиент", Projects.SPECIFIER: "бюро", Projects.DEALER: "дилер"}
DECISION_HEAD = {
    "Accept": "✅ Принято", "Accept — merge into Target Project": "✅ Принято",
    "Decline": "❌ Отказ", "Need more info": "❓ Запрошена информация",
}


def _admin_summary(reg: dict, pl, project_name: str, created: bool, dealer: str, sent: bool,
                   companies: list[str], terms: str | None) -> str:
    """Итог решения для админа: что сделано, без лишнего."""
    decision = reg.get(Registrations.DECISION) or ""
    head = DECISION_HEAD.get(decision, decision)
    title = project_name if decision.startswith("Accept") else (reg.get(Registrations.DEALER_PROJECT_NAME) or "")
    lines = [f"🛠 <b>{head} · {escape(title)}</b>"]
    if pl.status_label:
        lines.append(f"• {escape(dealer)}: статус <b>{pl.status_label}</b>")
    if pl.quote:
        lines.append("• Запрос квотации передан в работу")
        if terms:
            lines.append(f"• Условия: <b>{escape(terms)}</b>")
    if decision.startswith("Accept"):
        if created:
            lines.append("• Проект создан")
        elif pl.transferred or pl.log_lines:
            lines.append("• Проект дополнен данными из заявки")
        if companies:
            lines.append("• Компании: " + escape(", ".join(companies)))
    lines.append("• Дилеру отправлено" if sent else "• Дилеру не отправлено — нет Telegram")
    return "\n".join(lines)


async def _process_decisions(app: Application) -> None:
    repo: AirtableRepo = app.bot_data["repo"]
    created_projects: set = app.bot_data.setdefault("created_projects", set())
    decided_by: dict = app.bot_data.setdefault("decided_by", {})
    for rec in await asyncio.to_thread(repo.decisions_to_process):
        reg = rec["fields"]
        # Полный итог — тому, кто решал (по кнопке), иначе по маршруту; Егору при решении Лолы — строка
        routed = routing.resolve(app.bot_data, routing.route(
            await asyncio.to_thread(repo.company_types, reg.get(Registrations.DEALER) or [])))[0]
        decider, admin_chat = routing.resolve(app.bot_data, decided_by.get(rec["id"]) or routed)
        target = reg.get(Registrations.TARGET_PROJECT) or []
        project = await asyncio.to_thread(repo.project_fields, target[0]) if target else None
        pl = decision_plan(reg, project)
        if not pl.registration_updates:
            continue
        dealers = reg.get(Registrations.DEALER) or []
        dealer_name = ", ".join([await asyncio.to_thread(repo.company_name, c) for c in dealers]) or "дилер"
        company_notes: list[str] = []
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
                        repo.find_or_create_company, link["name"], link["type"], country)
                if link.get("append"):
                    updates[link["field"]] = list((project or {}).get(link["field"]) or []) + [cid]
                else:
                    updates[link["field"]] = [cid]
                cname = await asyncio.to_thread(repo.company_name, cid)
                company_notes.append(f"{cname} ({LINK_ROLE[link['field']]}{', новая' if created else ''})")
            await asyncio.to_thread(repo.update_project, target[0], updates)
        if pl.log_lines and target:
            header = f"{datetime.now(VALENCIA):%d.%m.%Y} · {dealer_name} (заявка бота):"
            await asyncio.to_thread(repo.append_project_log, target[0],
                                    (project or {}).get(Projects.PROJECT_LOG), header, pl.log_lines)
        # Карточка дилеру — по свежему состоянию проекта после записи
        accepted = ((reg.get(Registrations.DECISION) or "").startswith("Accept") or pl.declined_upgrade) \
            and target and not pl.blocked
        card = None
        if accepted:
            fresh = await asyncio.to_thread(repo.project_fields, target[0])
            dealer_id = dealers[0] if dealers else ""
            before = set((project or {}).get(Projects.NOTIFIED) or []) | set(
                (project or {}).get(Projects.PRE_REGISTERED) or []) | set((project or {}).get(Projects.REGISTERED) or [])
            note = (reg.get(Registrations.DECISION_NOTE) or "").strip()
            if pl.declined_upgrade:
                asked = "регистрация" if "Registration" in (reg.get(Registrations.REQUEST_TYPE) or "") \
                    and "Pre" not in (reg.get(Registrations.REQUEST_TYPE) or "") else "предрегистрация"
                card = cards.declined_upgrade_card(fresh, dealer_id, dealer_name, payload_of(reg), asked,
                                                   note, dealer_id not in before)
            elif pl.status_label:
                card = cards.accept_card(fresh, dealer_id, dealer_name, payload_of(reg), dealer_id not in before, note)
            else:
                rtype = reg.get(Registrations.REQUEST_TYPE)
                body = {
                    RequestType.UPDATE_WON: "📝 Информацию приняли — итог по проекту подтвердит Егор.",
                    RequestType.UPDATE_LOST: "📝 Информацию приняли — итог по проекту подтвердит Егор.",
                    RequestType.QUOTE_SIMPLIFIED: "📝 Спецификацию передали на квотацию.",
                    RequestType.UPDATE_NEWS: "📰 Новость записали в историю проекта.",
                }.get(rtype, "📝 Информацию внесли в проект.")
                card = cards.update_card(fresh, dealer_id, dealer_name, body, note)
            if pl.dealer_text:  # ответ на запрос принимается молча — у него dealer_text нет
                pl.dealer_text = card
        # Финальное решение закрывает заявку: Processed + время (запрос информации — ещё нет)
        mark = str(pl.registration_updates.get(Registrations.BOT_PROCESSED) or "")
        if mark.startswith("done:") and mark != f"done:{Decision.NEED_INFO}":
            pl.registration_updates[Registrations.PROCESSED] = True
            pl.registration_updates[Registrations.PROCESSED_AT] = datetime.now(timezone.utc).isoformat()
        # Отмечаем до рассылки — чтобы сбой отправки не повторил изменения
        await asyncio.to_thread(repo.update_registration, rec["id"], pl.registration_updates)
        log.info("Решение по %s: %s", rec["id"], pl.registration_updates.get(Registrations.BOT_PROCESSED))

        dealer_chats = await asyncio.to_thread(repo.chat_ids_for_people, reg.get(Registrations.SUBMITTED_BY) or [])
        if pl.dealer_text:
            markup = _markup([[("Ответить", f"{CB_REPLY}{rec['id']}")]]) if pl.reply_button else None
            for chat in dealer_chats:
                await _send_html(app.bot, chat, pl.dealer_text, markup)
        if pl.broadcast_companies and pl.broadcast_text:
            open_btn = _markup([[("Открыть проект", f"{CB_PICK}{MODE_MY}:{target[0]}")]]) if target else None
            for chat in await asyncio.to_thread(repo.chat_ids_for_companies, pl.broadcast_companies):
                if chat not in dealer_chats:
                    await _send_html(app.bot, chat, pl.broadcast_text, open_btn)
        if admin_chat:
            if pl.admin_text:
                await _send_html(app.bot, admin_chat, pl.admin_text,
                                 admin.ask_markup(rec["id"]) if pl.ask_text else None)
            elif card and pl.dealer_text:
                created = target[0] in created_projects
                created_projects.discard(target[0])
                extra = (["🆕 Проект создан"] if created else []) + (
                    ["🏢 Компании: " + escape(", ".join(company_notes))] if company_notes else [])
                await _send_html(app.bot, admin_chat, cards.admin_copy(card, bool(dealer_chats), extra))
            elif pl.dealer_text and not pl.blocked:
                await _send_html(app.bot, admin_chat, _admin_summary(
                    reg, pl, pl.project_name or "", False, dealer_name, bool(dealer_chats), [], None))
        if not pl.blocked:
            decided_by.pop(rec["id"], None)
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


async def _build_digest(repo: AirtableRepo, companies: list[str], today: date, name: str = ""):
    """(текст, кнопки, пункты) напоминания для компаний человека; None — напоминать нечего."""
    projects = _sorted(await asyncio.to_thread(repo.dealer_projects, companies))
    if not projects:
        return None
    news = await asyncio.to_thread(repo.last_news_by_project, companies)
    quotes = await asyncio.to_thread(repo.dealer_quotes, companies)
    items = [
        it for p in projects
        if (it := reminders.project_item(
            p, today, date.fromisoformat(news[p.record_id]) if p.record_id in news else None,
            (quotes.get(p.record_id) or {}).get("valid")))
    ]
    if not items:
        return None
    buttons = [[(f"Открыть: {it.name[:40]}", f"{CB_PICK}{MODE_MY}:{it.project_id}")] for it in items[:PAGE_SIZE]]
    intro = phrases.pick(phrases.REMINDER_INTRO)
    first = phrases.first_name(name)
    if first:
        intro = intro.replace("Ола!", f"Ола, {first}!")
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
            await update.effective_chat.send_message(f"🛠 Не нашёл в People @{escape(username.lstrip('@'))}.")
            return
        header = f"🛠 Так выглядело бы напоминание для {escape(person.name)} (не отправлено):\n\n"
    else:
        person = await _identify(update, ctx)
        header = ""
    if not person:
        return
    digest = await _build_digest(repo, person.company_ids, datetime.now(VALENCIA).date(), person.name)
    if not digest:
        await update.effective_chat.send_message("🛠 Напоминать нечего: по проектам всё в порядке.")
        return
    text, buttons, _ = digest
    # В чужом предпросмотре кнопок нет: проекты дилера админ из его карточки не откроет
    markup = None if username else _markup(buttons)
    await update.effective_chat.send_message(header + text, parse_mode=ParseMode.HTML, reply_markup=markup)


# ---------- админское меню ----------

CB_ADMIN_MENU = "am:"


def _admin_menu() -> engine.Screen:
    return engine.Screen("🛠 <b>Админ</b>", [
        [("📥 Заявки на проверке", f"{CB_ADMIN_MENU}inbox")],
        [("🔔 Моё напоминание", f"{CB_ADMIN_MENU}remind"), ("👀 Напоминание дилера", f"{CB_ADMIN_MENU}remind_user")],
        [("📊 Сводка", f"{CB_ADMIN_MENU}stats")],
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
        await chat.send_message("🛠 Username дилера (без @), например Lola_Sapaeva:")
    elif action == "stats":
        st = await asyncio.to_thread(_repo(ctx).admin_stats)
        lines = ["🛠 <b>Сводка</b>",
                 f"Без решения: {st['no_decision']}",
                 f"Ждём ответа дилера (Need more info): {st['need_info']}"]
        if st["blocked"]:
            lines.append("Заблокировано: " + escape(", ".join(st["blocked"])))
        lines.append(f"\nПодключено людей: {len(st['people'])}")
        for name, slot, last in st["people"]:
            lines.append(f"• {escape(name)} — время {slot or 'не выбрано'}, напоминание {_date(last) or '—'}")
        await chat.send_message("\n".join(lines), parse_mode=ParseMode.HTML)


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
        digest = await _build_digest(repo, f.get(People.COMPANY) or [], today, f.get(People.NAME, ""))
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
             "Егор в курсе, ответ придёт сюда.")


async def _check_overdue(app: Application) -> None:
    """Заявки без решения: 48 ч — снова карточка проверяющему, 72 ч — дилеру о задержке. По разу."""
    repo: AirtableRepo = app.bot_data["repo"]
    ctx = SimpleNamespace(bot_data=app.bot_data)
    for rec in await asyncio.to_thread(repo.undecided):
        f = rec["fields"]
        hours = admin.age_hours(f.get(Registrations.SUBMITTED_AT))
        mark = f.get(Registrations.BOT_PROCESSED) or ""
        name = f.get(Registrations.DEALER_PROJECT_NAME) or "без названия"
        if hours >= DEALER_LATE_HOURS and mark != "late:72":
            for chat in await asyncio.to_thread(repo.chat_ids_for_people, f.get(Registrations.SUBMITTED_BY) or []):
                await _send_html(app.bot, chat, LATE_TEXT.format(name=escape(name)))
            await asyncio.to_thread(repo.update_registration, rec["id"], {Registrations.BOT_PROCESSED: "late:72"})
        elif hours >= REVIEW_REMIND_HOURS and not mark:
            types = await asyncio.to_thread(repo.company_types, f.get(Registrations.DEALER) or [])
            _, chat = routing.resolve(app.bot_data, routing.route(types))
            if chat:
                card = admin.request_card(f, rec["id"], None, f.get(Registrations.ANALYSIS),
                                          ", ".join(repo.company_name(c) for c in f.get(Registrations.DEALER) or []) or None)
                await _send_html(app.bot, chat, "⏰ <b>Ждёт решения больше 48 часов</b>\n\n" + card
                                 + admin.age_line(f.get(Registrations.SUBMITTED_AT)),
                                 admin.decision_markup(ctx, rec["id"], await admin.merge_candidates(repo, f)))
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
                  BotCommand("reminder", "Напоминание сейчас")]


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Необработанная ошибка", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await update.effective_chat.send_message("Что-то пошло не так. Попробуй ещё раз.")
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
        app.bot_data["reminders_task"] = asyncio.create_task(_reminders_loop(app))

    async def post_shutdown(app: Application) -> None:
        for key in ("decisions_task", "reminders_task"):
            task = app.bot_data.get(key)
            if task:
                task.cancel()
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
    app.bot_data["base_id"] = settings.airtable_base_id
    app.bot_data["process_decisions"] = process_decisions
    app.bot_data["preview_reminder"] = preview_reminder

    app.add_handler(TypeHandler(Update, log_update), group=-1)
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    app.add_handler(CommandHandler("new", cmd_new, filters=private))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=private))
    app.add_handler(CommandHandler("time", cmd_time, filters=private))
    app.add_handler(CommandHandler("inbox", admin.cmd_inbox, filters=private))
    app.add_handler(CommandHandler("reminder", cmd_reminder, filters=private))
    app.add_handler(CommandHandler("admin", cmd_admin, filters=private))
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
