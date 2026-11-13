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
from datetime import datetime, timezone
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from . import dedup, memo, routing
from .cards import STATUS_RU, dealer_status, name_ref, others_line, project_line
from .decisions import COMPANY_PICKS, OWN_DESIGN, plan as decision_plan
from .labels import DECISION_RU, date_ru, missing_text
from .scenarios.base import describe_items, format_value, is_vague
from .scenarios.new_project import QUESTIONS as _NEW_PROJECT_QUESTIONS
from .slots import VALENCIA
from .airtable_ids import Decision, Projects, Registrations

log = logging.getLogger(__name__)

CB_ADMIN = "ad:"  # ad:<действие>:<id заявки>[:<id проекта>]
BASE_URL = "https://airtable.com/{base}/{table}/{rec}"

ACTION_ACCEPT, ACTION_DECLINE, ACTION_INFO = "acc", "dec", "inf"
ACTION_PICK, ACTION_NEW, ACTION_CANCEL = "pick", "new", "cancel"
ACTION_REMIND_USER = "remind_user"  # ждём username для предпросмотра напоминания
# Отказ в пред/регистрации без проекта: куда зафиксировать уведомление
ACTION_DPICK, ACTION_DNEW = "dpick", "dnew"
# Блокировка из-за недостающих данных: отправить дилеру готовый запрос
ACTION_ASK = "ask"
# Название нового проекта — предложенный транслит одним нажатием
ACTION_NAME = "name"
# Принять с комментарием: сначала текст, потом обычный выбор проекта
ACTION_ACCEPT_NOTE = "accn"
ACTION_DECLINE_BARE = "decn"   # отказ без комментария
ACTION_CALL_OK, ACTION_CALL_OTHER = "cok", "cot"   # звонок, предложенный партнёром: подтвердить / другое время
ACTION_CALL_NONE = "cno"                           # звонок не нужен
ACTION_ASK_SEND = "asks"                           # готовый запрос дилеру — после просмотра
ACTION_SET_STATUS = "rst"                          # застрявшее решение: какой статус дать (ad:rst:<заявка>:<n|p|r>)
ACTION_ACCEPT_LOWER = "acl"                        # не хватает данных — принять этапом ниже
ACTION_NOTE_OK = "nok"                             # отправить предложенный комментарий как есть
ACTION_DECLINE_CANCEL = "dcx"                      # отмена записанного отказа (выбор проекта для уведомления)
ACTION_FINISH = "fin"                              # зависшее решение: довести до конца (ad:fin:<заявка>)
# Пошаговое повышение: «Пока оставить» и «Отказать» — с комментарием дилеру
ACTION_UP_KEEP, ACTION_UP_DENY, ACTION_UP_BARE = "upk", "upd", "upb"
ACTION_UP_VIEW = "upv"   # посмотреть проект перед решением — потом снова те же кнопки
# Повышение статуса без заявки: ad:up:<проект>:<менеджер>:pre|reg; список /upgrade и выбор пары
ACTION_UP, ACTION_UP_NO, ACTION_UP_LIST, ACTION_UP_PICK = "up", "upno", "upl", "ups"
# Подтверждение несмотря на незаполненные ответы: те же действия с флагом «точно»
FORCE = {"pickf": ACTION_PICK, "accf": ACTION_ACCEPT, "newf": ACTION_NEW}
# «Принять как предрегистрацию» вместо запрошенной регистрации: dp<x> → то же решение без переспроса
DOWNGRADE = {"dpn": "newf", "dpp": "pickf", "dpa": "accf"}
_DOWN_OF = {v: k for k, v in DOWNGRADE.items()}
_PRE_OF = {"New — Registration": "New — Pre-registration", "Upgrade — Registration": "Upgrade — Pre-registration"}
# Проект для слияния назван не латиницей — сначала новое название
ACTION_RENAME = "ren"
ACTION_COMPANY = "co"   # ad:co:<заявка>:<№ вопроса>:<id компании или new> — похожая компания в базе
# Откат статуса менеджера: ad:rb:<проект>:<менеджер> → на этап ниже (rbd) или снять (rbc), затем причина текстом
ACTION_RB, ACTION_RB_DOWN, ACTION_RB_CANCEL = "rb", "rbd", "rbc"
_ROLLBACK = {ACTION_RB_DOWN: "down", ACTION_RB_CANCEL: "cancel"}
_LOWER_RU = {"registered": "предрегистрацию", "pre_registered": "уведомление"}
_STATUS_CODE = {"n": "Notification", "p": "Pre-registration", "r": "Registration"}
_STATUS_CODE_RU = {"n": "уведомление", "p": "предрегистрацию", "r": "регистрацию"}
UPGRADE_PAGE = 8
_TARGET_RU = {"pre": "предрегистрацию", "reg": "регистрацию"}
UPGRADE_REQUESTS = {"New — Pre-registration", "New — Registration",
                    "Upgrade — Pre-registration", "Upgrade — Registration"}


def is_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверяющий: Егор (ADMIN_CHAT_ID) или Лола (@aw_eeu, когда она уже писала боту)."""
    return update.effective_chat is not None and str(update.effective_chat.id) in routing.reviewer_chats(ctx.bot_data)


def _mark_decider(ctx: ContextTypes.DEFAULT_TYPE, update: Update, reg_id: str) -> None:
    """Кто нажал — ему полная карточка итога, второму проверяющему короткая строка."""
    who = routing.who_by_chat(ctx.bot_data, update.effective_chat.id)
    if who and reg_id:
        ctx.bot_data.setdefault("decided_by", {})[reg_id] = who


async def reviewer_of(ctx: ContextTypes.DEFAULT_TYPE, reg: dict) -> str:
    """Кому по маршруту достаётся заявка (с учётом того, есть ли Лола в боте)."""
    types = await asyncio.to_thread(ctx.bot_data["repo"].company_types, reg.get(Registrations.DEALER) or [])
    return routing.resolve(ctx.bot_data, routing.route(types))[0]


# Ожидания ввода без заявки (откат, «Пока оставить», «Отказать», другое время звонка) — в памяти бота (memo)
_MEMO_ACTIONS = {ACTION_RB_DOWN, ACTION_RB_CANCEL, ACTION_UP_KEEP, ACTION_UP_DENY, ACTION_CALL_OTHER,
                 "jrej", "jco"}   # + подключение нового человека (bot/onboarding.py)


async def drop_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> tuple | None:
    """Админ нажал меню или «Отмена» — незавершённый ввод отменяется (и в Airtable). Возвращает, что отменили."""
    if not is_admin(update, ctx):
        return None
    chat_id = update.effective_chat.id
    item = _pending(ctx).pop(chat_id, None)
    repo = ctx.bot_data["repo"]
    saved = await memo.pop(ctx.bot_data, "waits", str(chat_id))
    if not item and saved:
        item = tuple(saved)
    if not item:
        item = await asyncio.to_thread(repo.find_awaiting, chat_id)
    if item and item[0] != ACTION_REMIND_USER and item[0] not in _MEMO_ACTIONS:
        await asyncio.to_thread(repo.clear_awaiting, item[1])
    return item


def _pending(ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    return ctx.bot_data.setdefault("admin_pending", {})


async def _wait(ctx: ContextTypes.DEFAULT_TYPE, chat_id, item: tuple) -> None:
    """Ожидание ввода без заявки — в память и в Airtable (переживает перезапуск)."""
    _pending(ctx)[chat_id] = item
    await memo.set_item(ctx.bot_data, "waits", str(chat_id), list(item))


def cancel_row() -> list:
    return [InlineKeyboardButton("Отмена", callback_data=f"{CB_ADMIN}{ACTION_CANCEL}:")]


# Новая заявка на предрег./рег. проверяется по шагам: уведомление (нет дублей) → предрегистрация → регистрация
STEPWISE = {"New — Pre-registration": "pre", "New — Registration": "reg"}
_STEP_PHASE = {"pre": 2, "reg": 3}


async def _start_stepwise(ctx: ContextTypes.DEFAULT_TYPE, reg_id: str, reg: dict) -> None:
    """Принимаем сначала как уведомление; следующие шаги бот предложит после записи (main: offer_step)."""
    rtype = reg.get(Registrations.REQUEST_TYPE)
    if rtype not in STEPWISE:
        return
    await asyncio.to_thread(ctx.bot_data["repo"].update_registration, reg_id,
                            {Registrations.REQUEST_TYPE: "New — Notification"})
    reg[Registrations.REQUEST_TYPE] = "New — Notification"
    await memo.set_item(ctx.bot_data, "stepwise", reg_id, {"final": STEPWISE[rtype], "payload": _payload(reg)})


def step_offer(ref: str, name: str, step: str, final: str, payload: dict, missing: str = "",
               manager: str = "") -> str:
    """«Шаг 2 из 3 · предрегистрация?» — кто просил, с пустыми ответами этой фазы и недостающим по-русски."""
    total = 3 if final == "reg" else 2
    n = 2 if step == "pre" else 3
    asked = "регистрацию" if final == "reg" else "предрегистрацию"
    weak = weak_answers(payload, _STEP_PHASE[step])
    who = escape(manager) if manager else "Дилер"
    lines = [f"🛠 <b>Шаг {n} из {total}</b> · <code>{escape(name_ref(name, ref))}</code>",
             f"{who} просил(а) {asked}. Уведомление уже записано. Подтверждаем {_TARGET_RU[step]}?"]
    if weak:
        lines += ["Без ответа:"] + [f"• {escape(w)}" for w in weak]
    if missing and missing_text(missing):
        lines.append(f"По проекту не хватает: {escape(missing_text(missing))}")
    lines.append("«Пока оставить» и «Отказать» — дилеру уйдёт сообщение, сначала спрошу комментарий.")
    return "\n".join(lines)


def upgrade_markup(project_id: str, person_id: str, target: str) -> InlineKeyboardMarkup:
    what = "предрегистрации" if target == "pre" else "регистрации"
    tail = f"{project_id}:{person_id}:{target}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⬆️ Повысить до {what}", callback_data=f"{CB_ADMIN}{ACTION_UP}:{tail}")],
        [InlineKeyboardButton("❌ Отказать", callback_data=f"{CB_ADMIN}{ACTION_UP_DENY}:{tail}"),
         InlineKeyboardButton("👁 Посмотреть проект", callback_data=f"{CB_ADMIN}{ACTION_UP_VIEW}:{tail}")],
        [InlineKeyboardButton("Пока оставить", callback_data=f"{CB_ADMIN}{ACTION_UP_KEEP}:{tail}")],
    ])


_SHORT_STATUS = {"reg": "регистрация", "pre": "предрегистрация", "notified": "уведомление"}


def rollback_markup(project_id: str, person_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "↩️ Откатить / аннулировать", callback_data=f"{CB_ADMIN}{ACTION_RB}:{project_id}:{person_id}")]])


async def _upgrade_pairs(ctx: ContextTypes.DEFAULT_TYPE) -> list[tuple]:
    """(проект, менеджер, статус, подпись) — кого можно повысить: уведомление / предрегистрация, не Simplified."""
    repo = ctx.bot_data["repo"]
    names: dict = ctx.bot_data.setdefault("person_names", {})
    out = []
    for r in await asyncio.to_thread(repo.projects_with_managers):
        f = r["fields"]
        if f.get(Projects.PPR_TRACK) == "Simplified (<30k)":
            continue
        ref, pname = f.get(Projects.PPR_REF) or "", (f.get(Projects.NAME) or "")[:30]
        rows = [(p, "reg") for p in f.get(Projects.REGISTERED) or []]
        rows += [(p, "pre") for p in f.get(Projects.PRE_REGISTERED) or []]
        rows += [(p, "notified") for p in f.get(Projects.NOTIFIED) or []]
        for person, status in rows:
            if person not in names:
                names[person] = (await asyncio.to_thread(repo.person_name, person)) or person
            short = names[person].split()[0] if names[person] else "менеджер без имени"
            label = f"{name_ref(pname, ref)} · {short} · {_SHORT_STATUS[status]}"
            out.append((r["id"], person, status, label))
    return out


async def cmd_upgrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/upgrade — повысить менеджера в проекте: проект → статус → проверка → перестановка и карточка дилеру."""
    if not is_admin(update, ctx):
        return
    await _show_upgrade_list(update.effective_chat, ctx, 0)


async def _show_upgrade_list(chat, ctx: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    pairs = await _upgrade_pairs(ctx)
    if not pairs:
        await chat.send_message("🛠 Ни у одного менеджера нет статуса в действующих проектах — менять нечего.")
        return
    pages = (len(pairs) + UPGRADE_PAGE - 1) // UPGRADE_PAGE
    page = max(0, min(page, pages - 1))
    rows = [[InlineKeyboardButton(label[:64], callback_data=f"{CB_ADMIN}{ACTION_UP_PICK}:{pid}:{person}")]
            for pid, person, _, label in pairs[page * UPGRADE_PAGE:(page + 1) * UPGRADE_PAGE]]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("← Назад", callback_data=f"{CB_ADMIN}{ACTION_UP_LIST}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Дальше →", callback_data=f"{CB_ADMIN}{ACTION_UP_LIST}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(cancel_row())
    await chat.send_message(f"🛠 Чей статус изменить — повысить или откатить? Строка: проект · менеджер · "
                            f"текущий статус ({page + 1}/{pages})", reply_markup=InlineKeyboardMarkup(rows))


_LOWER_STAGE = {"Registration": ("p", "предрегистрацию"), "Pre-registration": ("n", "уведомление")}


def blocked_text(admin_text: str, ask: str | None = None) -> str:
    """Застрявшее решение: причина по-русски, готовый запрос дилеру целиком, «Выбери:»."""
    lines = [admin_text]
    if ask:
        lines += ["", "Дилеру уйдёт:", f"<i>{escape(ask)}</i>"]
    lines += ["", "Выбери:"]
    return "\n".join(lines)


def blocked_markup(reg_id: str, kind: str, reg: dict, candidates: list | None = None,
                   requested_status: str | None = None) -> InlineKeyboardMarkup | None:
    """Р5: застрявшее решение — кнопками в боте, не через Airtable."""
    rows: list = []
    declined = reg.get(Registrations.DECISION) == Decision.DECLINE
    if kind == "no_project":
        pick, new = (ACTION_DPICK, ACTION_DNEW) if declined else (ACTION_PICK, ACTION_NEW)
        rows += [[InlineKeyboardButton(f"В проект: {title[:50]}", callback_data=f"{CB_ADMIN}{pick}:{reg_id}:{pid}")]
                 for pid, title in candidates or []]
        rows.append([InlineKeyboardButton("Создать новый проект", callback_data=f"{CB_ADMIN}{new}:{reg_id}:-")])
    elif kind == "no_status":
        rows.append([InlineKeyboardButton(label.capitalize(), callback_data=f"{CB_ADMIN}{ACTION_SET_STATUS}:{reg_id}:{c}")
                     for c, label in (("n", "уведомление"), ("p", "предрегистрация"), ("r", "регистрация"))])
    elif kind == "missing":
        rows.append([InlineKeyboardButton("📨 Отправить запрос", callback_data=f"{CB_ADMIN}{ACTION_ASK_SEND}:{reg_id}")])
        rows.append([InlineKeyboardButton("✏️ Изменить текст", callback_data=f"{CB_ADMIN}{ACTION_INFO}:{reg_id}")])
        lower = _LOWER_STAGE.get(requested_status or "")
        if lower:
            rows.append([InlineKeyboardButton(f"Принять как {lower[1]}",
                                              callback_data=f"{CB_ADMIN}{ACTION_SET_STATUS}:{reg_id}:{lower[0]}")])
    elif kind in ("reg_taken", "pre_full"):
        rows.append([InlineKeyboardButton("Принять как уведомление",
                                          callback_data=f"{CB_ADMIN}{ACTION_SET_STATUS}:{reg_id}:n")])
        rows.append([InlineKeyboardButton("❌ Отклонить", callback_data=f"{CB_ADMIN}{ACTION_DECLINE}:{reg_id}")])
    elif kind == "no_author":
        rows.append([InlineKeyboardButton("❌ Отклонить заявку", callback_data=f"{CB_ADMIN}{ACTION_DECLINE}:{reg_id}")])
    elif kind == "no_note":
        rows.append([InlineKeyboardButton("✏️ Написать вопрос дилеру", callback_data=f"{CB_ADMIN}{ACTION_INFO}:{reg_id}")])
    if not rows:
        return None
    rows.append(cancel_row())
    return InlineKeyboardMarkup(rows)


def record_url(ctx: ContextTypes.DEFAULT_TYPE, reg_id: str) -> str:
    return BASE_URL.format(base=ctx.bot_data["base_id"], table="tbl4lKbYhOkVIYNHQ", rec=reg_id)


def decision_markup(ctx: ContextTypes.DEFAULT_TYPE, reg_id: str,
                    candidates: list | None = None) -> InlineKeyboardMarkup:
    """Два решения: «Принять» и «Отклонить». После нажатия — комментарий партнёру или «без комментария»;
    в какой проект (совпадения, новый) бот спросит после этого."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Принять", callback_data=f"{CB_ADMIN}{ACTION_ACCEPT_NOTE}:{reg_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"{CB_ADMIN}{ACTION_DECLINE}:{reg_id}"),
    ]])


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


def accept_phrase(rtype: str, name: str, payload: dict | None = None) -> str:
    """«Принимаем дополнение по «X»» — по типу заявки (name уже экранировано)."""
    payload = payload or {}
    what = f"«{name}»"
    if rtype.startswith("New — "):
        stage = REQUEST_LABELS.get(rtype, "уведомление")
        return f"Принимаем новый проект {what} как {_ACC.get(stage, stage)}"
    if rtype in ("Upgrade — Pre-registration", "Upgrade — Registration"):
        return f"Принимаем {REQUEST_LABELS[rtype]} по {what}"
    quote = payload.get("Quotation / Proforma №")
    phrases = {
        "Update — fields": f"Принимаем дополнение по {what}",
        "Update — news": f"Принимаем новость по {what}",
        "Update — info only": f"Принимаем обновление по {what}",
        "Update — Won": f"Принимаем: проект {what} выигран",
        "Update — Lost": f"Принимаем: проект {what} проигран",
        "Quotation — Simplified": (f"Принимаем квотацию № {escape(str(quote))} по {what} (упрощённая схема)"
                                   if quote else f"Принимаем квотацию по {what} (упрощённая схема)"),
    }
    return phrases.get(rtype, f"Принимаем заявку по {what}")


def _title(rtype: str, payload: dict, author: str | None = None, name: str = "", asked: str = "") -> str:
    if payload.get("In Reply To"):
        who = escape(author or "Дилер")
        head = f"💬 {who} ответил(а) на твой запрос по «{escape(name)}»"
        return head + (f": «{escape(asked)}»" if asked else "")
    if rtype == "Quotation — Simplified" and payload.get("Quotation / Proforma №"):
        return f"🧾 Упрощённая схема · квотация № {escape(str(payload['Quotation / Proforma №']))}"
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
    "Declared": "Контакт есть, подтвердить пока нечем",
    "Indirect (bureau docs)": "Документы бюро (контакт с клиентом не подтверждают)",
    "Verifiable (email/contact)": "Переписка или прямой контакт с клиентом",
    "Joint meeting with AW": "Готов к встрече с клиентом вместе с Andreu World",
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


_EXTRA_TITLES = {"Bureau": "Архбюро (AW не закладывало)", "Not Known Yet": "Дилер пока не знает",
                 "Order / Proforma №": "Заказ / проформа №", "Lost Reason": "Причина проигрыша",
                 "Quotation / Proforma №": "Квотация AW №", "Extra Materials": "Материалы",
                 "Dealer News": "Новость", "Contact Evidence": "Подтверждение контакта",
                 "Area Source": "Откуда площадь", "Country": "Страна", "City": "Город"}

# «Нет» здесь — полноценный ответ, а не пропуск


def weak_answers(payload: dict, phase: int | None = None) -> list[str]:
    """Вопросы заявки без настоящего ответа: пропущено, «нет», «пока неизвестно» (phase — только этой фазы)."""
    out = []
    for key, q in _QUESTIONS.items():
        if key.startswith("_") or key not in payload or (phase and q.phase != phase):
            continue  # не спрашивали — не ответ
        value = payload[key]
        if value in (None, "", []):
            out.append(q.title)
        elif isinstance(value, str) and is_vague(key, value):
            out.append(f"{q.title} — «{value.strip()[:40]}»")
    return out


def _ru(key: str, value) -> str:
    """Значение поля по-русски: варианты Airtable → подписи, числа, даты, файлы."""
    if key == "Contact Level":
        return CONTACT_RU.get(value, str(value))
    if key == "AW Alternatives":
        return ALT_RU.get(value, str(value))
    if key == "Project Stage":
        return STAGE_LABELS.get(value, str(value))
    if key == "Lost Reason":
        from .decisions import LOST_REASON_RU
        return LOST_REASON_RU.get(value, str(value))
    if key == "Contact Evidence" and isinstance(value, str):
        return "пришлёт позже — контакт пока без подтверждения"
    q = _QUESTIONS.get(key)
    if q is not None:
        return format_value(q, value)
    if isinstance(value, list):
        return describe_items(value)
    return str(value)


CALL_KEYS = ("Call Slot 15", "Call Slot 30")


def call_slots(payload: dict) -> list[tuple[str, str]]:
    """Предложенные партнёром звонки: [(«Звонок 15 минут», «05.11.2026 Чт, 14:00–16:00 Мск (Валенсия …)»)]."""
    out = []
    for key in CALL_KEYS:
        value = payload.get(key)
        if value not in (None, "", []):
            q = _QUESTIONS.get(key)
            out.append((q.title if q else key, str(value)))
    return out


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
                title = _QUESTIONS[k].title if k in _QUESTIONS else _EXTRA_TITLES.get(k, k)
                body.append(f"• {escape(title)}: {escape(_ru(k, v))[:300]}")
        return body
    # Всё, что партнёр ответил боту, — полностью, в порядке анкеты; звонок — отдельной строкой сверху
    calls = call_slots(payload)
    for label, value in calls:
        body.append(f"📞 <b>{escape(label)}: {escape(value)}</b>")
    if calls:
        body.append("")
    shown = set(CALL_KEYS)
    for key, q in _QUESTIONS.items():
        if key in shown or key not in payload or key.startswith("_"):
            continue
        shown.add(key)
        value = payload[key]
        text = "пропущено" if value in (None, "", []) else _ru(key, value)
        mark = " ⚠️" if isinstance(value, str) and q.label_for(value) is None and is_vague(key, value) else ""
        body.append(f"• {escape(q.title)}: {escape(text)}{mark}")
    for key, value in payload.items():
        if key in shown or key.startswith("_") or key in _META_KEYS or key == "Dealer News" or value in (None, "", []):
            continue
        title = _EXTRA_TITLES.get(key, key)
        body.append(f"• {escape(title)}: {escape(_ru(key, value))}")
    if rtype == "Update — Lost" and payload.get("Project Log"):
        body.append(escape(payload["Project Log"])[:300])
    if rtype.startswith("New") and rtype in STATUS_REQUESTS and payload.get("Contact Level") in (None, "", "Declared"):
        body += ["", "ℹ️ Контакт с клиентом не подтверждён: для предрегистрации он не нужен, "
                     "для регистрации нужен подтверждённый."]
    return body


def accept_effect(rtype: str, payload: dict, author: str | None = None) -> str:
    """Что изменится в базе при «Принять» (правило 1.5)."""
    who = escape(author or "дилер")
    if payload.get("In Reply To"):
        return "ответ запишу в журнал проекта и закрою вместе с исходной заявкой."
    return {
        "New — Notification": f"создам новый проект или добавлю в похожий; {who} получит уведомление о плейсменте.",
        "New — Pre-registration": f"проверим по шагам: сначала запишу уведомление ({who}), "
                                  "потом спрошу про предрегистрацию.",
        "New — Registration": f"проверим по шагам: сначала запишу уведомление ({who}), "
                              "потом спрошу про предрегистрацию и регистрацию.",
        "Upgrade — Pre-registration": f"{who} получит предрегистрацию в проекте, срок — месяц с сегодняшнего дня.",
        "Upgrade — Registration": f"{who} получит регистрацию в проекте, срок — месяц с сегодняшнего дня.",
        "Update — fields": "запишу новые данные в пустые поля проекта (объём и стадию — заменю), статус не меняется.",
        "Update — info only": "запишу новые данные в проект, статус не меняется.",
        "Update — news": "запишу новость в журнал проекта, статус не меняется.",
        "Update — Won": "проект станет «выигран», вероятность 100 %"
                        + (f", номер заказа {escape(str(payload['Order / Proforma №']))} — в квотации проекта."
                           if payload.get("Order / Proforma №") else "."),
        "Update — Lost": "проект станет «проигран», вероятность 0 %, причина — в журнал проекта.",
        "Quotation — Simplified": (f"запишу квотацию № {escape(str(payload['Quotation / Proforma №']))} в проект, "
                                   f"{who} — в уведомлении; защита на срок квотации."
                                   if payload.get("Quotation / Proforma №") else
                                   "передам спецификацию на квотацию, статус не меняется."),
    }.get(rtype, "запишу данные в проект.")


def dealer_gets(rtype: str, payload: dict, name: str) -> str:
    """Что получит дилер при «Принять» (правило 1.5): начало текста; комментарий — твой, если напишешь."""
    title = escape(name)
    if payload.get("In Reply To"):
        return f"«Твой ответ по проекту {title} получили и добавили в проект.»"
    if rtype == "Update — Won":
        return f"«🏆 Записали: проект {title} выигран. Спасибо!»"
    if rtype == "Update — Lost":
        return f"«Записали: проект {title} проигран, причина — …»"
    if rtype in STATUS_REQUESTS or rtype == "New — Notification":
        return ("карточку «✅ Заявка принята»: проект с референсом, его статус, есть ли на объекте другие партнёры, "
                "срок статуса и чего не хватает до следующего этапа.")
    return "карточку «✅ Заявка принята»: проект с референсом, что внесли и что осталось до следующего этапа."


UPGRADE_REQUESTS = {"Upgrade — Pre-registration", "Upgrade — Registration"}


def project_digest(project: dict, manager: str, companies: dict) -> list[str]:
    """Что есть в проекте на момент запроса повышения: поля этапа, досье, кто ещё на объекте, хвост лога.
    companies — {id компании: название} для End Client / Specifier."""
    def names(field):
        return ", ".join(escape(companies.get(c, c)) for c in project.get(field) or [])
    exw = project.get(Projects.EXW_POTENTIAL)
    order = str(project.get(Projects.EXPECTED_ORDER_DATE) or "")[:10]
    files = len(project.get(Projects.DOSSIER_FILES) or [])
    links = len([x for x in str(project.get(Projects.DOSSIER_LINKS) or "").splitlines() if x.strip()])
    part = project.get(Projects.OBJECT_PART)
    obj = escape(str(project.get(Projects.OBJECT_NAME) or "")) + (f" · {escape(str(part))}" if part else "")
    lines = [f"🏗 {project_line(project)}", "", "<b>Что есть в проекте</b>"]
    for row in (
        _pair(("Клиент", names(Projects.END_CLIENT))),
        _pair(("Объект", obj)),
        _pair(("Город", escape(str(project.get(Projects.CITY) or ""))),
              ("Стадия", escape(_ru("Project Stage", project[Projects.PROJECT_STAGE]))
               if project.get(Projects.PROJECT_STAGE) else "")),
        _pair(("Адрес", escape(str(project.get(Projects.OBJECT_ADDRESS) or "")))),
        _pair(("Бюро", names(Projects.SPECIFIER))),
        _pair(("EXW", f"{exw:,.0f} €".replace(",", " ") if isinstance(exw, (int, float)) and exw else ""),
              ("по прайсу", f"{exw / 0.45:,.0f} €".replace(",", " ") if isinstance(exw, (int, float)) and exw else "")),
        _pair(("Заказ", date_ru(order) if order else "")),
        _pair(("Уровень контакта", escape(_ru("Contact Level", project[Projects.CONTACT_LEVEL]))
               if project.get(Projects.CONTACT_LEVEL) else "")),
        _pair(("Альтернативы AW", escape(_ru("AW Alternatives", project[Projects.AW_ALTERNATIVES]))
               if project.get(Projects.AW_ALTERNATIVES) else "")),
        _pair(("Параллельные регистрации", escape(str(project.get(Projects.PARALLEL_REGISTRATIONS) or ""))[:200])),
        _pair(("Досье", f"файлов {files}, ссылок {links}" if files or links else "пусто")),
        _pair(("Квотация", escape(str(project.get(Projects.QUOTATION) or "")))),
    ):
        if row:
            lines.append(row)
    missing = missing_text(project.get(Projects.PPR_NEXT_MISSING))
    lines.append(f"⚠️ Не хватает: {escape(missing)}" if missing else "✔️ Условия следующего этапа выполнены")
    lines += [ln.replace("🔢 Твой номер", "🔢 Номер дилера")
              for ln in others_line(project, manager, dealer_status(project, manager))]
    entries = [e for e in re.split(r"\n\s*\n", str(project.get(Projects.PROJECT_LOG) or "").strip()) if e.strip()]
    if entries:
        head = "<b>Журнал проекта</b>" + (f" — последние 5 из {len(entries)}" if len(entries) > 5 else "")
        lines += ["", head, escape("\n\n".join(entries[-5:]))]
    return lines


async def upgrade_digest(repo, reg: dict) -> list[str]:
    """Для запросов повышения — сводка по проекту (заявка сама по себе пустая: только кнопка)."""
    target = reg.get(Registrations.TARGET_PROJECT) or []
    if reg.get(Registrations.REQUEST_TYPE) not in UPGRADE_REQUESTS or not target:
        return []
    try:
        project = await asyncio.to_thread(repo.project_fields, target[0])
        ids = (project.get(Projects.END_CLIENT) or []) + (project.get(Projects.SPECIFIER) or [])
        companies = {c: await asyncio.to_thread(repo.company_name, c) for c in ids}
        manager = (reg.get(Registrations.SUBMITTED_BY) or [""])[0]
        return project_digest(project, manager, companies)
    except Exception:
        log.exception("Сводка проекта для запроса повышения")
        return []


def request_card(reg: dict, reg_id: str, author: str | None = None, analysis: str | None = None,
                 dealer: str | None = None, extra: list[str] | None = None, asked: str = "",
                 failed_files: list[str] | None = None) -> str:
    """Карточка-вопрос проверяющему: что пришло, что изменится при «Принять», что получит дилер (правило 1.5)."""
    name = reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия"
    rtype = reg.get(Registrations.REQUEST_TYPE) or "—"
    try:
        payload = json.loads(reg.get(Registrations.PAYLOAD) or "{}")
    except ValueError:
        payload = {}

    lines = [f"🛠 <b>{_title(rtype, payload, author, name, asked)}</b>"]
    if dealer or author:
        who = f"<b>{escape(dealer)}</b>" if dealer else ""
        if author:
            who = f"{who} · {escape(author)}" if who else escape(author)
        lines.append(f"👤 {who}")
    if not extra:
        lines.append(f"🏗 <b>{escape(name)}</b>")

    body = _card_body(rtype, payload)
    if body:
        lines += [""] + body
    if extra:
        lines += extra
    if failed_files:
        lines += ["", f"⚠️ Не загрузились файлы ({len(failed_files)}): {escape(', '.join(failed_files))} — "
                      "дилера попросил прислать их ссылкой."]
    if analysis:
        lines += ["", "🔎 " + escape(analysis)]
    lines += ["", f"<b>При «Принять»:</b> {accept_effect(rtype, payload, author)}",
              f"<b>Дилеру уйдёт:</b> {dealer_gets(rtype, payload, name)} При «Отклонить» — «Заявка по проекту "
              f"{escape(name)} не принята» и как подать заново. Комментарий — твой, если напишешь."]
    # ID заявки в тексте не показываем
    return "\n".join(lines)


TG_LIMIT = 3800   # запас до 4096 символов сообщения Telegram


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Длинная карточка — на части по строкам (теги в карточке не переходят через строку)."""
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:   # одна очень длинная строка — режем по месту
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts or [""]


MAX_FILES_SHOWN = 10


def payload_files(payload: dict) -> list[tuple[str, dict]]:
    """Файлы дилера из заявки (то, что он прислал боту): [(о чём, файл)]."""
    out = []
    for key, value in payload.items():
        if isinstance(value, list):
            title = _QUESTIONS[key].title if key in _QUESTIONS else _EXTRA_TITLES.get(key, "Файл к заявке")
            out += [(title, it) for it in value if isinstance(it, dict) and it.get("kind") == "file" and it.get("file_id")]
    return out


async def send_files(bot, chat_id, payload: dict, project: str = "") -> str:
    """Сами файлы дилера — проверяющему перед карточкой, чтобы видеть, что прислали.
    Возвращает строку для карточки («📎 Файлы — выше») или пустую."""
    files = payload_files(payload)
    sent = 0
    for title, it in files[:MAX_FILES_SHOWN]:
        caption = " · ".join(x for x in (f"📎 {title}", project) if x)[:1000]
        # Картинку дилер мог прислать «файлом», а не фото: тогда как фото её не отправить — шлём документом
        tries = (bot.send_photo, bot.send_document) if str(it.get("mime") or "").startswith("image/") \
            else (bot.send_document,)
        for send in tries:
            try:
                await send(chat_id, it["file_id"], caption=caption)
                sent += 1
                break
            except Exception:
                log.warning("Не удалось переслать файл %s проверяющему (%s)", it.get("name"), send.__name__,
                            exc_info=True)
    if not files:
        return ""
    lines = []
    if sent:
        lines.append(f"📎 Файлы дилера ({sent}) — сообщениями выше.")
    if len(files) > sent:
        lines.append(f"📎 Ещё файлов: {len(files) - sent} — переслать не получилось, они во вложениях заявки.")
    return "\n" + "\n".join(lines)


async def send_card(bot, chat_id, text: str, markup=None) -> None:
    """Карточка целиком, без сокращений: части подряд, кнопки — под последней."""
    parts = split_message(text)
    for i, part in enumerate(parts):
        await bot.send_message(chat_id, part, parse_mode=ParseMode.HTML,
                               reply_markup=markup if i == len(parts) - 1 else None)


def _call_text(reg: dict, idx: int) -> tuple[str, str, str]:
    """(подпись, время, название проекта) звонка №idx из заявки."""
    calls = call_slots(_payload(reg))
    label, value = calls[idx] if 0 <= idx < len(calls) else ("Звонок", "—")
    return label, value, reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия"


CB_DEALER_CALL = "dc:"   # дилер отвечает на другое время звонка: dc:ok|other:<заявка>:<№>


def call_offer_markup(reg_id: str, idx: int) -> InlineKeyboardMarkup:
    tail = f"{reg_id}:{idx}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить", callback_data=f"{CB_ADMIN}{ACTION_CALL_OK}:{tail}"),
         InlineKeyboardButton("🕑 Другое время", callback_data=f"{CB_ADMIN}{ACTION_CALL_OTHER}:{tail}")],
        [InlineKeyboardButton("Звонок не нужен", callback_data=f"{CB_ADMIN}{ACTION_CALL_NONE}:{tail}")],
    ])


async def send_call_offer(bot, chat_id, reg_id: str, reg: dict, author: str | None) -> None:
    """Партнёр выбрал время звонка — отдельным сообщением, чтобы не потерялось: подтвердить или другое время."""
    for idx, (label, value) in enumerate(call_slots(_payload(reg))):
        name = reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия"
        await bot.send_message(
            chat_id,
            f"📞 <b>{escape(author or 'Дилер')} предлагает звонок</b> · {escape(label.lower())}\n"
            f"🏗 {escape(name)}\n🕑 <b>{escape(value)}</b>\n"
            "Подтвердишь — дилеру уйдёт подтверждение, а я напомню тебе за час до звонка.",
            parse_mode=ParseMode.HTML,
            reply_markup=call_offer_markup(reg_id, idx),
        )


async def _tell_partner(ctx: ContextTypes.DEFAULT_TYPE, reg: dict, text: str, markup=None) -> bool:
    repo = ctx.bot_data["repo"]
    chats = await asyncio.to_thread(repo.chat_ids_for_people, reg.get(Registrations.SUBMITTED_BY) or [])
    for chat_id in chats:
        await ctx.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
    return bool(chats)


async def confirm_call(bot_data: dict, reg_id: str, idx: int, value: str) -> None:
    """Звонок подтверждён — в память бота (утренний список и напоминание за час — только по подтверждённым)."""
    await memo.set_item(bot_data, "calls_confirmed", f"{reg_id}:{idx}", value)


async def asked_of(repo, reg: dict) -> str:
    """Для ответа дилера — твой исходный вопрос (Decision Note исходной заявки)."""
    original = _payload(reg).get("In Reply To")
    if not original:
        return ""
    try:
        orig = await asyncio.to_thread(repo.registration_fields, original)
    except Exception:
        return ""
    return str(orig.get(Registrations.DECISION_NOTE) or "").strip()


async def send_request(ctx: ContextTypes.DEFAULT_TYPE, chat_id, reg_id: str, reg: dict, header: str = "") -> None:
    """Карточка-вопрос по заявке (с кнопками решения и предложением звонка) — из /inbox, повторно, по 48 ч."""
    repo = ctx.bot_data["repo"]
    author = await _author(repo, reg)
    text = request_card(reg, reg_id, author, reg.get(Registrations.ANALYSIS), await _dealer(repo, reg),
                        await upgrade_digest(repo, reg), await asked_of(repo, reg))
    text += await send_files(ctx.bot, chat_id, _payload(reg), reg.get(Registrations.DEALER_PROJECT_NAME) or "")
    await send_card(ctx.bot, chat_id, header + text + age_line(reg.get(Registrations.SUBMITTED_AT)),
                    decision_markup(ctx, reg_id, await merge_candidates(repo, reg)))
    await send_call_offer(ctx.bot, chat_id, reg_id, reg, author)
    from . import file_notes
    file_notes.schedule(ctx.bot_data, ctx.bot, chat_id, _payload(reg), reg.get(Registrations.DEALER_PROJECT_NAME) or "")


async def cmd_inbox(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update, ctx):
        return
    repo = ctx.bot_data["repo"]
    recs = await asyncio.to_thread(repo.pending_without_decision)
    recs = [rec for rec in recs if not await _close_if_orphan_reply(repo, rec)]
    me = routing.who_by_chat(ctx.bot_data, update.effective_chat.id)
    recs = [rec for rec in recs if await reviewer_of(ctx, rec["fields"]) == me]
    stuck = [rec for rec in await asyncio.to_thread(repo.stuck_decisions)
             if await reviewer_of(ctx, rec["fields"]) == me]
    if stuck:
        await update.effective_chat.send_message(
            f"🛠 ⚠️ <b>Зависли: {len(stuck)}</b> — решение записано, но бот его не довёл:\n"
            + "\n".join(stuck_line(r["fields"]) for r in stuck)
            + "\nНажми «Довести до конца» — бот обработает решение сейчас.",
            parse_mode=ParseMode.HTML, reply_markup=stuck_markup(stuck))
    if not recs:
        if not stuck:
            await update.effective_chat.send_message("🛠 Заявок на проверке нет — делать ничего не нужно.")
        return
    await update.effective_chat.send_message(f"🛠 Заявок без решения: {len(recs)}. Присылаю по одной, старые первыми.")
    for rec in recs:
        await send_request(ctx, update.effective_chat.id, rec["id"], rec["fields"])


def age_hours(submitted_at: str | None, now: datetime | None = None) -> float:
    if not submitted_at:
        return 0.0
    then = datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
    return ((now or datetime.now(timezone.utc)) - then).total_seconds() / 3600


def age_line(submitted_at: str | None, now: datetime | None = None) -> str:
    """«⏰ На проверке 2 дн.» — срок ответа дилеру 72 часа."""
    if not submitted_at:
        return ""
    hours = age_hours(submitted_at, now)
    age = f"{int(hours)} ч" if hours < 24 else f"{int(hours // 24)} дн."
    late = " — срок 72 ч прошёл" if hours >= 72 else ""
    return f"\n⏰ На проверке {age}{late}"


async def merge_candidates(repo, reg: dict) -> list:
    """Совпадения для кнопок «Слить с»: только у новой заявки без Target Project."""
    if reg.get(Registrations.TARGET_PROJECT) or not str(reg.get(Registrations.REQUEST_TYPE) or "").startswith("New"):
        return []
    projects = await asyncio.to_thread(repo.dedup_projects)
    found = dedup.find(_payload(reg), reg.get(Registrations.DEALER_PROJECT_NAME) or "", projects)
    return [(c.project_id, c.title) for c in found]


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


# Компании из заявки, которые бот свяжет с проектом: (ключ анкеты, роль, поле проекта)
_COMPANY_KEYS = (("End Client", "клиент", Projects.END_CLIENT), ("Specifier", "бюро", Projects.SPECIFIER))


async def _ask_companies(ctx: ContextTypes.DEFAULT_TYPE, chat, reg_id: str, reg: dict, project_id: str,
                         resume: str) -> bool:
    """Клиент / бюро похожи на компанию из базы, но не то же название — спросить до записи. True — спросили."""
    repo = ctx.bot_data["repo"]
    payload = _payload(reg)
    picks = payload.get(COMPANY_PICKS) or {}
    project = await asyncio.to_thread(repo.project_fields, project_id) if project_id.startswith("rec") else {}
    for idx, (key, role, field) in enumerate(_COMPANY_KEYS):
        name = str(payload.get(key) or "").strip()
        if not name or key in picks or name == OWN_DESIGN or project.get(field):
            continue
        cands = await asyncio.to_thread(repo.company_candidates, name)
        if not cands:
            continue
        ctx.bot_data.setdefault("company_resume", {})[reg_id] = resume
        rows = [[InlineKeyboardButton(f"Это «{cname[:40]}»", callback_data=f"{CB_ADMIN}{ACTION_COMPANY}:{reg_id}:{idx}:{cid}")]
                for cid, cname in cands]
        rows.append([InlineKeyboardButton(f"🆕 Новая компания «{name[:35]}»",
                                          callback_data=f"{CB_ADMIN}{ACTION_COMPANY}:{reg_id}:{idx}:new")])
        rows.append([InlineKeyboardButton("Отмена", callback_data=f"{CB_ADMIN}{ACTION_CANCEL}:")])
        title = escape(reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия")
        await chat.send_message(f"🛠 «{title}»: {role} <b>{escape(name)}</b> — в базе есть похожие. "
                                "Это одна из них или новая компания?",
                                reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")
        return True
    return False


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
        log.exception("Ошибка немедленной обработки решения", extra={
            "alert": "Решение записано, но сразу довести его до конца не получилось. Бот попробует снова "
                     "в течение минуты; если заявка не закроется — /inbox покажет её в блоке «Зависли»."})


def stuck_line(reg: dict) -> str:
    """«• Saratov … — принята в проект, ждёт с 13.11 07:10»."""
    name = escape(reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия")
    decided = DECISION_RU.get(reg.get(Registrations.DECISION), "решение записано")
    parts = str(reg.get(Registrations.BOT_PROCESSED) or "").split(":")
    since = ""
    if len(parts) > 3 and parts[3].isdigit():
        since = ", висит с " + datetime.fromtimestamp(int(parts[3]), VALENCIA).strftime("%d.%m %H:%M")
    return f"• «{name}» — {decided}{since}"


def stuck_markup(recs: list[dict]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        f"▶️ Довести до конца: {(r['fields'].get(Registrations.DEALER_PROJECT_NAME) or 'без названия')[:40]}",
        callback_data=f"{CB_ADMIN}{ACTION_FINISH}:{r['id']}")] for r in recs])


async def finish_stuck(ctx: ContextTypes.DEFAULT_TYPE, chat, reg_id: str, reg: dict) -> None:
    """«Довести до конца»: снять метку ожидания и обработать записанное решение сейчас."""
    name = escape(reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия")
    if not reg.get(Registrations.DECISION):
        await chat.send_message(f"🛠 По «{name}» решения нет — доводить нечего. Заявка снова ждёт решения:")
        await send_request(ctx, chat.id, reg_id, reg)
        return
    if str(reg.get(Registrations.BOT_PROCESSED) or "").startswith("done:"):
        await chat.send_message(f"🛠 «{name}» уже обработана — делать ничего не нужно.")
        return
    _pending(ctx).pop(chat.id, None)
    await asyncio.to_thread(ctx.bot_data["repo"].clear_awaiting, reg_id)
    await chat.send_message(f"🛠 Довожу «{name}» до конца — итог придёт следующим сообщением.")
    await _apply_now(ctx)


async def callback_failed(ctx: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    """Сбой после нажатия кнопки проверяющим — алерт по-русски с названием проекта."""
    action, _, rest = data[len(CB_ADMIN):].partition(":")
    reg_id = rest.split(":")[0]
    name = ""
    if reg_id.startswith("rec"):
        try:
            reg = await asyncio.to_thread(ctx.bot_data["repo"].registration_fields, reg_id)
            name = reg.get(Registrations.DEALER_PROJECT_NAME) or ""
        except Exception:
            pass
    log.exception("Сбой кнопки проверяющего %s", data, extra={"alert": callback_failed_text(name)})


def callback_failed_text(name: str) -> str:
    what = f"по «{name}»" if name else "по заявке"
    return (f"Кнопка {what} не сработала до конца — решение могло не записаться или не уйти дилеру. "
            "Открой /inbox: заявка будет среди ожидающих решения или в блоке «Зависли».")




# ---------- что отменили ----------

_CANCEL_WHAT = {
    ACTION_ACCEPT_NOTE: "принятие — решения нет",
    ACTION_DECLINE: "отказ — решения нет",
    ACTION_INFO: "запрос дилеру — ничего не ушло",
    ACTION_NEW: "создание нового проекта — решения нет",
    ACTION_DNEW: "создание нового проекта",
    ACTION_RENAME: "переименование проекта — решения нет",
    ACTION_RB_DOWN: "откат — статус не менялся",
    ACTION_RB_CANCEL: "аннулирование — статус не менялся",
    ACTION_UP_KEEP: "сообщение дилеру — ничего не ушло, статус не менялся",
    ACTION_UP_DENY: "отказ — ничего не ушло, статус не менялся",
    ACTION_CALL_OTHER: "другое время звонка — дилеру ничего не ушло",
    ACTION_REMIND_USER: "просмотр напоминания",
    "jrej": "отказ в подключении — человеку ничего не ушло",
    "jco": "выбор компании — человек не подключён, заявка ждёт",
    "notecont": "принятие — решения нет",
}
_REG_ACTIONS = {ACTION_ACCEPT_NOTE, ACTION_DECLINE, ACTION_INFO, ACTION_NEW, ACTION_DNEW, ACTION_RENAME,
                ACTION_DPICK, "notecont"}


async def _cancel_text(ctx: ContextTypes.DEFAULT_TYPE, item: tuple | None) -> str:
    if not item:
        return "🛠 Отменено. Ничего не изменилось."
    what = _CANCEL_WHAT.get(item[0], "ввод")
    text = f"🛠 Отменено: {what}."
    if item[0] in _REG_ACTIONS and item[1]:
        try:
            reg = await asyncio.to_thread(ctx.bot_data["repo"].registration_fields, item[1])
            name = reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия"
            text += f" Заявка «{escape(name)}» по-прежнему ждёт решения — /inbox пришлёт её снова."
        except Exception:
            pass
    return text


# ---------- «Пока оставить» / «Отказать» на шаге повышения (Р1, Р2) ----------

def _step_dealer_text(project: dict, person_id: str, target: str, mode: str, comment: str) -> str:
    title = escape(name_ref(project.get(Projects.NAME) or "", project.get(Projects.PPR_REF) or ""))
    status = dealer_status(project, person_id)
    current = "уведомление о плейсменте" if status in (None, "notified") else STATUS_RU[status]
    note = f"\n💬 Комментарий: {escape(comment)}" if comment else ""
    if mode == "deny":
        asked = "Предрегистрация" if target == "pre" else "Регистрация"
        return (f"❌ {asked} по проекту <code>{title}</code> не подтверждена. Статус остаётся «{current}».{note}\n"
                "Если появятся новые данные — «Обновить проект» → «Дополнить материалы».")
    return (f"По проекту <code>{title}</code> статус пока остаётся «{current}».{note}\n"
            "Когда появятся новые данные — «Обновить проект» → «Дополнить материалы».")


async def step_decision(ctx: ContextTypes.DEFAULT_TYPE, chat, project_id: str, person_id: str, target: str,
                        mode: str, comment: str) -> None:
    """Проверяющий оставил статус или отказал в повышении — дилеру сообщение, в журнал строка."""
    repo = ctx.bot_data["repo"]
    project = await asyncio.to_thread(repo.project_fields, project_id)
    text = _step_dealer_text(project, person_id, target, mode, comment)
    chats = await asyncio.to_thread(repo.chat_ids_for_people, [person_id])
    for c in chats:
        await ctx.bot.send_message(c, text, parse_mode=ParseMode.HTML)
    person = await asyncio.to_thread(repo.person_name, person_id) or "менеджер"
    what = (f"отказ в {'предрегистрации' if target == 'pre' else 'регистрации'}" if mode == "deny"
            else "статус пока оставлен")
    try:
        await asyncio.to_thread(repo.append_project_log, project_id, project.get(Projects.PROJECT_LOG),
                                f"{datetime.now(timezone.utc):%d.%m.%Y} · проверка:",
                                [f"{person}: {what}" + (f". Комментарий: {comment}" if comment else "")])
    except Exception:
        log.exception("Журнал проекта: решение по шагу")
    await memo.pop(ctx.bot_data, "stepwise_next", f"{project_id}:{person_id}")
    sent = "Дилеру отправлено:" if chats else "⚠️ У дилера нет Telegram в базе — не отправлено:"
    await chat.send_message(f"🛠 {escape(person)} · {project_line(project)}: {what}.\n{sent}\n<i>{text}</i>",
                            parse_mode=ParseMode.HTML)


# ---------- звонок: ответ дилера на другое время (Р7) ----------

async def reviewer_chat(ctx: ContextTypes.DEFAULT_TYPE, reg: dict) -> str | None:
    return routing.resolve(ctx.bot_data, await reviewer_of(ctx, reg))[1]


async def dealer_call_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    """Дилер: «✅ Подходит» / «🕑 Предложить другое» под предложенным временем звонка."""
    action, _, rest = data[len(CB_DEALER_CALL):].partition(":")
    reg_id, _, idx = rest.partition(":")
    repo = ctx.bot_data["repo"]
    try:
        reg = await asyncio.to_thread(repo.registration_fields, reg_id)
    except Exception:
        await update.effective_chat.send_message("Эта заявка уже закрыта. Выбери действие внизу.")
        return
    name = reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия"
    proposed = (await memo.get(ctx.bot_data, "calls_proposed", {})).get(f"{reg_id}:{idx}", "")
    author = await _author(repo, reg) or "Дилер"
    target = await reviewer_chat(ctx, reg)
    if action == "ok":
        await confirm_call(ctx.bot_data, reg_id, int(idx or 0), proposed)
        await update.effective_chat.send_message(f"📞 Договорились: звонок по «{escape(name)}» — {escape(proposed)}. "
                                                 "До связи!", parse_mode=ParseMode.HTML)
        if target:
            await ctx.bot.send_message(target, f"📞 {escape(author)} подтвердил(а) звонок по «{escape(name)}»: "
                                               f"<b>{escape(proposed)}</b>. Напомню за час.", parse_mode=ParseMode.HTML)
        return
    await memo.set_item(ctx.bot_data, "waits", f"dealer:{update.effective_chat.id}", ["callalt", reg_id, idx])
    await update.effective_chat.send_message("Напиши, когда тебе удобно, — дату и время. Передам на проверку.")


async def dealer_call_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Дилер написал своё время звонка. True — обработано."""
    item = await memo.pop(ctx.bot_data, "waits", f"dealer:{update.effective_chat.id}")
    if not item or item[0] != "callalt":
        return False
    _, reg_id, idx = item
    repo = ctx.bot_data["repo"]
    reg = await asyncio.to_thread(repo.registration_fields, reg_id)
    name = reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия"
    await memo.set_item(ctx.bot_data, "calls_proposed", f"{reg_id}:{idx}", text.strip())
    target = await reviewer_chat(ctx, reg)
    author = await _author(repo, reg) or "Дилер"
    if target:
        await ctx.bot.send_message(target, f"📞 <b>{escape(author)} предлагает другое время звонка</b>\n"
                                           f"🏗 {escape(name)}\n🕑 <b>{escape(text.strip())}</b>",
                                   parse_mode=ParseMode.HTML, reply_markup=call_offer_markup(reg_id, int(idx or 0)))
    await update.effective_chat.send_message("Передал. Ответ — подтверждение или другое время — придёт сюда.")
    return True


# ---------- кнопки проверяющего ----------

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
    force = action in FORCE
    action = FORCE.get(action, action)
    who_name = routing.NAMES.get(routing.who_by_chat(ctx.bot_data, chat.id), "Егор")

    if action == ACTION_CANCEL:
        await chat.send_message(await _cancel_text(ctx, await drop_pending(update, ctx)))
        return

    if action in (ACTION_CALL_OK, ACTION_CALL_OTHER, ACTION_CALL_NONE) and project_id.isdigit():
        reg = await asyncio.to_thread(repo.registration_fields, reg_id)
        label, value, name = _call_text(reg, int(project_id))
        proposed = (await memo.get(ctx.bot_data, "calls_proposed", {})).get(f"{reg_id}:{project_id}")
        value = proposed or value
        if action == ACTION_CALL_OTHER:
            await _wait(ctx, chat.id, (ACTION_CALL_OTHER, reg_id, project_id))
            await chat.send_message(f"🛠 «{escape(name)}»: напиши, какое время звонка предложить. Дилеру уйдёт "
                                    "оно с кнопками «Подходит» / «Предложить другое».",
                                    reply_markup=InlineKeyboardMarkup([cancel_row()]))
            return
        if action == ACTION_CALL_NONE:
            sent = await _tell_partner(ctx, reg, f"📞 Звонок по проекту «{escape(name)}» не понадобится, "
                                                 "ответ придёт сюда.")
            await chat.send_message(f"🛠 «{escape(name)}»: звонок не нужен. "
                                    + ("Дилеру написал." if sent else "⚠️ У дилера нет Telegram в базе — не написал."))
            return
        sent = await _tell_partner(ctx, reg, f"📞 {who_name} подтверждает звонок по «{escape(name)}»: "
                                             f"<b>{escape(value)}</b>. До связи!")
        await confirm_call(ctx.bot_data, reg_id, int(project_id), value)
        from .slots import valencia_start
        remind = "Напомню за час." if valencia_start(value) else \
            "Время указано текстом — напомнить за час не смогу, запиши себе."
        await chat.send_message(f"🛠 Звонок по «{escape(name)}» подтверждён: {escape(value)}. "
                                + ("Дилеру написал. " if sent else "⚠️ У дилера нет Telegram в базе — не написал. ")
                                + remind)
        return

    # Повышение статуса без заявки — по проекту и менеджеру, не по записи Inbox
    if action == ACTION_UP_NO:   # кнопка старых сообщений
        await chat.send_message("🛠 Статус не менял. Повысить позже — /upgrade.")
        return
    if action in (ACTION_UP_KEEP, ACTION_UP_DENY) and len(parts) >= 3:
        mode = "deny" if action == ACTION_UP_DENY else "keep"
        project = await asyncio.to_thread(repo.project_fields, parts[0])
        await _wait(ctx, chat.id, (action, f"{parts[0]}:{parts[1]}", parts[2]))
        preview = _step_dealer_text(project, parts[1], parts[2], mode, "")
        head = "отказываем в повышении" if mode == "deny" else "статус пока оставляем"
        await chat.send_message(
            f"🛠 {project_line(project)}: {head}. Напиши комментарий дилеру или отправь без него.\n"
            f"Дилеру уйдёт:\n<i>{preview}</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Отправить без комментария",
                                      callback_data=f"{CB_ADMIN}{ACTION_UP_BARE}:{parts[0]}:{parts[1]}:{parts[2]}:{mode[0]}")],
                cancel_row()]))
        return
    if action == ACTION_UP_VIEW and len(parts) >= 3:
        # Всё, что есть в проекте, — и снова те же четыре кнопки решения
        project = await asyncio.to_thread(repo.project_fields, parts[0])
        ids = (project.get(Projects.END_CLIENT) or []) + (project.get(Projects.SPECIFIER) or [])
        companies = {c: await asyncio.to_thread(repo.company_name, c) for c in ids}
        person = await asyncio.to_thread(repo.person_name, parts[1]) or "менеджер без имени"
        status = dealer_status(project, parts[1])
        what = "предрегистрации" if parts[2] == "pre" else "регистрации"
        head = (f"🛠 <b>Проект перед решением</b> · {escape(person)} — сейчас "
                f"{STATUS_RU.get(status, 'нет статуса')}, вопрос о {what}")
        await send_card(ctx.bot, chat.id, "\n".join([head] + project_digest(project, parts[1], companies)),
                        upgrade_markup(parts[0], parts[1], parts[2]))
        return
    if action == ACTION_UP_BARE and len(parts) >= 4:
        pending.pop(chat.id, None)
        await memo.pop(ctx.bot_data, "waits", str(chat.id))
        await step_decision(ctx, chat, parts[0], parts[1], parts[2], "deny" if parts[3] == "d" else "keep", "")
        return
    if action == ACTION_UP_LIST:
        await _show_upgrade_list(chat, ctx, int(parts[0]) if parts and parts[0].isdigit() else 0)
        return
    if action == ACTION_UP_PICK and len(parts) >= 2:
        project = await asyncio.to_thread(repo.project_fields, parts[0])
        person = await asyncio.to_thread(repo.person_name, parts[1]) or "менеджер без имени"
        status = dealer_status(project, parts[1])
        targets = ["pre", "reg"] if status == "notified" else ["reg"] if status == "pre_registered" else []
        if project.get(Projects.REGISTERED):
            targets = [t for t in targets if t != "reg"]
        rows = [[InlineKeyboardButton(f"⬆️ На {_TARGET_RU[t]}",
                                      callback_data=f"{CB_ADMIN}{ACTION_UP}:{parts[0]}:{parts[1]}:{t}")] for t in targets]
        if status:
            rows.append([InlineKeyboardButton("↩️ Откатить / аннулировать",
                                              callback_data=f"{CB_ADMIN}{ACTION_RB}:{parts[0]}:{parts[1]}")])
        rows.append(cancel_row())
        now = "нет статуса" if not status else STATUS_RU[status]
        if targets:
            missing = missing_text(project.get(Projects.PPR_NEXT_MISSING))
            hint = f"\nДля следующего этапа не хватает: {escape(missing)}" if missing else ""
            question = "На какой статус?"
        else:
            why = ("у него уже регистрация — это последний этап" if status == "registered"
                   else "регистрация на объекте уже у другого партнёра")
            hint, question = "", f"Повышать некуда: {why}. Можно только откатить."
        await chat.send_message(f"🛠 {project_line(project)} · {escape(person)} — сейчас {now}.\n{question}{hint}",
                                reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")
        return
    if action == ACTION_RB and len(parts) >= 2:
        project = await asyncio.to_thread(repo.project_fields, parts[0])
        person = await asyncio.to_thread(repo.person_name, parts[1]) or "менеджер без имени"
        status = dealer_status(project, parts[1])
        if not status:
            await chat.send_message(f"🛠 У {escape(person)} в {project_line(project)} уже нет статуса — откатывать нечего.",
                                    parse_mode="HTML")
            return
        rows = []
        if status in _LOWER_RU:
            rows.append([InlineKeyboardButton(f"↩️ На {_LOWER_RU[status]}",
                                              callback_data=f"{CB_ADMIN}{ACTION_RB_DOWN}:{parts[0]}:{parts[1]}")])
        rows.append([InlineKeyboardButton("✖️ Аннулировать — снять статус",
                                          callback_data=f"{CB_ADMIN}{ACTION_RB_CANCEL}:{parts[0]}:{parts[1]}")])
        rows.append(cancel_row())
        await chat.send_message(f"🛠 {project_line(project)} · {escape(person)} — сейчас {STATUS_RU[status]}. "
                                "Что сделать со статусом?",
                                reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")
        return
    if action in _ROLLBACK and len(parts) >= 2:
        person = await asyncio.to_thread(repo.person_name, parts[1]) or "менеджер"
        await _wait(ctx, chat.id, (action, parts[0], parts[1]))
        await chat.send_message(f"🛠 Напиши причину — она уйдёт менеджеру ({escape(person)}) вместе с новым статусом.",
                                reply_markup=InlineKeyboardMarkup([cancel_row()]))
        return
    if action == ACTION_UP and len(parts) >= 3 and parts[2] in _TARGET_RU:
        who = routing.who_by_chat(ctx.bot_data, chat.id)
        await ctx.bot_data["upgrade_status"](ctx.application, parts[0], parts[1], parts[2], who)
        return

    try:
        reg = await asyncio.to_thread(repo.registration_fields, reg_id)
    except Exception:
        await chat.send_message("🛠 Этой заявки больше нет в базе — делать ничего не нужно.")
        return
    if action == ACTION_FINISH:
        await finish_stuck(ctx, chat, reg_id, reg)
        return
    # Кнопка под ответом дилера — решение по исходной заявке, ответ закрывается вместе с ней
    original = _payload(reg).get("In Reply To")
    if original and action in (ACTION_ACCEPT, ACTION_DECLINE, ACTION_INFO, ACTION_ACCEPT_NOTE, ACTION_DECLINE_BARE):
        reply_id, reg_id = reg_id, original
        try:
            reg = await asyncio.to_thread(repo.registration_fields, reg_id)
        except Exception:
            await chat.send_message("🛠 Исходной заявки больше нет в базе — ответ дилера закрыть не к чему.")
            return
    name = escape(reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия")
    _mark_decider(ctx, update, reg_id)
    decided = reg.get(Registrations.DECISION)
    # Застрявшее решение можно доделать кнопками (Р5)
    stuck = str(reg.get(Registrations.BOT_PROCESSED) or "").startswith("blocked:") and action in (
        ACTION_ASK, ACTION_ASK_SEND, ACTION_INFO, ACTION_NEW, ACTION_SET_STATUS, ACTION_DECLINE, ACTION_DECLINE_BARE)
    if decided and decided != Decision.NEED_INFO and not stuck \
            and action not in (ACTION_PICK, ACTION_DPICK, ACTION_DNEW, ACTION_NAME, ACTION_NOTE_OK,
                               ACTION_DECLINE_CANCEL):
        decided_ru = DECISION_RU.get(decided, decided)
        if reply_id:
            # Исходная заявка уже решена — ответ дилера закрываем, чтобы не висел в /inbox
            target = reg.get(Registrations.TARGET_PROJECT) or []
            await asyncio.to_thread(repo.close_reply, reply_id, target[0] if target else None)
            await chat.send_message(
                f"🛠 Заявка «{name}» уже {decided_ru}. Ответ дилера закрыт"
                + (" и добавлен в проект." if target else ".") + " Делать ничего не нужно."
            )
            await _apply_now(ctx)
            return
        await chat.send_message(f"🛠 Заявка «{name}» уже {decided_ru} — делать ничего не нужно.")
        return

    if action in (ACTION_ASK, ACTION_ASK_SEND):
        target = reg.get(Registrations.TARGET_PROJECT) or []
        project = await asyncio.to_thread(repo.project_fields, target[0]) if target else None
        text = decision_plan(reg, project).ask_text
        if not text:
            await chat.send_message(f"🛠 По «{name}» данных уже хватает — запрашивать нечего. Реши заявку:")
            await send_request(ctx, chat.id, reg_id, reg)
            return
        if action == ACTION_ASK:   # старая кнопка: сначала показать текст
            await chat.send_message(blocked_text(f"🛠 Запрос дилеру по «{name}».", text), parse_mode=ParseMode.HTML,
                                    reply_markup=blocked_markup(reg_id, "missing", reg))
            return
        await asyncio.to_thread(repo.update_registration, reg_id, {Registrations.BOT_PROCESSED: ""})
        await asyncio.to_thread(repo.set_decision, reg_id, Decision.NEED_INFO, text)
        await _apply_now(ctx)
        await chat.send_message(f"🛠 Запрос по «{name}» отправлен дилеру. Ответ придёт сюда.")
        return

    if action == ACTION_SET_STATUS and project_id in _STATUS_CODE:
        status = _STATUS_CODE[project_id]
        fields = {Registrations.REG_STATUS: status, Registrations.BOT_REG_STATUS: status,
                  Registrations.BOT_PROCESSED: ""}
        if reg.get(Registrations.DECISION) not in (Decision.ACCEPT, Decision.ACCEPT_MERGE):
            fields[Registrations.DECISION] = Decision.ACCEPT_MERGE if reg.get(Registrations.TARGET_PROJECT) \
                else Decision.ACCEPT
        await asyncio.to_thread(repo.update_registration, reg_id, fields)
        await chat.send_message(f"🛠 «{name}»: принимаю как {_STATUS_CODE_RU[project_id]}.")
        await _apply_now(ctx)
        return

    if action == ACTION_ACCEPT_NOTE:
        pending[chat.id] = (action, reg_id, reply_id)
        await asyncio.to_thread(repo.set_awaiting, reg_id, action, reply_id, chat.id)
        tail = f"::{reply_id}" if reply_id else ""
        rtype = reg.get(Registrations.REQUEST_TYPE) or ""
        lines = [f"🛠 {accept_phrase(rtype, name, _payload(reg))}."]
        if rtype in STEPWISE:
            lines.append("Проверим по шагам: сначала запишу уведомление, потом спрошу про предрегистрацию"
                         + (" и регистрацию." if STEPWISE[rtype] == "reg" else "."))
        lines.append(f"Дилеру уйдёт: {dealer_gets(rtype, _payload(reg), reg.get(Registrations.DEALER_PROJECT_NAME) or '')}")
        lines.append("Напиши комментарий дилеру или отправь без него.")
        await chat.send_message(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Отправить без комментария", callback_data=f"{CB_ADMIN}{ACTION_ACCEPT}:{reg_id}{tail}")],
                cancel_row()]),
        )
        return

    if action == ACTION_DECLINE_BARE:
        pending[chat.id] = (ACTION_DECLINE, reg_id, reply_id)
        await on_admin_text(update, ctx, "", resume=False)
        return

    if action in (ACTION_DECLINE, ACTION_INFO):
        pending[chat.id] = (action, reg_id, reply_id)
        await asyncio.to_thread(repo.set_awaiting, reg_id, action, reply_id, chat.id)
        rows = [cancel_row()]
        if action == ACTION_DECLINE:
            again = "«Новый проект»" if str(reg.get(Registrations.REQUEST_TYPE) or "").startswith("New") \
                else "«Обновить проект»"
            if reg.get(Registrations.REQUEST_TYPE) in UPGRADE_REQUESTS:
                text = (f"🛠 «{name}» — отклоняем повышение. Дилеру уйдёт карточка «❌ … не подтверждена»: "
                        "плейсмент остаётся уведомлением (или прежним статусом), и что нужно для следующего этапа. "
                        "Напиши причину или отправь без комментария.")
            else:
                text = (f"🛠 «{name}» — отклоняем. Дилеру уйдёт: «Заявка по проекту {name} не принята. "
                        f"Если появятся новые данные — подай заново через {again}». "
                        "Напиши причину или отправь без комментария.")
            tail = f"::{reply_id}" if reply_id else ""
            rows.insert(0, [InlineKeyboardButton("Отправить без комментария",
                                                 callback_data=f"{CB_ADMIN}{ACTION_DECLINE_BARE}:{reg_id}{tail}")])
        else:
            text = (f"🛠 «{name}»: напиши вопрос. Дилеру уйдёт «❓ По проекту {name} нужна дополнительная "
                    "информация: {твой текст}» с кнопкой «Ответить». Ответ придёт сюда.")
        await chat.send_message(text, reply_markup=InlineKeyboardMarkup(rows))
        return

    if action == ACTION_DECLINE_CANCEL:
        # Отказ в повышении записан, а проект для уведомления не выбран — отменить отказ целиком
        await asyncio.to_thread(repo.update_registration, reg_id, {
            Registrations.DECISION: None, Registrations.DECISION_NOTE: "", Registrations.BOT_PROCESSED: ""})
        await chat.send_message(f"🛠 Отказ по «{name}» отменён. Заявка снова ждёт решения — /inbox пришлёт её.")
        return

    if action == ACTION_COMPANY and len(parts) >= 3 and parts[1].isdigit() and int(parts[1]) < len(_COMPANY_KEYS):
        payload = _payload(reg)
        payload.setdefault(COMPANY_PICKS, {})[_COMPANY_KEYS[int(parts[1])][0]] = parts[2]
        await asyncio.to_thread(repo.update_registration, reg_id,
                                {Registrations.PAYLOAD: json.dumps(payload, ensure_ascii=False, indent=2)})
        resume = ctx.bot_data.setdefault("company_resume", {}).pop(reg_id, "")
        if resume:
            await on_admin_callback(update, ctx, resume)   # дальше — следующий вопрос или само решение
        else:
            await chat.send_message("🛠 Записал компанию. Вот заявка снова — реши её:")
            reg[Registrations.PAYLOAD] = json.dumps(payload, ensure_ascii=False)
            await send_request(ctx, chat.id, reg_id, reg)
        return

    if action == ACTION_NOTE_OK:
        # Предложенный комментарий (принято этапом ниже): parts[1] — «1» отправить его, «0» — без комментария
        saved = ctx.bot_data.setdefault("note_resume", {}).pop(reg_id, None)
        pending.pop(chat.id, None)
        await asyncio.to_thread(repo.clear_awaiting, reg_id)
        if not saved:
            await chat.send_message("🛠 Это предложение уже неактуально. Реши заявку:")
            await send_request(ctx, chat.id, reg_id, reg)
            return
        if project_id == "1":
            ctx.bot_data.setdefault("accept_notes", {})[reg_id] = saved["note"]
        await on_admin_callback(update, ctx, saved["resume"])
        return

    target_now = (reg.get(Registrations.TARGET_PROJECT) or [""])[0]
    if action == ACTION_NEW or (action == ACTION_PICK and project_id) or (action == ACTION_ACCEPT and target_now):
        where = project_id if action == ACTION_PICK else target_now if action == ACTION_ACCEPT else "-"
        if await _ask_companies(ctx, chat, reg_id, reg, where, data):
            return
    if action in (ACTION_ACCEPT, ACTION_PICK, ACTION_NEW):
        await _start_stepwise(ctx, reg_id, reg)

    if action == ACTION_ACCEPT:
        target = reg.get(Registrations.TARGET_PROJECT) or []
        if target and not force and await _confirm_weak(chat, reg, "accf", reg_id, "-", reply_id):
            return
        if target and await _ask_rename(ctx, chat, reg_id, target[0], reply_id):
            return
        if target:
            pending.pop(chat.id, None)   # комментарий больше не ждём — следующий текст не должен уйти в эту заявку
            await asyncio.to_thread(repo.set_decision, reg_id, Decision.ACCEPT_MERGE, _accept_note(ctx, reg_id))
            if reply_id:
                await asyncio.to_thread(repo.close_reply, reply_id, target[0])
            await _apply_now(ctx)
            return
        projects = await asyncio.to_thread(repo.dedup_projects)
        found = dedup.find(_payload(reg), reg.get(Registrations.DEALER_PROJECT_NAME) or "", projects)
        tail = f":{reply_id}" if reply_id else ""
        rows = [[InlineKeyboardButton(f"В проект: {c.title[:50]}",
                                      callback_data=f"{CB_ADMIN}{ACTION_PICK}:{reg_id}:{c.project_id}{tail}")]
                for c in found]
        rows.append([InlineKeyboardButton("Создать новый проект",
                                          callback_data=f"{CB_ADMIN}{ACTION_NEW}:{reg_id}:-{tail}")])
        rows.append(cancel_row())
        if found:
            hint = "Похожие проекты в базе:\n" + "\n".join(
                f"• {escape(c.title)} — совпадает: {escape(', '.join(c.reasons))}" for c in found)
        else:
            hint = "Похожих проектов по объекту, клиенту и адресу не нашёл."
        await chat.send_message(f"🛠 «{name}»: в какой проект?\n{hint}", reply_markup=InlineKeyboardMarkup(rows))
        return

    if action == ACTION_DPICK and project_id:
        await asyncio.to_thread(repo.clear_awaiting, reg_id)
        await asyncio.to_thread(repo.update_registration, reg_id, {Registrations.TARGET_PROJECT: [project_id],
                                                                   Registrations.BOT_PROCESSED: ""})
        await _apply_now(ctx)
        return

    if action == ACTION_RENAME:
        suggestion = ctx.bot_data.get("rename_suggest", {}).pop(reg_id, "")
        if suggestion:
            pending.pop(chat.id, None)
            await asyncio.to_thread(repo.clear_awaiting, reg_id)
            await _rename_and_merge(ctx, reg_id, reply_id, suggestion)
        return

    if action == ACTION_NAME:
        # project_id здесь — исходное действие (new / dnew), reply_id — ответ дилера
        suggestion = dedup.latin_name(_payload(reg))
        if suggestion and project_id in (ACTION_NEW, ACTION_DNEW):
            pending.pop(chat.id, None)
            await asyncio.to_thread(repo.clear_awaiting, reg_id)
            await create_named_project(ctx, project_id, reg_id, reply_id, suggestion, chat)
        return

    if action == ACTION_DNEW:
        pending[chat.id] = (ACTION_DNEW, reg_id, reply_id)
        await asyncio.to_thread(repo.set_awaiting, reg_id, ACTION_DNEW, reply_id, chat.id)
        await chat.send_message(f"🛠 «{name}»: напиши название нового проекта латиницей — {NAME_SAMPLE}.",
                                reply_markup=name_markup(reg, reg_id, ACTION_DNEW, reply_id))
        return

    if action == ACTION_PICK and project_id:
        if not force and await _confirm_weak(chat, reg, "pickf", reg_id, project_id, reply_id):
            return
        if await _ask_rename(ctx, chat, reg_id, project_id, reply_id):
            return
        await asyncio.to_thread(repo.set_decision, reg_id, Decision.ACCEPT_MERGE, _accept_note(ctx, reg_id), project_id)
        await asyncio.to_thread(repo.update_registration, reg_id, {Registrations.BOT_PROCESSED: ""})
        if reply_id:
            await asyncio.to_thread(repo.close_reply, reply_id, project_id)
        await _apply_now(ctx)
        return

    if action in DOWNGRADE:
        rtype = reg.get(Registrations.REQUEST_TYPE)
        tail = f":{reply_id}" if reply_id else ""
        resume = f"{CB_ADMIN}{DOWNGRADE[action]}:{reg_id}:{project_id or '-'}{tail}"
        if rtype in _PRE_OF:
            await asyncio.to_thread(repo.update_registration, reg_id, {Registrations.REQUEST_TYPE: _PRE_OF[rtype]})
            missing = "; ".join(w.split(" — «")[0] for w in weak_answers(_payload(reg)))
            note = "Принято как предрегистрация." + (f" Для регистрации не хватает: {missing}." if missing else "")
            prev = ctx.bot_data.setdefault("accept_notes", {}).pop(reg_id, "")
            note = f"{prev}\n{note}" if prev else note
            # Правило 1.4: автокомментарий дилеру — только после просмотра
            ctx.bot_data.setdefault("note_resume", {})[reg_id] = {"note": note, "resume": resume}
            pending[chat.id] = ("notecont", reg_id, reply_id)
            await asyncio.to_thread(repo.set_awaiting, reg_id, "notecont", reply_id, chat.id)
            await chat.send_message(
                f"🛠 Принимаем «{name}» как предрегистрацию. Дилеру уйдёт комментарий:\n<i>{escape(note)}</i>\n"
                "Отправить его, без комментария — или напиши свой текст.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Отправить этот комментарий", callback_data=f"{CB_ADMIN}{ACTION_NOTE_OK}:{reg_id}:1")],
                    [InlineKeyboardButton("Без комментария", callback_data=f"{CB_ADMIN}{ACTION_NOTE_OK}:{reg_id}:0")],
                    cancel_row()]))
            return
        await on_admin_callback(update, ctx, resume)
        return

    if action == ACTION_NEW:
        name_ready = ctx.bot_data.setdefault("new_names", {}).pop(reg_id, "") if force else ""
        if name_ready:   # название уже выбрано, переспрос по пустым полям подтверждён
            await create_named_project(ctx, ACTION_NEW, reg_id, reply_id, name_ready, chat, force=True)
            return
        pending[chat.id] = (ACTION_NEW, reg_id, reply_id)
        await asyncio.to_thread(repo.set_awaiting, reg_id, ACTION_NEW, reply_id, chat.id)
        await chat.send_message(
            f"🛠 «{name}»: напиши название нового проекта латиницей — {NAME_SAMPLE}"
            + (" — или возьми вариант кнопкой" if dedup.latin_name(_payload(reg)) else "") + ".",
            reply_markup=name_markup(reg, reg_id, ACTION_NEW, reply_id),
        )


NAME_SAMPLE = "образец: PVSMART InsaitBC FL3 (клиент, объект с кодом типа, этаж)"
# Вин. падеж для «Принимаем как …», «Точно подтверждаешь …?»
_ACC = {"уведомление": "уведомление", "предрегистрация": "предрегистрацию", "регистрация": "регистрацию",
        "переход на предрегистрацию": "переход на предрегистрацию", "переход на регистрацию": "переход на регистрацию"}


async def _confirm_weak(chat, reg: dict, force_action: str, reg_id: str, project_id: str, reply_id: str,
                        new_name: str = "") -> bool:
    """Заявка на статус с незаполненными ответами — переспросить. True — спросили, дальше не идём.

    Для запрошенной регистрации — ещё и «Принять как предрегистрацию»."""
    rtype = reg.get(Registrations.REQUEST_TYPE)
    if rtype not in STATUS_REQUESTS:
        return False
    weak = weak_answers(_payload(reg))
    if not weak:
        return False
    what = _ACC.get(REQUEST_LABELS.get(rtype, ""), "статус")
    data = f"{CB_ADMIN}{force_action}:{reg_id}:{project_id}" + (f":{reply_id}" if reply_id else "")
    rows = [[InlineKeyboardButton(f"✅ Да, подтверждаю {what}", callback_data=data)]]
    if rtype in _PRE_OF:
        down = f"{CB_ADMIN}{_DOWN_OF[force_action]}:{reg_id}:{project_id}" + (f":{reply_id}" if reply_id else "")
        rows.append([InlineKeyboardButton("⬇️ Принять как предрегистрацию", callback_data=down)])
    rows += [[InlineKeyboardButton("✏️ Запросить у дилера", callback_data=f"{CB_ADMIN}{ACTION_INFO}:{reg_id}")],
             cancel_row()]
    name = escape(reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия")
    head = (f"🛠 «{name}» → новый проект <b>{escape(new_name)}</b> (референс присвоится при создании)"
            if new_name else f"🛠 «{name}»")
    await chat.send_message(
        f"{head}: дилер не ответил на вопросы:\n" + "\n".join(f"• {escape(w)}" for w in weak)
        + f"\n\nТочно подтверждаешь {what}?",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")
    return True


async def _ask_rename(ctx: ContextTypes.DEFAULT_TYPE, chat, reg_id: str, project_id: str, reply_id: str) -> bool:
    """Проект для слияния назван не латиницей или длиннее 80 знаков — сначала новое название."""
    repo = ctx.bot_data["repo"]
    project = await asyncio.to_thread(repo.project_fields, project_id)
    name = project.get(Projects.NAME) or ""
    problem = dedup.name_problem(name)
    if not problem:
        return False
    await asyncio.to_thread(repo.update_registration, reg_id, {Registrations.TARGET_PROJECT: [project_id]})
    _pending(ctx)[chat.id] = (ACTION_RENAME, reg_id, reply_id)
    await asyncio.to_thread(repo.set_awaiting, reg_id, ACTION_RENAME, reply_id, chat.id)
    suggestion = dedup.to_latin(name)
    ctx.bot_data.setdefault("rename_suggest", {})[reg_id] = suggestion
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ {suggestion[:60]}",
                                                         callback_data=f"{CB_ADMIN}{ACTION_RENAME}:{reg_id}")],
                                   cancel_row()])
    await chat.send_message(
        f"🛠 Проект «{escape(name)}»: {problem}. Напиши новое название ({NAME_SAMPLE}) — "
        "или возьми транслит кнопкой.",
        reply_markup=markup, parse_mode="HTML")
    return True


async def _rename_and_merge(ctx: ContextTypes.DEFAULT_TYPE, reg_id: str, reply_id: str, name: str) -> None:
    repo = ctx.bot_data["repo"]
    reg = await asyncio.to_thread(repo.registration_fields, reg_id)
    target = (reg.get(Registrations.TARGET_PROJECT) or [None])[0]
    if not target:
        return
    await asyncio.to_thread(repo.update_project, target, {Projects.NAME: name})
    await asyncio.to_thread(repo.set_decision, reg_id, Decision.ACCEPT_MERGE, _accept_note(ctx, reg_id), target)
    if reply_id:
        await asyncio.to_thread(repo.close_reply, reply_id, target)
    await _apply_now(ctx)


def _accept_note(ctx: ContextTypes.DEFAULT_TYPE, reg_id: str) -> str:
    """Комментарий к «Принять» (или пусто — чтобы не ушёл старый текст запроса информации)."""
    return ctx.bot_data.setdefault("accept_notes", {}).pop(reg_id, "")


def name_markup(reg: dict, reg_id: str, action: str, reply_id: str = "") -> InlineKeyboardMarkup:
    rows = []
    suggestion = dedup.latin_name(_payload(reg))
    if suggestion:
        data = f"{CB_ADMIN}{ACTION_NAME}:{reg_id}:{action}" + (f":{reply_id}" if reply_id else "")
        rows.append([InlineKeyboardButton(f"✅ {suggestion}", callback_data=data)])
    rows.append(cancel_row())
    return InlineKeyboardMarkup(rows)


async def create_named_project(ctx: ContextTypes.DEFAULT_TYPE, action: str, reg_id: str, reply_id: str,
                               name: str, chat=None, force: bool = False) -> None:
    """Создать проект с этим названием: new — принять в него, dnew — отказ фиксируется в нём.

    Сначала название, потом переспрос по пустым полям (в нём уже видно, какой проект появится)."""
    repo = ctx.bot_data["repo"]
    if action == ACTION_NEW and not force and chat is not None:
        reg = await asyncio.to_thread(repo.registration_fields, reg_id)
        ctx.bot_data.setdefault("new_names", {})[reg_id] = name
        if await _confirm_weak(chat, reg, "newf", reg_id, "-", reply_id, new_name=name):
            return
        ctx.bot_data["new_names"].pop(reg_id, None)
    project_id = await asyncio.to_thread(repo.create_project, name)
    ctx.bot_data.setdefault("created_projects", set()).add(project_id)  # для итога «Проект создан»
    if action == ACTION_DNEW:
        await asyncio.to_thread(repo.update_registration, reg_id, {Registrations.TARGET_PROJECT: [project_id],
                                                                   Registrations.BOT_PROCESSED: ""})
    else:
        await asyncio.to_thread(repo.set_decision, reg_id, Decision.ACCEPT, _accept_note(ctx, reg_id), project_id)
        await asyncio.to_thread(repo.update_registration, reg_id, {Registrations.BOT_PROCESSED: ""})
        if reply_id:
            await asyncio.to_thread(repo.close_reply, reply_id, project_id)
    await _apply_now(ctx)


async def on_admin_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str, resume: bool = True) -> bool:
    """Текст админа в ответ на вопрос бота. True — обработано, дальше не передавать."""
    if not is_admin(update, ctx):
        return False
    pending = _pending(ctx)
    repo = ctx.bot_data["repo"]
    chat = update.effective_chat
    item = pending.pop(chat.id, None)
    if not item and resume:
        # Бот мог перезапуститься, пока админ печатал, — берём ожидание из памяти бота или Airtable
        saved = (await memo.get(ctx.bot_data, "waits", {})).get(str(chat.id))
        item = tuple(saved) if saved else await asyncio.to_thread(repo.find_awaiting, chat.id)
    if not item:
        return False  # бот ничего не ждёт — текст идёт дальше, в обычный диалог
    action, reg_id, reply_id = item
    if action in _MEMO_ACTIONS and text.strip():
        await memo.pop(ctx.bot_data, "waits", str(chat.id))
    from . import onboarding
    if action in onboarding.ADMIN_ACTIONS:
        return await onboarding.on_admin_text(update, ctx, item, text)
    if action == ACTION_CALL_OTHER:
        if not text.strip():
            pending[chat.id] = item
            await chat.send_message("🛠 Пустой текст — напиши время звонка.", reply_markup=InlineKeyboardMarkup([cancel_row()]))
            return True
        reg = await asyncio.to_thread(repo.registration_fields, reg_id)
        _, value, name = _call_text(reg, int(reply_id or 0))
        who = routing.NAMES.get(routing.who_by_chat(ctx.bot_data, chat.id), "Егор")
        await memo.set_item(ctx.bot_data, "calls_proposed", f"{reg_id}:{reply_id or 0}", text.strip())
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Подходит", callback_data=f"{CB_DEALER_CALL}ok:{reg_id}:{reply_id or 0}"),
            InlineKeyboardButton("🕑 Предложить другое", callback_data=f"{CB_DEALER_CALL}other:{reg_id}:{reply_id or 0}"),
        ]])
        sent = await _tell_partner(ctx, reg, f"📞 Звонок по «{escape(name)}»: вместо {escape(value)} {who} предлагает "
                                             f"<b>{escape(text.strip())}</b>. Подходит?", markup)
        await chat.send_message("🛠 Дилеру отправил — ответ придёт сюда." if sent
                                else "🛠 ⚠️ У дилера нет Telegram в базе — не отправил.")
        return True
    if action in _ROLLBACK:
        reason = text.strip()
        if not reason:
            pending[chat.id] = item
            await chat.send_message("🛠 Пустой текст — напиши причину.", reply_markup=InlineKeyboardMarkup([cancel_row()]))
            return True
        who = routing.who_by_chat(ctx.bot_data, chat.id)
        await ctx.bot_data["rollback_status"](ctx.application, reg_id, reply_id, _ROLLBACK[action], reason, who)
        return True
    if action in (ACTION_UP_KEEP, ACTION_UP_DENY):
        project_id, _, person_id = reg_id.partition(":")
        await step_decision(ctx, chat, project_id, person_id, reply_id,
                            "deny" if action == ACTION_UP_DENY else "keep", text.strip())
        return True
    _mark_decider(ctx, update, reg_id)
    if action != ACTION_REMIND_USER:
        await asyncio.to_thread(repo.clear_awaiting, reg_id)
    text = text.strip()
    if not text and action != ACTION_DECLINE:   # отказ — можно без комментария
        pending[chat.id] = item
        await chat.send_message("🛠 Пустой текст — напиши ещё раз.", reply_markup=InlineKeyboardMarkup([cancel_row()]))
        return True
    if action == ACTION_REMIND_USER:
        await ctx.bot_data["preview_reminder"](update, ctx, text)
        return True
    if action == "notecont":
        saved = ctx.bot_data.setdefault("note_resume", {}).pop(reg_id, None)
        if saved:
            ctx.bot_data.setdefault("accept_notes", {})[reg_id] = text
            await on_admin_callback(update, ctx, saved["resume"])
        return True
    if action == ACTION_DPICK:
        # Ждём выбор проекта кнопкой, а не текст
        await asyncio.to_thread(repo.set_awaiting, reg_id, ACTION_DPICK, reply_id, chat.id)
        await chat.send_message("🛠 Здесь нужен выбор проекта кнопкой — в сообщении «в какой проект?» выше.")
        return True
    if action in (ACTION_DNEW, ACTION_NEW, ACTION_RENAME):
        problem = dedup.name_problem(text)
        if problem:
            pending[chat.id] = item
            await asyncio.to_thread(repo.set_awaiting, reg_id, action, reply_id, chat.id)
            await chat.send_message(f"🛠 {problem[:1].upper()}{problem[1:]}. Напиши название ещё раз — {NAME_SAMPLE}.",
                                    reply_markup=InlineKeyboardMarkup([cancel_row()]))
            return True
        if action == ACTION_RENAME:
            await _rename_and_merge(ctx, reg_id, reply_id, text)
        else:
            await create_named_project(ctx, action, reg_id, reply_id, text, chat)
        return True
    if action == ACTION_ACCEPT_NOTE:
        # Комментарий запомнили — дальше как обычное «Принять»
        ctx.bot_data.setdefault("accept_notes", {})[reg_id] = text
        await on_admin_callback(update, ctx, f"{CB_ADMIN}{ACTION_ACCEPT}:{reg_id}" + (f"::{reply_id}" if reply_id else ""))
        return True
    decision = Decision.DECLINE if action == ACTION_DECLINE else Decision.NEED_INFO
    reg = await asyncio.to_thread(repo.registration_fields, reg_id)
    if (decision == Decision.DECLINE and reg.get(Registrations.REQUEST_TYPE) in UPGRADE_REQUESTS
            and not reg.get(Registrations.TARGET_PROJECT)):
        # Отказ всё равно фиксирует плейсмент уведомлением — сначала выбрать проект
        await asyncio.to_thread(repo.set_decision, reg_id, decision, text)
        await asyncio.to_thread(repo.set_awaiting, reg_id, ACTION_DPICK, reply_id, chat.id)
        found = await merge_candidates(repo, reg)
        rows = [[InlineKeyboardButton(f"🔗 В проект: {title[:50]}",
                                      callback_data=f"{CB_ADMIN}{ACTION_DPICK}:{reg_id}:{pid}")]
                for pid, title in found]
        rows.append([InlineKeyboardButton("🆕 Новый проект", callback_data=f"{CB_ADMIN}{ACTION_DNEW}:{reg_id}:-")])
        rows.append([InlineKeyboardButton("Отмена — не отказывать",
                                          callback_data=f"{CB_ADMIN}{ACTION_DECLINE_CANCEL}:{reg_id}")])
        name = escape(reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия")
        await chat.send_message(
            f"🛠 Отказ по «{name}» записан. Отказ в повышении всё равно фиксирует плейсмент уведомлением — "
            "в какой проект?",
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
