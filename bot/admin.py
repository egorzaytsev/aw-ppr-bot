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

from . import dedup, routing
from .cards import dealer_status
from .decisions import plan as decision_plan
from .scenarios.base import describe_items, format_value
from .scenarios.new_project import QUESTIONS as _NEW_PROJECT_QUESTIONS
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
# Повышение статуса без заявки: ad:up:<проект>:<менеджер>:pre|reg; список /upgrade и выбор пары
ACTION_UP, ACTION_UP_NO, ACTION_UP_LIST, ACTION_UP_PICK = "up", "upno", "upl", "ups"
# Подтверждение несмотря на незаполненные ответы: те же действия с флагом «точно»
FORCE = {"pickf": ACTION_PICK, "accf": ACTION_ACCEPT, "newf": ACTION_NEW}
# Проект для слияния назван не латиницей — сначала новое название
ACTION_RENAME = "ren"
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


async def drop_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Админ нажал меню — незавершённый ввод отменяется (и в Airtable)."""
    if not is_admin(update, ctx):
        return
    item = _pending(ctx).pop(update.effective_chat.id, None)
    repo = ctx.bot_data["repo"]
    if not item:
        item = await asyncio.to_thread(repo.find_awaiting, update.effective_chat.id)
    if item and item[0] != ACTION_REMIND_USER:
        await asyncio.to_thread(repo.clear_awaiting, item[1])


def _pending(ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    return ctx.bot_data.setdefault("admin_pending", {})


def upgrade_markup(project_id: str, person_id: str, target: str) -> InlineKeyboardMarkup:
    what = "предрегистрации" if target == "pre" else "регистрации"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⬆️ Повысить до {what}", callback_data=f"{CB_ADMIN}{ACTION_UP}:{project_id}:{person_id}:{target}")],
        [InlineKeyboardButton("Пока оставить", callback_data=f"{CB_ADMIN}{ACTION_UP_NO}:")],
    ])


async def _upgrade_pairs(ctx: ContextTypes.DEFAULT_TYPE) -> list[tuple]:
    """(проект, менеджер, статус, подпись) — кого можно повысить: уведомление / предрегистрация, не Simplified."""
    repo = ctx.bot_data["repo"]
    names: dict = ctx.bot_data.setdefault("person_names", {})
    out = []
    for r in await asyncio.to_thread(repo.projects_with_managers):
        f = r["fields"]
        if f.get(Projects.PPR_TRACK) == "Simplified (<30k)":
            continue
        ref, pname = f.get(Projects.PPR_REF) or "", (f.get(Projects.NAME) or "")[:24]
        rows = [(p, "pre") for p in f.get(Projects.PRE_REGISTERED) or []] if not f.get(Projects.REGISTERED) else []
        rows += [(p, "notified") for p in f.get(Projects.NOTIFIED) or []]
        for person, status in rows:
            if person not in names:
                names[person] = (await asyncio.to_thread(repo.person_name, person)) or person
            short = names[person].split()[0] if names[person] else "?"
            label = f"{ref} · {pname} · {short} · {'предрег.' if status == 'pre' else 'увед.'}"
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
        await chat.send_message("🛠 Повышать некого: нет менеджеров в уведомлении или предрегистрации.")
        return
    pages = (len(pairs) + UPGRADE_PAGE - 1) // UPGRADE_PAGE
    page = max(0, min(page, pages - 1))
    rows = [[InlineKeyboardButton(label[:60], callback_data=f"{CB_ADMIN}{ACTION_UP_PICK}:{pid}:{person}")]
            for pid, person, _, label in pairs[page * UPGRADE_PAGE:(page + 1) * UPGRADE_PAGE]]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("← Назад", callback_data=f"{CB_ADMIN}{ACTION_UP_LIST}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Дальше →", callback_data=f"{CB_ADMIN}{ACTION_UP_LIST}:{page + 1}"))
    if nav:
        rows.append(nav)
    await chat.send_message(f"🛠 Кого повысить? ({page + 1}/{pages})", reply_markup=InlineKeyboardMarkup(rows))


def ask_markup(reg_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📨 Запросить у дилера", callback_data=f"{CB_ADMIN}{ACTION_ASK}:{reg_id}")],
        [InlineKeyboardButton("✏️ Свой текст запроса", callback_data=f"{CB_ADMIN}{ACTION_INFO}:{reg_id}")],
    ])


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
    rows.append([InlineKeyboardButton("💬 Принять с комментарием",
                                      callback_data=f"{CB_ADMIN}{ACTION_ACCEPT_NOTE}:{reg_id}")])
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


_EXTRA_TITLES = {"Bureau": "Архбюро (AW не закладывало)"}

# «Нет» здесь — полноценный ответ, а не пропуск
_NO_IS_ANSWER = {"Parallel Registrations", "AW Alternatives", "Competing Dealers", "Area Source"}
_VAGUE = re.compile(r"^(нет\b|—$|-$|не знаю|пока\b)|неизвест|не получил|нет данных|уточня", re.I)


def weak_answers(payload: dict) -> list[str]:
    """Вопросы заявки без настоящего ответа: пропущено, «нет», «пока неизвестно»."""
    out = []
    for key, q in _QUESTIONS.items():
        if key.startswith("_") or key not in payload:
            continue  # не спрашивали — не ответ
        value = payload[key]
        if value in (None, "", []):
            out.append(q.title)
        elif isinstance(value, str) and _VAGUE.search(value.strip()) and not (
                key in _NO_IS_ANSWER and value.strip().lower() == "нет"):
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
                title = _QUESTIONS[k].title if k in _QUESTIONS else _EXTRA_TITLES.get(k, k)
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
        _pair(("AW в проекте бюро", _v(payload, "AW Origin"))),
        _pair(("Бюро", _v(payload, "Bureau") or _v(payload, "Specifier"))),
        _pair(("ГАП", _v(payload, "Lead Architect"))),
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
    me = routing.who_by_chat(ctx.bot_data, update.effective_chat.id)
    recs = [rec for rec in recs if await reviewer_of(ctx, rec["fields"]) == me]
    if not recs:
        await update.effective_chat.send_message("🛠 В Inbox нет заявок без решения.")
        return
    if not recs:
        await update.effective_chat.send_message("🛠 В Inbox нет заявок без решения.")
        return
    await update.effective_chat.send_message(f"🛠 Заявок без решения: {len(recs)}")
    for rec in recs:
        await update.effective_chat.send_message(
            request_card(rec["fields"], rec["id"], await _author(repo, rec["fields"]),
                         rec["fields"].get(Registrations.ANALYSIS), await _dealer(repo, rec["fields"]))
            + age_line(rec["fields"].get(Registrations.SUBMITTED_AT)),
            parse_mode=ParseMode.HTML,
            reply_markup=decision_markup(ctx, rec["id"], await merge_candidates(repo, rec["fields"])),
        )


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
    force = action in FORCE
    action = FORCE.get(action, action)

    if action == ACTION_CANCEL:
        await drop_pending(update, ctx)
        await chat.send_message("🛠 Отменено.")
        return

    # Повышение статуса без заявки — по проекту и менеджеру, не по записи Inbox
    if action == ACTION_UP_NO:
        await chat.send_message("🛠 Оставил как есть. Повысить позже — /upgrade.")
        return
    if action == ACTION_UP_LIST:
        await _show_upgrade_list(chat, ctx, int(parts[0]) if parts and parts[0].isdigit() else 0)
        return
    if action == ACTION_UP_PICK and len(parts) >= 2:
        project = await asyncio.to_thread(repo.project_fields, parts[0])
        status = dealer_status(project, parts[1])
        targets = ["pre", "reg"] if status == "notified" else ["reg"] if status == "pre_registered" else []
        if project.get(Projects.REGISTERED):
            targets = [t for t in targets if t != "reg"]
        if not targets:
            await chat.send_message("🛠 Повышать некуда: статус уже максимальный или регистрация занята.")
            return
        rows = [[InlineKeyboardButton(f"⬆️ На {_TARGET_RU[t]}",
                                      callback_data=f"{CB_ADMIN}{ACTION_UP}:{parts[0]}:{parts[1]}:{t}")] for t in targets]
        rows.append([InlineKeyboardButton("Отмена", callback_data=f"{CB_ADMIN}{ACTION_UP_NO}:")])
        missing = project.get(Projects.PPR_NEXT_MISSING) or ""
        hint = f"\nДля следующего этапа не хватает: {escape(missing)}" if missing else ""
        await chat.send_message(f"🛠 {escape(project.get(Projects.PPR_REF) or '')} · "
                                f"{escape(project.get(Projects.NAME) or '')}: на какой статус?{hint}",
                                reply_markup=InlineKeyboardMarkup(rows))
        return
    if action == ACTION_UP and len(parts) >= 3 and parts[2] in _TARGET_RU:
        who = routing.who_by_chat(ctx.bot_data, chat.id)
        await ctx.bot_data["upgrade_status"](ctx.application, parts[0], parts[1], parts[2], who)
        return

    try:
        reg = await asyncio.to_thread(repo.registration_fields, reg_id)
    except Exception:
        await chat.send_message("Заявка не найдена.")
        return
    # Кнопка под ответом дилера — решение по исходной заявке, ответ закрывается вместе с ней
    original = _payload(reg).get("In Reply To")
    if original and action in (ACTION_ACCEPT, ACTION_DECLINE, ACTION_INFO, ACTION_ACCEPT_NOTE):
        reply_id, reg_id = reg_id, original
        try:
            reg = await asyncio.to_thread(repo.registration_fields, reg_id)
        except Exception:
            await chat.send_message("Исходная заявка не найдена.")
            return
    name = escape(reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия")
    _mark_decider(ctx, update, reg_id)
    decided = reg.get(Registrations.DECISION)
    # Заблокированное решение ещё можно заменить запросом дилеру
    stuck = str(reg.get(Registrations.BOT_PROCESSED) or "").startswith("blocked:") and action in (ACTION_ASK, ACTION_INFO)
    if decided and decided != Decision.NEED_INFO and not stuck \
            and action not in (ACTION_PICK, ACTION_DPICK, ACTION_DNEW, ACTION_NAME):
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

    if action == ACTION_ASK:
        target = reg.get(Registrations.TARGET_PROJECT) or []
        project = await asyncio.to_thread(repo.project_fields, target[0]) if target else None
        text = decision_plan(reg, project).ask_text
        if not text:
            await chat.send_message(f"🛠 По «{name}» данных уже хватает — нажми решение ещё раз.")
            return
        await asyncio.to_thread(repo.update_registration, reg_id, {Registrations.BOT_PROCESSED: ""})
        await asyncio.to_thread(repo.set_decision, reg_id, Decision.NEED_INFO, text)
        await _apply_now(ctx)
        return

    if action == ACTION_ACCEPT_NOTE:
        pending[chat.id] = (action, reg_id, reply_id)
        await asyncio.to_thread(repo.set_awaiting, reg_id, action, reply_id, chat.id)
        await chat.send_message(
            f"🛠 «{name}»: напиши комментарий — он уйдёт дилеру строкой 💬 в карточке. Потом выберешь проект.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data=f"{CB_ADMIN}{ACTION_CANCEL}:")]]),
        )
        return

    if action in (ACTION_DECLINE, ACTION_INFO):
        pending[chat.id] = (action, reg_id, reply_id)
        await asyncio.to_thread(repo.set_awaiting, reg_id, action, reply_id, chat.id)
        what = "отказа" if action == ACTION_DECLINE else "запроса"
        await chat.send_message(
            f"🛠 «{name}»: напиши текст {what} — он уйдёт дилеру как есть.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data=f"{CB_ADMIN}{ACTION_CANCEL}:")]]),
        )
        return

    if action == ACTION_ACCEPT:
        target = reg.get(Registrations.TARGET_PROJECT) or []
        if target and not force and await _confirm_weak(chat, reg, "accf", reg_id, "-", reply_id):
            return
        if target and await _ask_rename(ctx, chat, reg_id, target[0], reply_id):
            return
        if target:
            await asyncio.to_thread(repo.set_decision, reg_id, Decision.ACCEPT_MERGE, _accept_note(ctx, reg_id))
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
            await create_named_project(ctx, project_id, reg_id, reply_id, suggestion)
        return

    if action == ACTION_DNEW:
        pending[chat.id] = (ACTION_DNEW, reg_id, reply_id)
        await asyncio.to_thread(repo.set_awaiting, reg_id, ACTION_DNEW, reply_id, chat.id)
        await chat.send_message(f"🛠 «{name}»: напиши название нового проекта латиницей. Передумал — нажми меню внизу.",
                                reply_markup=name_markup(reg, reg_id, ACTION_DNEW, reply_id))
        return

    if action == ACTION_PICK and project_id:
        if not force and await _confirm_weak(chat, reg, "pickf", reg_id, project_id, reply_id):
            return
        if await _ask_rename(ctx, chat, reg_id, project_id, reply_id):
            return
        await asyncio.to_thread(repo.set_decision, reg_id, Decision.ACCEPT_MERGE, _accept_note(ctx, reg_id), project_id)
        if reply_id:
            await asyncio.to_thread(repo.close_reply, reply_id, project_id)
        await _apply_now(ctx)
        return

    if action == ACTION_NEW:
        if not force and await _confirm_weak(chat, reg, "newf", reg_id, "-", reply_id):
            return
        pending[chat.id] = (ACTION_NEW, reg_id, reply_id)
        await asyncio.to_thread(repo.set_awaiting, reg_id, ACTION_NEW, reply_id, chat.id)
        await chat.send_message(
            f"🛠 «{name}»: напиши название нового проекта латиницей, как оно будет в Projects"
            + (" — или возьми вариант кнопкой" if dedup.latin_name(_payload(reg)) else "")
            + ". Передумал — просто нажми меню внизу.",
            reply_markup=name_markup(reg, reg_id, ACTION_NEW, reply_id),
        )


async def _confirm_weak(chat, reg: dict, force_action: str, reg_id: str, project_id: str, reply_id: str) -> bool:
    """Заявка на статус с незаполненными ответами — переспросить. True — спросили, дальше не идём."""
    rtype = reg.get(Registrations.REQUEST_TYPE)
    if rtype not in STATUS_REQUESTS:
        return False
    weak = weak_answers(_payload(reg))
    if not weak:
        return False
    data = f"{CB_ADMIN}{force_action}:{reg_id}:{project_id}" + (f":{reply_id}" if reply_id else "")
    rows = [[InlineKeyboardButton(f"✅ Да, подтверждаю {REQUEST_LABELS.get(rtype, 'статус')}", callback_data=data)],
            [InlineKeyboardButton("✏️ Запросить у дилера", callback_data=f"{CB_ADMIN}{ACTION_INFO}:{reg_id}")],
            [InlineKeyboardButton("Отмена", callback_data=f"{CB_ADMIN}{ACTION_CANCEL}:")]]
    name = escape(reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия")
    await chat.send_message(
        f"🛠 «{name}»: не заполнены поля:\n" + "\n".join(f"• {escape(w)}" for w in weak)
        + f"\n\nТочно подтверждаешь {REQUEST_LABELS.get(rtype, 'статус')}?",
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
                                                         callback_data=f"{CB_ADMIN}{ACTION_RENAME}:{reg_id}")]])
    await chat.send_message(
        f"🛠 Проект «{escape(name)}»: {problem}. Напиши новое название — "
        "или возьми транслит кнопкой. Передумал — нажми меню внизу.",
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


def name_markup(reg: dict, reg_id: str, action: str, reply_id: str = "") -> InlineKeyboardMarkup | None:
    suggestion = dedup.latin_name(_payload(reg))
    if not suggestion:
        return None
    data = f"{CB_ADMIN}{ACTION_NAME}:{reg_id}:{action}" + (f":{reply_id}" if reply_id else "")
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ {suggestion}", callback_data=data)]])


async def create_named_project(ctx: ContextTypes.DEFAULT_TYPE, action: str, reg_id: str, reply_id: str,
                               name: str) -> None:
    """Создать проект с этим названием: new — принять в него, dnew — отказ фиксируется в нём."""
    repo = ctx.bot_data["repo"]
    project_id = await asyncio.to_thread(repo.create_project, name)
    ctx.bot_data.setdefault("created_projects", set()).add(project_id)  # для итога «Проект создан»
    if action == ACTION_DNEW:
        await asyncio.to_thread(repo.update_registration, reg_id, {Registrations.TARGET_PROJECT: [project_id]})
    else:
        await asyncio.to_thread(repo.set_decision, reg_id, Decision.ACCEPT, _accept_note(ctx, reg_id), project_id)
        if reply_id:
            await asyncio.to_thread(repo.close_reply, reply_id, project_id)
    await _apply_now(ctx)


async def on_admin_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str, resume: bool = True) -> bool:
    """Текст админа в ответ на вопрос бота. True — обработано, дальше не передавать."""
    if not is_admin(update, ctx):
        return False
    pending = _pending(ctx)
    repo = ctx.bot_data["repo"]
    item = pending.pop(update.effective_chat.id, None)
    if not item and resume:
        # Бот мог перезапуститься, пока админ печатал, — берём ожидание из Airtable
        item = await asyncio.to_thread(repo.find_awaiting, update.effective_chat.id)
    if not item:
        return False  # бот ничего не ждёт — текст идёт дальше, в обычный диалог
    action, reg_id, reply_id = item
    chat = update.effective_chat
    _mark_decider(ctx, update, reg_id)
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
        await asyncio.to_thread(repo.set_awaiting, reg_id, ACTION_DPICK, reply_id, chat.id)
        await chat.send_message("🛠 Выбери проект кнопкой под сообщением об отказе.")
        return True
    if action in (ACTION_DNEW, ACTION_NEW, ACTION_RENAME):
        problem = dedup.name_problem(text)
        if problem:
            pending[chat.id] = item
            await asyncio.to_thread(repo.set_awaiting, reg_id, action, reply_id, chat.id)
            await chat.send_message(f"🛠 {problem}. Напиши название ещё раз.")
            return True
        if action == ACTION_RENAME:
            await _rename_and_merge(ctx, reg_id, reply_id, text)
        else:
            await create_named_project(ctx, action, reg_id, reply_id, text)
        return True
    if action == ACTION_ACCEPT_NOTE:
        # Комментарий запомнили — дальше как обычное «Принять»
        ctx.bot_data.setdefault("accept_notes", {})[reg_id] = text
        await on_admin_callback(update, ctx, f"{CB_ADMIN}{ACTION_ACCEPT}:{reg_id}" + (f"::{reply_id}" if reply_id else ""))
        return True
    else:
        decision = Decision.DECLINE if action == ACTION_DECLINE else Decision.NEED_INFO
        reg = await asyncio.to_thread(repo.registration_fields, reg_id)
        if (decision == Decision.DECLINE and reg.get(Registrations.REQUEST_TYPE) in UPGRADE_REQUESTS
                and not reg.get(Registrations.TARGET_PROJECT)):
            # Отказ всё равно фиксирует плейсмент уведомлением — сначала выбрать проект
            await asyncio.to_thread(repo.set_decision, reg_id, decision, text)
            await asyncio.to_thread(repo.set_awaiting, reg_id, ACTION_DPICK, reply_id, chat.id)
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
