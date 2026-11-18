"""«Проекты моих дилеров» — раздел проверяющего (Егор, Лола), отдельно от «Мои проекты» дилера.

Чьи проекты: Owner = этот проверяющий ИЛИ Partner — его партнёр (Лола: Astros, Aluframe); Егору — все.
Сначала выбор дилера (кнопки с числом проектов), внутри — список проектов; поиск по коду AW… или названию.
"""
from __future__ import annotations

import asyncio
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from . import admin, cards, dedup, routing
from .airtable_ids import Partners, Projects

CB = "am:"                  # тот же префикс, что у админ-меню (main.CB_ADMIN_MENU)
GROUPS, GROUP, OPEN, FIND = "dp", "dpg", "dpo", "dpf"
ACTION_FIND = "dpfind"      # ожидание текста поиска у проверяющего (admin.on_admin_text)
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
                          InlineKeyboardMarkup([[InlineKeyboardButton("← К списку", callback_data=f"{CB}{GROUP}:{key}:0")]]))


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
    else:
        return False
    return True
