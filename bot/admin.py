"""Решения по заявкам из Telegram — только для админа (ADMIN_CHAT_ID).

Кнопки в уведомлении о заявке и в /inbox пишут Decision (и Target Project /
Decision Note) в Airtable и сразу запускают обычную обработку решений.
Что админ сейчас вводит текстом (текст отказа, название проекта) — в памяти бота.
"""
from __future__ import annotations

import asyncio
import json
import logging
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from . import dedup
from .scenarios.base import describe_items, format_value
from .scenarios.new_project import QUESTIONS as _NEW_PROJECT_QUESTIONS
from .airtable_ids import Decision, Registrations

log = logging.getLogger(__name__)

CB_ADMIN = "ad:"  # ad:<действие>:<id заявки>[:<id проекта>]
BASE_URL = "https://airtable.com/{base}/{table}/{rec}"

ACTION_ACCEPT, ACTION_DECLINE, ACTION_INFO = "acc", "dec", "inf"
ACTION_PICK, ACTION_NEW, ACTION_CANCEL = "pick", "new", "cancel"
ACTION_REMIND_USER = "remind_user"  # ждём username для предпросмотра напоминания
# Отказ в пред/регистрации без проекта: куда зафиксировать уведомление
ACTION_DPICK, ACTION_DNEW = "dpick", "dnew"
UPGRADE_REQUESTS = {"New — Pre-registration", "New — Registration",
                    "Upgrade — Pre-registration", "Upgrade — Registration"}


def is_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    admin = ctx.bot_data.get("admin_chat_id")
    return bool(admin) and update.effective_chat is not None and str(update.effective_chat.id) == str(admin)


async def drop_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Админ нажал меню — незавершённый ввод отменяется (и в Airtable)."""
    if not is_admin(update, ctx):
        return
    item = _pending(ctx).pop(update.effective_chat.id, None)
    repo = ctx.bot_data["repo"]
    if not item:
        item = await asyncio.to_thread(repo.find_awaiting)
    if item and item[0] != ACTION_REMIND_USER:
        await asyncio.to_thread(repo.clear_awaiting, item[1])


def _pending(ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    return ctx.bot_data.setdefault("admin_pending", {})


def record_url(ctx: ContextTypes.DEFAULT_TYPE, reg_id: str) -> str:
    return BASE_URL.format(base=ctx.bot_data["base_id"], table="tbl4lKbYhOkVIYNHQ", rec=reg_id)


def decision_markup(ctx: ContextTypes.DEFAULT_TYPE, reg_id: str,
                    candidates: list | None = None) -> InlineKeyboardMarkup:
    """Совпадения есть — «Слить с: …» на каждое и «Принять как новый»; нет — просто «Принять»."""
    rows = [[InlineKeyboardButton(f"🔗 Слить с: {name[:40]}",
                                  callback_data=f"{CB_ADMIN}{ACTION_PICK}:{reg_id}:{pid}")]
            for pid, name in (candidates or [])]
    if candidates:
        rows.append([InlineKeyboardButton("🆕 Принять как новый", callback_data=f"{CB_ADMIN}{ACTION_NEW}:{reg_id}:-")])
    else:
        rows.append([InlineKeyboardButton("Принять", callback_data=f"{CB_ADMIN}{ACTION_ACCEPT}:{reg_id}")])
    return InlineKeyboardMarkup(rows + [
        [InlineKeyboardButton("Отклонить", callback_data=f"{CB_ADMIN}{ACTION_DECLINE}:{reg_id}"),
         InlineKeyboardButton("Запросить инфо", callback_data=f"{CB_ADMIN}{ACTION_INFO}:{reg_id}")],
        [InlineKeyboardButton("Открыть в Airtable", url=record_url(ctx, reg_id))],
    ])


REQUEST_LABELS = {
    "New — Notification": "уведомление",
    "New — Pre-registration": "предрегистрация",
    "New — Registration": "регистрация",
    "Upgrade — Pre-registration": "переход на предрегистрацию",
    "Upgrade — Registration": "переход на регистрацию",
}
STAGE_LABELS = {
    "Architect selection": "выбор архитектора", "Concept": "концепция", "Design": "проектирование",
    "Tender": "тендер", "Procurement": "закупка",
}


def _title(rtype: str, payload: dict) -> str:
    if payload.get("In Reply To"):
        return "💬 Ответ на твой запрос"
    special = {
        "Update — info only": "📰 Обновление по проекту",
        "Update — news": "📰 Новость по проекту",
        "Update — fields": "📝 Дополнение по проекту",
        "Update — Won": "🏆 Дилер сообщает: выигран",
        "Update — Lost": "✖️ Дилер сообщает: проигран",
        "Quotation — Simplified": "💶 Запрос квотации · упрощённая схема",
    }
    if rtype in special:
        return special[rtype]
    return f"📥 Новая заявка · {REQUEST_LABELS.get(rtype, rtype)}"


def _v(payload: dict, key: str) -> str:
    value = payload.get(key)
    return "" if value in (None, "") else escape(str(value))[:300]


def _pair(*items: tuple) -> str | None:
    """«Город: X · Стадия: Y» — только заполненные части."""
    parts = [f"{label}: {value}" for label, value in items if value]
    return " · ".join(parts) or None


_QUESTIONS = {q.key: q for q in _NEW_PROJECT_QUESTIONS}

CONTACT_RU = {
    "Declared": "Заявляю (без подтверждения)",
    "Indirect (bureau docs)": "Документы от бюро",
    "Verifiable (email/contact)": "Переписка или контакт",
    "Joint meeting with AW": "Готов к совместной встрече",
}
ALT_RU = {
    "No — AW only": "Нет, только AW",
    "Yes — tender requirement": "Да, по требованию тендера",
    "Yes — own initiative": "Да, по своей инициативе",
}
STATUS_REQUESTS = {"New — Pre-registration", "New — Registration",
                   "Upgrade — Pre-registration", "Upgrade — Registration"}
FIELD_UPDATES = {"Update — info only", "Update — fields"}
# Служебные ключи Payload — в списке изменений не показываем
_META_KEYS = {"Dealer Project Name", "Project Log", "PPR Track", "EXW Potential", "In Reply To",
              "Specifier Type", "Dealer News Audio"}


def _ru(key: str, value) -> str:
    """Значение поля по-русски: варианты Airtable → подписи, числа, даты, файлы."""
    if key == "Contact Level":
        return CONTACT_RU.get(value, str(value))
    if key == "AW Alternatives":
        return ALT_RU.get(value, str(value))
    if key == "Project Stage":
        return STAGE_LABELS.get(value, str(value))
    q = _QUESTIONS.get(key)
    if q is not None:
        return format_value(q, value)
    if isinstance(value, list):
        return describe_items(value)
    return str(value)


def _card_body(rtype: str, payload: dict) -> list[str]:
    body: list[str] = []
    if payload.get("Dealer News"):
        body.append(f"«{_v(payload, 'Dealer News')}»")
    if rtype in FIELD_UPDATES and not payload.get("In Reply To"):
        changes = [(k, v) for k, v in payload.items()
                   if k not in _META_KEYS and k != "Dealer News" and v not in (None, "")]
        if changes:
            body.append("📝 Изменения:")
            for k, v in changes:
                title = _QUESTIONS[k].title if k in _QUESTIONS else k
                body.append(f"• {escape(title)}: {escape(_ru(k, v))[:300]}")
        return body
    pl = payload.get("PL Volume")
    stage = _ru("Project Stage", payload["Project Stage"]) if payload.get("Project Stage") else ""
    part = payload.get("Object Part")
    obj = _v(payload, "Object Name") + (f" · {escape(str(part))}" if part and part != "Весь объект" else "")
    for row in (
        _pair(("Клиент", _v(payload, "End Client"))),
        _pair(("Объект", obj)),
        _pair(("Город", _v(payload, "City")), ("Стадия", escape(stage))),
        _pair(("Бюро", _v(payload, "Specifier")), ("ГАП", _v(payload, "Lead Architect"))),
        _pair(("Объём по прайсу", f"{pl:,.0f} €".replace(",", " ") if isinstance(pl, (int, float)) else "")),
        _pair(("Альтернативы AW", escape(_ru("AW Alternatives", payload["AW Alternatives"]))
               if payload.get("AW Alternatives") else "")),
        _pair(("Уровень контакта", escape(_ru("Contact Level", payload["Contact Level"]))
               if payload.get("Contact Level") else "")),
        _pair(("Причина", _v(payload, "Lost Reason"))),
        _pair(("Заказ / проформа", _v(payload, "Order / Proforma №"))),
    ):
        if row:
            body.append(row)
    if rtype == "Update — Lost" and payload.get("Project Log"):
        body.append(escape(payload["Project Log"])[:300])
    if rtype in STATUS_REQUESTS and payload.get("Contact Level") == "Declared":
        body += ["", "⚠️ Уровень контакта — заявляю, без подтверждения. "
                     "Для предрегистрации рекомендуется запросить документы"]
    return body


def request_card(reg: dict, reg_id: str, author: str | None = None, analysis: str | None = None,
                 dealer: str | None = None) -> str:
    name = reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия"
    rtype = reg.get(Registrations.REQUEST_TYPE) or "—"
    try:
        payload = json.loads(reg.get(Registrations.PAYLOAD) or "{}")
    except ValueError:
        payload = {}

    lines = [f"🛠 <b>{_title(rtype, payload)}</b>"]
    if dealer or author:
        who = f"<b>{escape(dealer)}</b>" if dealer else ""
        if author:
            who = f"{who} · {escape(author)}" if who else escape(author)
        lines.append(f"👤 {who}")
    lines.append(f"🏗 <b>{escape(name)}</b>")

    body = _card_body(rtype, payload)
    if body:
        lines += [""] + body
    if analysis:
        lines += ["", "🔎 " + escape(analysis)]
    # ID заявки в тексте не показываем — запись открывается кнопкой «Открыть в Airtable»
    return "\n".join(lines)


async def cmd_inbox(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update, ctx):
        return
    repo = ctx.bot_data["repo"]
    recs = await asyncio.to_thread(repo.pending_without_decision)
    recs = [rec for rec in recs if not await _close_if_orphan_reply(repo, rec)]
    if not recs:
        await update.effective_chat.send_message("🛠 В Inbox нет заявок без решения.")
        return
    await update.effective_chat.send_message(f"🛠 Заявок без решения: {len(recs)}")
    for rec in recs:
        await update.effective_chat.send_message(
            request_card(rec["fields"], rec["id"], await _author(repo, rec["fields"]),
                         rec["fields"].get(Registrations.ANALYSIS), await _dealer(repo, rec["fields"])),
            parse_mode=ParseMode.HTML,
            reply_markup=decision_markup(ctx, rec["id"], await merge_candidates(repo, rec["fields"])),
        )


async def merge_candidates(repo, reg: dict) -> list:
    """Совпадения для кнопок «Слить с»: только у новой заявки без Target Project."""
    if reg.get(Registrations.TARGET_PROJECT) or not str(reg.get(Registrations.REQUEST_TYPE) or "").startswith("New"):
        return []
    projects = await asyncio.to_thread(repo.dedup_projects)
    found = dedup.find(_payload(reg), reg.get(Registrations.DEALER_PROJECT_NAME) or "", projects)
    return [(c.project_id, c.name) for c in found]


async def _dealer(repo, reg: dict) -> str | None:
    ids = reg.get(Registrations.DEALER) or []
    names = [await asyncio.to_thread(repo.company_name, c) for c in ids]
    return ", ".join(names) or None


async def _author(repo, reg: dict) -> str | None:
    ids = reg.get(Registrations.SUBMITTED_BY) or []
    return await asyncio.to_thread(repo.person_name, ids[0]) if ids else None


async def _close_if_orphan_reply(repo, rec: dict) -> bool:
    """Ответ дилера, чья исходная заявка уже решена, — закрыть молча. True — закрыт."""
    original = _payload(rec["fields"]).get("In Reply To")
    if not original:
        return False
    try:
        orig = await asyncio.to_thread(repo.registration_fields, original)
    except Exception:
        return False
    decided = orig.get(Registrations.DECISION)
    if not decided or decided == Decision.NEED_INFO:
        return False
    target = orig.get(Registrations.TARGET_PROJECT) or []
    await asyncio.to_thread(repo.close_reply, rec["id"], target[0] if target else None)
    return True


def _payload(reg: dict) -> dict:
    try:
        data = json.loads(reg.get(Registrations.PAYLOAD) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


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
    parts = rest.split(":")
    reg_id = parts[0] if parts else ""
    project_id = parts[1] if len(parts) > 1 else ""
    reply_id = parts[2] if len(parts) > 2 else ""
    pending = _pending(ctx)

    if action == ACTION_CANCEL:
        await drop_pending(update, ctx)
        await chat.send_message("🛠 Отменено.")
        return

    try:
        reg = await asyncio.to_thread(repo.registration_fields, reg_id)
    except Exception:
        await chat.send_message("Заявка не найдена.")
        return
    # Кнопка под ответом дилера — решение по исходной заявке, ответ закрывается вместе с ней
    original = _payload(reg).get("In Reply To")
    if original and action in (ACTION_ACCEPT, ACTION_DECLINE, ACTION_INFO):
        reply_id, reg_id = reg_id, original
        try:
            reg = await asyncio.to_thread(repo.registration_fields, reg_id)
        except Exception:
            await chat.send_message("Исходная заявка не найдена.")
            return
    name = escape(reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия")
    decided = reg.get(Registrations.DECISION)
    if decided and decided != Decision.NEED_INFO and action not in (ACTION_PICK, ACTION_DPICK, ACTION_DNEW):
        if reply_id:
            # Исходная заявка уже решена — ответ дилера закрываем, чтобы не висел в /inbox
            target = reg.get(Registrations.TARGET_PROJECT) or []
            await asyncio.to_thread(repo.close_reply, reply_id, target[0] if target else None)
            await chat.send_message(
                f"🛠 По «{name}» решение уже стоит: {escape(decided)}. "
                "Ответ дилера закрыт" + (" и добавлен в проект." if target else ".")
            )
            await _apply_now(ctx)
            return
        await chat.send_message(f"🛠 По «{name}» решение уже стоит: {escape(decided)}.")
        return

    if action in (ACTION_DECLINE, ACTION_INFO):
        pending[chat.id] = (action, reg_id, reply_id)
        await asyncio.to_thread(repo.set_awaiting, reg_id, action, reply_id)
        what = "отказа" if action == ACTION_DECLINE else "запроса"
        await chat.send_message(
            f"🛠 «{name}»: напиши текст {what} — он уйдёт дилеру как есть.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data=f"{CB_ADMIN}{ACTION_CANCEL}:")]]),
        )
        return

    if action == ACTION_ACCEPT:
        target = reg.get(Registrations.TARGET_PROJECT) or []
        if target:
            await asyncio.to_thread(repo.set_decision, reg_id, Decision.ACCEPT_MERGE, "")
            if reply_id:
                await asyncio.to_thread(repo.close_reply, reply_id, target[0])
            await _apply_now(ctx)
            return
        projects = await asyncio.to_thread(repo.dedup_projects)
        found = dedup.find(_payload(reg), reg.get(Registrations.DEALER_PROJECT_NAME) or "", projects)
        candidates = [(c.project_id, f"{c.name} ({', '.join(c.reasons)})") for c in found]
        tail = f":{reply_id}" if reply_id else ""
        rows = [[InlineKeyboardButton(f"В проект: {pname[:40]}",
                                      callback_data=f"{CB_ADMIN}{ACTION_PICK}:{reg_id}:{pid}{tail}")]
                for pid, pname in candidates]
        rows.append([InlineKeyboardButton("Создать новый проект",
                                          callback_data=f"{CB_ADMIN}{ACTION_NEW}:{reg_id}:-{tail}")])
        rows.append([InlineKeyboardButton("Отмена", callback_data=f"{CB_ADMIN}{ACTION_CANCEL}:")])
        hint = ("Похожие проекты в базе (по объекту, клиенту, адресу):" if candidates
                else "Похожих проектов по объекту, клиенту и адресу не нашёл.")
        await chat.send_message(f"🛠 «{name}»: в какой проект?\n{hint}", reply_markup=InlineKeyboardMarkup(rows))
        return

    if action == ACTION_DPICK and project_id:
        await asyncio.to_thread(repo.clear_awaiting, reg_id)
        await asyncio.to_thread(repo.update_registration, reg_id, {Registrations.TARGET_PROJECT: [project_id]})
        await _apply_now(ctx)
        return

    if action == ACTION_DNEW:
        pending[chat.id] = (ACTION_DNEW, reg_id, reply_id)
        await asyncio.to_thread(repo.set_awaiting, reg_id, ACTION_DNEW, reply_id)
        await chat.send_message(f"🛠 «{name}»: напиши название нового проекта латиницей. Передумал — нажми меню внизу.")
        return

    if action == ACTION_PICK and project_id:
        await asyncio.to_thread(repo.set_decision, reg_id, Decision.ACCEPT_MERGE, "", project_id)
        if reply_id:
            await asyncio.to_thread(repo.close_reply, reply_id, project_id)
        await _apply_now(ctx)
        return

    if action == ACTION_NEW:
        pending[chat.id] = (ACTION_NEW, reg_id, reply_id)
        await asyncio.to_thread(repo.set_awaiting, reg_id, ACTION_NEW, reply_id)
        await chat.send_message(
            f"🛠 «{name}»: напиши название нового проекта латиницей, как оно будет в Projects. "
            "Передумал — просто нажми меню внизу.",
        )


async def on_admin_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str, resume: bool = True) -> bool:
    """Текст админа в ответ на вопрос бота. True — обработано, дальше не передавать."""
    if not is_admin(update, ctx):
        return False
    pending = _pending(ctx)
    repo = ctx.bot_data["repo"]
    item = pending.pop(update.effective_chat.id, None)
    if not item and resume:
        # Бот мог перезапуститься, пока админ печатал, — берём ожидание из Airtable
        item = await asyncio.to_thread(repo.find_awaiting)
    if not item:
        return False  # бот ничего не ждёт — текст идёт дальше, в обычный диалог
    action, reg_id, reply_id = item
    chat = update.effective_chat
    if action != ACTION_REMIND_USER:
        await asyncio.to_thread(repo.clear_awaiting, reg_id)
    text = text.strip()
    if not text:
        pending[chat.id] = item
        await chat.send_message("🛠 Пустой текст — напиши ещё раз.")
        return True
    if action == ACTION_REMIND_USER:
        await ctx.bot_data["preview_reminder"](update, ctx, text)
        return True
    if action == ACTION_DPICK:
        # Ждём выбор проекта кнопкой, а не текст
        await asyncio.to_thread(repo.set_awaiting, reg_id, ACTION_DPICK, reply_id)
        await chat.send_message("🛠 Выбери проект кнопкой под сообщением об отказе.")
        return True
    if action == ACTION_DNEW:
        project_id = await asyncio.to_thread(repo.create_project, text)
        ctx.bot_data.setdefault("created_projects", set()).add(project_id)
        await asyncio.to_thread(repo.update_registration, reg_id, {Registrations.TARGET_PROJECT: [project_id]})
        await _apply_now(ctx)
        return True
    if action == ACTION_NEW:
        project_id = await asyncio.to_thread(repo.create_project, text)
        ctx.bot_data.setdefault("created_projects", set()).add(project_id)  # для итога «Проект создан»
        await asyncio.to_thread(repo.set_decision, reg_id, Decision.ACCEPT, "", project_id)
        if reply_id:
            await asyncio.to_thread(repo.close_reply, reply_id, project_id)
    else:
        decision = Decision.DECLINE if action == ACTION_DECLINE else Decision.NEED_INFO
        reg = await asyncio.to_thread(repo.registration_fields, reg_id)
        if (decision == Decision.DECLINE and reg.get(Registrations.REQUEST_TYPE) in UPGRADE_REQUESTS
                and not reg.get(Registrations.TARGET_PROJECT)):
            # Отказ всё равно фиксирует плейсмент уведомлением — сначала выбрать проект
            await asyncio.to_thread(repo.set_decision, reg_id, decision, text)
            await asyncio.to_thread(repo.set_awaiting, reg_id, ACTION_DPICK, reply_id)
            found = await merge_candidates(repo, reg)
            rows = [[InlineKeyboardButton(f"🔗 В проект: {name[:40]}",
                                          callback_data=f"{CB_ADMIN}{ACTION_DPICK}:{reg_id}:{pid}")]
                    for pid, name in found]
            rows.append([InlineKeyboardButton("🆕 Новый проект", callback_data=f"{CB_ADMIN}{ACTION_DNEW}:{reg_id}:-")])
            await chat.send_message(
                "🛠 Отказ записан. Плейсмент фиксируется уведомлением — в какой проект?",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return True
        if decision == Decision.NEED_INFO:
            # Повторный запрос: очищаем отметку, чтобы бот отправил новый текст
            await asyncio.to_thread(repo.update_registration, reg_id, {Registrations.BOT_PROCESSED: ""})
        await asyncio.to_thread(repo.set_decision, reg_id, decision, text)
        if reply_id:
            reg = await asyncio.to_thread(repo.registration_fields, reg_id)
            target = reg.get(Registrations.TARGET_PROJECT) or []
            await asyncio.to_thread(repo.close_reply, reply_id, target[0] if target else None)
    await _apply_now(ctx)
    return True
