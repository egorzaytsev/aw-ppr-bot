"""Решения по заявкам из Telegram — только для админа (ADMIN_CHAT_ID).

Кнопки в уведомлении о заявке и в /inbox пишут Decision (и Target Project /
Decision Note) в Airtable и сразу запускают обычную обработку решений.
Что админ сейчас вводит текстом (текст отказа, название проекта) — в памяти бота.
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

from .airtable_ids import Decision, Registrations

log = logging.getLogger(__name__)

CB_ADMIN = "ad:"  # ad:<действие>:<id заявки>[:<id проекта>]
BASE_URL = "https://airtable.com/{base}/{table}/{rec}"

ACTION_ACCEPT, ACTION_DECLINE, ACTION_INFO = "acc", "dec", "inf"
ACTION_PICK, ACTION_NEW, ACTION_CANCEL = "pick", "new", "cancel"


def is_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    admin = ctx.bot_data.get("admin_chat_id")
    return bool(admin) and update.effective_chat is not None and str(update.effective_chat.id) == str(admin)


def _pending(ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    return ctx.bot_data.setdefault("admin_pending", {})


def record_url(ctx: ContextTypes.DEFAULT_TYPE, reg_id: str) -> str:
    return BASE_URL.format(base=ctx.bot_data["base_id"], table="tbl4lKbYhOkVIYNHQ", rec=reg_id)


def decision_markup(ctx: ContextTypes.DEFAULT_TYPE, reg_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Принять", callback_data=f"{CB_ADMIN}{ACTION_ACCEPT}:{reg_id}")],
        [InlineKeyboardButton("Отклонить", callback_data=f"{CB_ADMIN}{ACTION_DECLINE}:{reg_id}"),
         InlineKeyboardButton("Запросить инфо", callback_data=f"{CB_ADMIN}{ACTION_INFO}:{reg_id}")],
        [InlineKeyboardButton("Открыть в Airtable", url=record_url(ctx, reg_id))],
    ])


CARD_FIELDS = (
    ("Dealer News", "Текст"), ("End Client", "Клиент"), ("Object Name", "Объект"), ("City", "Город"),
    ("Project Stage", "Стадия"), ("Specifier", "Бюро"), ("Lead Architect", "ГАП"),
    ("PL Volume", "Объём по прайсу, €"), ("Lost Reason", "Причина"), ("Order / Proforma №", "Заказ / проформа"),
)


def _title(rtype: str, payload: dict) -> str:
    if payload.get("In Reply To"):
        return "💬 Ответ на ваш запрос"
    return {
        "Update — info only": "📰 Новость по проекту",
        "Update — Won": "🏆 Дилер сообщает: выигран",
        "Update — Lost": "✖️ Дилер сообщает: проигран",
    }.get(rtype, "Новая заявка")


def request_card(reg: dict, reg_id: str, author: str | None = None) -> str:
    name = reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия"
    rtype = reg.get(Registrations.REQUEST_TYPE) or "—"
    try:
        payload = json.loads(reg.get(Registrations.PAYLOAD) or "{}")
    except ValueError:
        payload = {}
    head = f"{_title(rtype, payload)}: <b>{escape(name)}</b>"
    lines = [head, escape(rtype) + (f" · от {escape(author)}" if author else "")]
    for key, label in CARD_FIELDS:
        if payload.get(key) not in (None, ""):
            lines.append(f"{label}: {escape(str(payload[key]))[:300]}")
    if rtype == "Update — Lost" and payload.get("Project Log"):
        lines.append(escape(payload["Project Log"])[:300])
    lines.append(f"<code>{reg_id}</code>")
    return "\n".join(lines)


async def cmd_inbox(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update, ctx):
        return
    recs = await asyncio.to_thread(ctx.bot_data["repo"].pending_without_decision)
    if not recs:
        await update.effective_chat.send_message("В Inbox нет заявок без решения.")
        return
    await update.effective_chat.send_message(f"Заявок без решения: {len(recs)}")
    for rec in recs:
        await update.effective_chat.send_message(
            request_card(rec["fields"], rec["id"]), parse_mode=ParseMode.HTML,
            reply_markup=decision_markup(ctx, rec["id"]),
        )


def _search_words(reg: dict) -> list[str]:
    try:
        payload = json.loads(reg.get(Registrations.PAYLOAD) or "{}")
    except ValueError:
        payload = {}
    text = " ".join(str(payload.get(k) or "") for k in ("Object Name", "End Client", "Object Address"))
    text += " " + (reg.get(Registrations.DEALER_PROJECT_NAME) or "")
    return sorted(set(re.findall(r"[\w-]{4,}", text.lower())), key=len, reverse=True)


async def _apply_now(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Не ждать минутного цикла — обработать решения сразу."""
    try:
        await ctx.bot_data["process_decisions"](ctx.application)
    except Exception:
        log.exception("Ошибка немедленной обработки решения")


async def on_admin_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    chat = update.effective_chat
    if not is_admin(update, ctx):
        return
    repo = ctx.bot_data["repo"]
    action, _, rest = data[len(CB_ADMIN):].partition(":")
    reg_id, _, project_id = rest.partition(":")
    pending = _pending(ctx)

    if action == ACTION_CANCEL:
        pending.pop(chat.id, None)
        await chat.send_message("Отменено.")
        return

    try:
        reg = await asyncio.to_thread(repo.registration_fields, reg_id)
    except Exception:
        await chat.send_message("Заявка не найдена.")
        return
    name = escape(reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия")
    if reg.get(Registrations.DECISION) and action != ACTION_PICK:
        await chat.send_message(f"По «{name}» решение уже стоит: {escape(reg[Registrations.DECISION])}.")
        return

    if action in (ACTION_DECLINE, ACTION_INFO):
        pending[chat.id] = (action, reg_id)
        what = "отказа" if action == ACTION_DECLINE else "запроса"
        await chat.send_message(
            f"«{name}»: напишите текст {what} — он уйдёт дилеру как есть.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data=f"{CB_ADMIN}{ACTION_CANCEL}:")]]),
        )
        return

    if action == ACTION_ACCEPT:
        if reg.get(Registrations.TARGET_PROJECT):
            await asyncio.to_thread(repo.set_decision, reg_id, Decision.ACCEPT_MERGE)
            await chat.send_message(f"«{name}»: принято, применяю…")
            await _apply_now(ctx)
            return
        candidates = await asyncio.to_thread(repo.find_project_candidates, _search_words(reg))
        rows = [[InlineKeyboardButton(f"В проект: {pname[:40]}", callback_data=f"{CB_ADMIN}{ACTION_PICK}:{reg_id}:{pid}")]
                for pid, pname in candidates]
        rows.append([InlineKeyboardButton("Создать новый проект", callback_data=f"{CB_ADMIN}{ACTION_NEW}:{reg_id}")])
        rows.append([InlineKeyboardButton("Отмена", callback_data=f"{CB_ADMIN}{ACTION_CANCEL}:")])
        hint = "Похожие проекты в базе:" if candidates else "Похожих проектов по словам из заявки не нашёл."
        await chat.send_message(f"«{name}»: в какой проект?\n{hint}", reply_markup=InlineKeyboardMarkup(rows))
        return

    if action == ACTION_PICK and project_id:
        await asyncio.to_thread(repo.set_decision, reg_id, Decision.ACCEPT_MERGE, None, project_id)
        await chat.send_message(f"«{name}»: принято в существующий проект, применяю…")
        await _apply_now(ctx)
        return

    if action == ACTION_NEW:
        pending[chat.id] = (ACTION_NEW, reg_id)
        await chat.send_message(
            f"«{name}»: название нового проекта латиницей, как оно будет в Projects.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data=f"{CB_ADMIN}{ACTION_CANCEL}:")]]),
        )


async def on_admin_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Текст админа в ответ на вопрос бота. True — обработано, дальше не передавать."""
    if not is_admin(update, ctx):
        return False
    pending = _pending(ctx)
    item = pending.pop(update.effective_chat.id, None)
    if not item:
        return False
    action, reg_id = item
    repo = ctx.bot_data["repo"]
    chat = update.effective_chat
    text = text.strip()
    if not text:
        pending[chat.id] = item
        await chat.send_message("Пустой текст — напишите ещё раз.")
        return True
    if action == ACTION_NEW:
        project_id = await asyncio.to_thread(repo.create_project, text)
        await asyncio.to_thread(repo.set_decision, reg_id, Decision.ACCEPT, None, project_id)
        await chat.send_message(f"Проект «{escape(text)}» создан, принимаю заявку…", parse_mode=ParseMode.HTML)
    else:
        decision = Decision.DECLINE if action == ACTION_DECLINE else Decision.NEED_INFO
        await asyncio.to_thread(repo.set_decision, reg_id, decision, text)
        await chat.send_message("Отправляю дилеру…")
    await _apply_now(ctx)
    return True
