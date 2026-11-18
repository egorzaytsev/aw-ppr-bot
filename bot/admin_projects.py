"""«Проекты моих дилеров» — раздел проверяющего (Егор, Лола), отдельно от «Мои проекты» дилера.

Чьи проекты: Owner = этот проверяющий ИЛИ Partner — его партнёр (Лола: Astros, Aluframe); Егору — все.
Сначала выбор дилера (кнопки с числом проектов), внутри — список проектов; поиск по коду AW… или названию.
"""
from __future__ import annotations

import asyncio
import logging
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from datetime import datetime

from . import admin, cards, dedup, memo, routing
from .slots import VALENCIA
from .airtable_ids import Partners, Projects

log = logging.getLogger(__name__)

CB = "am:"                  # тот же префикс, что у админ-меню (main.CB_ADMIN_MENU)
GROUPS, GROUP, OPEN, FIND = "dp", "dpg", "dpo", "dpf"
WRITE, WRITE_TO, DONE, SEND = "dpw", "dpm", "dpd", "dps"   # «✉️ Написать менеджеру»
ACTION_FIND = "dpfind"      # ожидание текста поиска у проверяющего (admin.on_admin_text)
ACTION_MSG = "dpmsg"        # ожидание текста сообщения менеджеру
MEMO_KEY = "admin_msg"      # черновик сообщения: {чат проверяющего: {project, person, text, files}}
CB_DEALER_NEWS = "pc:news:" # у дилера: «Ответить» → «Сообщить новость» по этому проекту (main.CB_PICK + MODE_NEWS)
PAGE = 8
NO_DEALER = "-"
OWNER_OF = {routing.EGOR: "Egor Zaitcev (me)", routing.LOLA: "Lola Sapaeva"}
PARTNERS_OF = {routing.LOLA: set(Partners.BY_COUNTRY.values())}   # Astros, Aluframe
_CLOSED = {"Won", "Lost"}


def scoped(projects: list[dict], who: str | None) -> list[dict]:
    """Проекты этого проверяющего: Егору — все; Лоле — свои (Owner) и её партнёров (Partner)."""
    if who == routing.EGOR or who is None:
        return list(projects)
    owner, partners = OWNER_OF.get(who), PARTNERS_OF.get(who, set())
    return [p for p in projects if p.get("owner") == owner or set(p.get("partner_ids") or []) & partners]


def group_key(p: dict) -> str:
    """Дилер проекта: компания в DEALER, иначе партнёр, иначе «без дилера»."""
    return ((p.get("dealer_ids") or []) + (p.get("partner_ids") or []) + [NO_DEALER])[0]


def _order(projects: list[dict]) -> list[dict]:
    """Открытые сверху, внутри — новые первыми (по коду)."""
    return sorted(projects, key=lambda p: (p.get("status") in _CLOSED, [-ord(c) for c in (p.get("ref") or "")]))


def _button(p: dict) -> InlineKeyboardButton:
    return InlineKeyboardButton(cards.project_button(p.get("ref") or "", p.get("name") or "Без названия",
                                                     p.get("status") or ""), callback_data=f"{CB}{OPEN}:{p['id']}")


async def _mine(ctx: ContextTypes.DEFAULT_TYPE, chat_id) -> list[dict]:
    projects = await asyncio.to_thread(ctx.bot_data["repo"].dedup_projects)
    return scoped(projects, routing.who_by_chat(ctx.bot_data, chat_id))


async def _show(update: Update, text: str, rows: list, edit: bool) -> None:
    markup = InlineKeyboardMarkup(rows)
    query = update.callback_query
    if edit and query and query.message:
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            return
        except Exception:
            pass
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def show_groups(update: Update, ctx: ContextTypes.DEFAULT_TYPE, page: int = 0, edit: bool = False) -> None:
    repo = ctx.bot_data["repo"]
    mine = await _mine(ctx, update.effective_chat.id)
    if not mine:
        await update.effective_chat.send_message("🛠 Проектов твоих дилеров в базе нет.")
        return
    counts: dict[str, int] = {}
    for p in mine:
        counts[group_key(p)] = counts.get(group_key(p), 0) + 1
    names = {k: ("Без дилера" if k == NO_DEALER else await asyncio.to_thread(repo.company_name, k)) for k in counts}
    keys = sorted(counts, key=lambda k: (k == NO_DEALER, -counts[k], names[k].lower()))
    pages = (len(keys) + PAGE - 1) // PAGE
    page = max(0, min(page, pages - 1))
    rows = [[InlineKeyboardButton(f"{names[k][:40]} ({counts[k]})", callback_data=f"{CB}{GROUP}:{k}:0")]
            for k in keys[page * PAGE:(page + 1) * PAGE]]
    nav = ([InlineKeyboardButton("← Назад", callback_data=f"{CB}{GROUPS}:{page - 1}:e")] if page > 0 else []) + \
          ([InlineKeyboardButton("Дальше →", callback_data=f"{CB}{GROUPS}:{page + 1}:e")] if page < pages - 1 else [])
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔎 Найти по коду AW… или названию", callback_data=f"{CB}{FIND}")])
    title = f"🛠 <b>Проекты моих дилеров</b> — {len(mine)}. Выбери дилера:" + (f" ({page + 1}/{pages})" if pages > 1 else "")
    await _show(update, title, rows, edit)


async def show_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE, key: str, page: int = 0,
                     edit: bool = False) -> None:
    repo = ctx.bot_data["repo"]
    items = _order([p for p in await _mine(ctx, update.effective_chat.id) if group_key(p) == key])
    name = "Без дилера" if key == NO_DEALER else await asyncio.to_thread(repo.company_name, key)
    if not items:
        await update.effective_chat.send_message(f"🛠 У «{escape(name)}» проектов нет.")
        return
    pages = (len(items) + PAGE - 1) // PAGE
    page = max(0, min(page, pages - 1))
    rows = [[_button(p)] for p in items[page * PAGE:(page + 1) * PAGE]]
    nav = ([InlineKeyboardButton("← Назад", callback_data=f"{CB}{GROUP}:{key}:{page - 1}:e")] if page > 0 else []) + \
          ([InlineKeyboardButton("Дальше →", callback_data=f"{CB}{GROUP}:{key}:{page + 1}:e")] if page < pages - 1 else [])
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("← К дилерам", callback_data=f"{CB}{GROUPS}:0:e")])
    title = f"🛠 <b>{escape(name)}</b> — проектов {len(items)}" + (f" ({page + 1}/{pages})" if pages > 1 else "")
    await _show(update, title, rows, edit)


async def open_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE, project_id: str) -> None:
    repo = ctx.bot_data["repo"]
    project = await asyncio.to_thread(repo.project_fields, project_id)
    ids = (project.get(Projects.END_CLIENT) or []) + (project.get(Projects.SPECIFIER) or [])
    companies = {c: await asyncio.to_thread(repo.company_name, c) for c in ids}
    dealers = (project.get(Projects.DEALER) or []) + (project.get(Projects.PARTNER) or [])
    who = ", ".join([await asyncio.to_thread(repo.company_name, c) for c in dealers]) or "без дилера"
    status = project.get(Projects.STATUS) or ""
    head = f"🛠 <b>Проект</b> · {escape(who)}" + (f" · {escape(status)}" if status else "")
    key = (dealers + [NO_DEALER])[0]
    await admin.send_card(ctx.bot, update.effective_chat.id,
                          "\n".join([head] + admin.project_digest(project, "", companies)),
                          InlineKeyboardMarkup([
                              [InlineKeyboardButton("✉️ Написать менеджеру", callback_data=f"{CB}{WRITE}:{project_id}")],
                              [InlineKeyboardButton("← К списку", callback_data=f"{CB}{GROUP}:{key}:0")]]))


# ---------- «✉️ Написать менеджеру»: текст + файлы → просмотр → менеджеру дилера ----------

def managers_of(project: dict) -> list[str]:
    out: list[str] = []
    for f in (Projects.REGISTERED, Projects.PRE_REGISTERED, Projects.NOTIFIED):
        out += [m for m in project.get(f) or [] if m not in out]
    return out


async def write(update: Update, ctx: ContextTypes.DEFAULT_TYPE, project_id: str) -> None:
    repo = ctx.bot_data["repo"]
    project = await asyncio.to_thread(repo.project_fields, project_id)
    people = managers_of(project)
    if not people:
        await update.effective_chat.send_message(
            f"🛠 В {cards.project_line(project)} нет менеджеров дилеров — писать некому.", parse_mode=ParseMode.HTML)
        return
    if len(people) == 1:
        await write_to(update, ctx, project_id, people[0])
        return
    rows = [[InlineKeyboardButton((await asyncio.to_thread(repo.person_name, m) or "менеджер без имени")[:40],
                                  callback_data=f"{CB}{WRITE_TO}:{project_id}:{m}")] for m in people]
    rows.append(admin.cancel_row())
    await update.effective_chat.send_message(f"🛠 {cards.project_line(project)}: кому написать?",
                                             parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def write_to(update: Update, ctx: ContextTypes.DEFAULT_TYPE, project_id: str, person_id: str) -> None:
    repo = ctx.bot_data["repo"]
    chat = update.effective_chat
    name = await asyncio.to_thread(repo.person_name, person_id) or "менеджер без имени"
    await memo.set_item(ctx.bot_data, MEMO_KEY, str(chat.id),
                        {"project": project_id, "person": person_id, "text": "", "files": [],
                         "at": datetime.now(VALENCIA).timestamp()})
    admin._pending(ctx)[chat.id] = (ACTION_MSG, project_id, person_id)
    await chat.send_message(
        f"🛠 Кому: {escape(name)}. Напиши текст и пришли файлы — по одному, в любом порядке. "
        "Когда всё готово — «Готово», я покажу, как это увидит менеджер.",
        reply_markup=_draft_markup())


def _draft_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Готово", callback_data=f"{CB}{DONE}")], admin.cancel_row()])


DRAFT_TTL = 3 * 3600   # брошенный черновик не должен ловить файлы, присланные потом по другому делу


async def draft(ctx: ContextTypes.DEFAULT_TYPE, chat_id) -> dict | None:
    d = (await memo.get(ctx.bot_data, MEMO_KEY, {})).get(str(chat_id))
    if d and datetime.now(VALENCIA).timestamp() - float(d.get("at") or 0) > DRAFT_TTL:
        await memo.pop(ctx.bot_data, MEMO_KEY, str(chat_id))
        return None
    return d


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, item: tuple, text: str) -> None:
    chat = update.effective_chat
    d = await draft(ctx, chat.id)
    admin._pending(ctx)[chat.id] = item          # ждём ещё текст, файлы или «Готово»
    if not d:
        await chat.send_message("🛠 Черновик потерялся — открой проект и нажми «✉️ Написать менеджеру» снова.")
        admin._pending(ctx).pop(chat.id, None)
        return
    d["text"] = f"{d['text']}\n{text}".strip() if d.get("text") else text
    await memo.set_item(ctx.bot_data, MEMO_KEY, str(chat.id), d)
    await chat.send_message(f"🛠 Текст записал. Файлов: {len(d['files'])}. Пришли ещё файлы или нажми «Готово».",
                            reply_markup=_draft_markup())


async def on_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE, item: dict) -> bool:
    """Файл от проверяющего, пока пишется сообщение менеджеру. True — взяли в черновик."""
    chat = update.effective_chat
    d = await draft(ctx, chat.id)
    if not d:
        return False
    d["files"].append(item)
    await memo.set_item(ctx.bot_data, MEMO_KEY, str(chat.id), d)
    await chat.send_message(f"🛠 Файл «{escape(item.get('name') or 'файл')}» добавил (всего {len(d['files'])}). "
                            + ("Напиши текст или нажми «Готово»." if not d.get("text") else "Ещё файлы или «Готово»."),
                            reply_markup=_draft_markup())
    return True


def sender(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    """Отправитель — аккаунт, а не человек: «@aw_eeu» (сегодня за ним Лола, завтра — другой)."""
    user = update.effective_user
    if user and user.username:
        return f"@{user.username}"
    return routing.NAMES.get(routing.who_by_chat(ctx.bot_data, update.effective_chat.id), "Andreu World")


def dealer_text(who: str, project: dict, text: str, files: int) -> str:
    lines = [f"✉️ {escape(who)} по проекту {cards.project_line(project)}:"]
    if text:
        lines.append(escape(text))
    if files:
        lines.append(f"📎 Файлы ({files}) — сообщениями ниже.")
    lines.append("Ответить можно кнопкой «Ответить» " + ("под файлами." if files else "ниже."))
    return "\n".join(lines)


async def done(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    d = await draft(ctx, chat.id)
    if not d or not (d.get("text") or d.get("files")):
        await chat.send_message("🛠 Пока пусто — напиши текст или пришли файл.", reply_markup=_draft_markup())
        return
    repo = ctx.bot_data["repo"]
    project = await asyncio.to_thread(repo.project_fields, d["project"])
    name = await asyncio.to_thread(repo.person_name, d["person"]) or "менеджер без имени"
    who = sender(update, ctx)
    files = "\n".join(f"• {escape(f.get('name') or 'файл')}" for f in d["files"])
    await chat.send_message(
        f"🛠 Так увидит {escape(name)}:\n\n{dealer_text(who, project, d.get('text', ''), len(d['files']))}"
        + (f"\n\nФайлы:\n{files}" if files else ""),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📨 Отправить", callback_data=f"{CB}{SEND}")],
                                           [InlineKeyboardButton("✏️ Дописать", callback_data=f"{CB}{DONE}:more")],
                                           admin.cancel_row()]))


async def send(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    d = await memo.pop(ctx.bot_data, MEMO_KEY, str(chat.id))
    admin._pending(ctx).pop(chat.id, None)
    if not d:
        await chat.send_message("🛠 Это сообщение уже отправлено или отменено.")
        return
    repo = ctx.bot_data["repo"]
    project = await asyncio.to_thread(repo.project_fields, d["project"])
    name = await asyncio.to_thread(repo.person_name, d["person"]) or "менеджер без имени"
    who = sender(update, ctx)
    chats = await asyncio.to_thread(repo.chat_ids_for_people, [d["person"]])
    if not chats:
        await chat.send_message(f"🛠 ⚠️ У {escape(name)} нет Telegram в базе — сообщение не ушло.")
        return
    reply = InlineKeyboardMarkup([[InlineKeyboardButton("Ответить", callback_data=f"{CB_DEALER_NEWS}{d['project']}")]])
    failed = 0
    for c in chats:
        await ctx.bot.send_message(c, dealer_text(who, project, d.get("text", ""), len(d["files"])),
                                   parse_mode=ParseMode.HTML, reply_markup=None if d["files"] else reply)
        for i, f in enumerate(d["files"]):
            last = i == len(d["files"]) - 1
            markup = reply if last else None
            try:
                if str(f.get("mime") or "").startswith("image/"):
                    try:
                        await ctx.bot.send_photo(c, f["file_id"], reply_markup=markup)
                        continue
                    except Exception:
                        pass   # картинка, присланная файлом, — уходит документом
                await ctx.bot.send_document(c, f["file_id"], reply_markup=markup)
            except Exception:
                log.warning("Файл %s менеджеру не ушёл", f.get("name"), exc_info=True)
                failed += 1
    line = f"{datetime.now(VALENCIA):%d.%m.%Y} · {who} → {name}:"
    body = [d.get("text") or "(без текста)"] + ([f"Файлы: {', '.join(f.get('name') or 'файл' for f in d['files'])}"]
                                                if d["files"] else [])
    await asyncio.to_thread(repo.append_project_log, d["project"], project.get(Projects.PROJECT_LOG), line, body)
    await chat.send_message(f"🛠 Отправлено: {escape(name)}. Записал в журнал проекта."
                            + (f" ⚠️ Не ушло файлов: {failed}." if failed else "")
                            + " Ответ придёт как новость по проекту.")


async def ask_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    admin._pending(ctx)[update.effective_chat.id] = (ACTION_FIND, "", "")
    await update.effective_chat.send_message(
        "🛠 Напиши код (AW260268 или 260268) или слово из названия, объекта, клиента — пришлю найденные проекты.",
        reply_markup=InlineKeyboardMarkup([admin.cancel_row()]))


async def find(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    mine = await _mine(ctx, update.effective_chat.id)
    status = {p["id"]: p.get("status") or "" for p in mine}
    found = dedup.search(text, mine)
    if not found:
        await update.effective_chat.send_message(
            f"🛠 Среди проектов твоих дилеров по «{escape(text)}» ничего не нашёл.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔎 Искать ещё раз", callback_data=f"{CB}{FIND}")]]))
        return
    rows = [[InlineKeyboardButton(cards.project_button(c.ref, c.name, status.get(c.project_id, "")),
                                  callback_data=f"{CB}{OPEN}:{c.project_id}")]
            for c in found]
    rows.append([InlineKeyboardButton("🔎 Искать ещё раз", callback_data=f"{CB}{FIND}")])
    await update.effective_chat.send_message(f"🛠 Нашёл по «{escape(text)}»:", reply_markup=InlineKeyboardMarkup(rows))


async def on_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE, action: str) -> bool:
    """am:dp… из админ-меню. True — обработано."""
    head, _, rest = action.partition(":")
    parts = rest.split(":") if rest else []
    edit = bool(parts) and parts[-1] == "e"
    if head == GROUPS:
        await show_groups(update, ctx, int(parts[0]) if parts and parts[0].isdigit() else 0, edit)
    elif head == GROUP and parts:
        page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        await show_group(update, ctx, parts[0], page, edit)
    elif head == OPEN and parts:
        await open_project(update, ctx, parts[0])
    elif head == FIND:
        await ask_find(update, ctx)
    elif head == WRITE and parts:
        await write(update, ctx, parts[0])
    elif head == WRITE_TO and len(parts) >= 2:
        await write_to(update, ctx, parts[0], parts[1])
    elif head == DONE:
        if parts and parts[0] == "more":
            d = await draft(ctx, update.effective_chat.id)
            if d:
                admin._pending(ctx)[update.effective_chat.id] = (ACTION_MSG, d["project"], d["person"])
            await update.effective_chat.send_message("🛠 Допиши текст или пришли ещё файлы, потом «Готово».",
                                                     reply_markup=_draft_markup())
        else:
            await done(update, ctx)
    elif head == SEND:
        await send(update, ctx)
    else:
        return False
    return True
