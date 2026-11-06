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
from .cards import dealer_status, name_ref, others_line, project_line
from .decisions import COMPANY_PICKS, OWN_DESIGN, plan as decision_plan
from .scenarios.base import describe_items, format_value, is_vague
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
ACTION_DECLINE_BARE = "decn"   # отказ без комментария
ACTION_CALL_OK, ACTION_CALL_OTHER = "cok", "cot"   # звонок, предложенный партнёром: подтвердить / другое время
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
    if item and item[0] != ACTION_REMIND_USER and item[0] not in _ROLLBACK:
        await asyncio.to_thread(repo.clear_awaiting, item[1])


def _pending(ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    return ctx.bot_data.setdefault("admin_pending", {})


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
    ctx.bot_data.setdefault("stepwise", {})[reg_id] = {"final": STEPWISE[rtype], "payload": _payload(reg)}


def step_offer(ref: str, name: str, step: str, final: str, payload: dict, missing: str = "") -> str:
    """«Шаг 2 из 3 · предрегистрация?» — с пустыми ответами этой фазы."""
    total = 3 if final == "reg" else 2
    n = 2 if step == "pre" else 3
    asked = "регистрацию" if final == "reg" else "предрегистрацию"
    weak = weak_answers(payload, _STEP_PHASE[step])
    lines = [f"🛠 <b>Шаг {n} из {total}</b> · <code>{escape(name_ref(name, ref))}</code>",
             f"Дилер просил {asked}. Подтверждаем {_TARGET_RU[step]}?"]
    if weak:
        lines += ["Без ответа:"] + [f"• {escape(w)}" for w in weak]
    if missing:
        lines.append(f"По проекту не хватает: {escape(missing)}")
    return "\n".join(lines)


def upgrade_markup(project_id: str, person_id: str, target: str) -> InlineKeyboardMarkup:
    what = "предрегистрации" if target == "pre" else "регистрации"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⬆️ Повысить до {what}", callback_data=f"{CB_ADMIN}{ACTION_UP}:{project_id}:{person_id}:{target}")],
        [InlineKeyboardButton("Пока оставить", callback_data=f"{CB_ADMIN}{ACTION_UP_NO}:")],
    ])


_SHORT_STATUS = {"reg": "рег.", "pre": "предрег.", "notified": "увед."}


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
        ref, pname = f.get(Projects.PPR_REF) or "", (f.get(Projects.NAME) or "")[:24]
        rows = [(p, "reg") for p in f.get(Projects.REGISTERED) or []]
        rows += [(p, "pre") for p in f.get(Projects.PRE_REGISTERED) or []]
        rows += [(p, "notified") for p in f.get(Projects.NOTIFIED) or []]
        for person, status in rows:
            if person not in names:
                names[person] = (await asyncio.to_thread(repo.person_name, person)) or person
            short = names[person].split()[0] if names[person] else "?"
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
        await chat.send_message("🛠 В проектах нет менеджеров со статусом.")
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
    await chat.send_message(f"🛠 Чей статус изменить — повысить или откатить? ({page + 1}/{pages})", reply_markup=InlineKeyboardMarkup(rows))


def ask_markup(reg_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📨 Запросить у дилера", callback_data=f"{CB_ADMIN}{ACTION_ASK}:{reg_id}")],
        [InlineKeyboardButton("✏️ Свой текст запроса", callback_data=f"{CB_ADMIN}{ACTION_INFO}:{reg_id}")],
    ])


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
        if key in shown or key.startswith("_") or key in _META_KEYS or value in (None, "", []):
            continue
        title = _EXTRA_TITLES.get(key, key)
        body.append(f"• {escape(title)}: {escape(_ru(key, value))}")
    if rtype == "Update — Lost" and payload.get("Project Log"):
        body.append(escape(payload["Project Log"])[:300])
    if rtype in STATUS_REQUESTS and payload.get("Contact Level") == "Declared":
        body += ["", "⚠️ Уровень контакта — заявляю, без подтверждения. "
                     "Для предрегистрации рекомендуется запросить документы"]
    return body


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
        _pair(("Заказ", f"{order[8:10]}.{order[5:7]}.{order[:4]}" if order else "")),
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
    missing = project.get(Projects.PPR_NEXT_MISSING)
    lines.append(f"⚠️ Не хватает: {escape(missing)}" if missing else "✔️ Условия этапа выполнены (PPR Next Step Missing пуст)")
    lines += others_line(project, manager, dealer_status(project, manager))
    log_text = str(project.get(Projects.PROJECT_LOG) or "").strip()
    if log_text:
        lines += ["", "<b>Журнал проекта</b>", escape(log_text)]
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
                 dealer: str | None = None, extra: list[str] | None = None) -> str:
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
    if not extra:
        lines.append(f"🏗 <b>{escape(name)}</b>")

    body = _card_body(rtype, payload)
    if body:
        lines += [""] + body
    if extra:
        lines += extra
    if analysis:
        lines += ["", "🔎 " + escape(analysis)]
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


async def send_call_offer(bot, chat_id, reg_id: str, reg: dict, author: str | None) -> None:
    """Партнёр выбрал время звонка — отдельным сообщением, чтобы не потерялось: подтвердить или другое время."""
    for idx, (label, value) in enumerate(call_slots(_payload(reg))):
        name = reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия"
        await bot.send_message(
            chat_id,
            f"📞 <b>{escape(author or 'Партнёр')} предлагает звонок</b> · {escape(label.lower())}\n"
            f"🏗 {escape(name)}\n🕑 <b>{escape(value)}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"{CB_ADMIN}{ACTION_CALL_OK}:{reg_id}:{idx}"),
                InlineKeyboardButton("🕑 Другое время", callback_data=f"{CB_ADMIN}{ACTION_CALL_OTHER}:{reg_id}:{idx}"),
            ]]),
        )


async def _tell_partner(ctx: ContextTypes.DEFAULT_TYPE, reg: dict, text: str) -> bool:
    repo = ctx.bot_data["repo"]
    chats = await asyncio.to_thread(repo.chat_ids_for_people, reg.get(Registrations.SUBMITTED_BY) or [])
    for chat_id in chats:
        await ctx.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    return bool(chats)


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
        await send_card(
            ctx.bot, update.effective_chat.id,
            request_card(rec["fields"], rec["id"], await _author(repo, rec["fields"]),
                         rec["fields"].get(Registrations.ANALYSIS), await _dealer(repo, rec["fields"]),
                         await upgrade_digest(repo, rec["fields"]))
            + age_line(rec["fields"].get(Registrations.SUBMITTED_AT)),
            decision_markup(ctx, rec["id"], await merge_candidates(repo, rec["fields"])),
        )
        await send_call_offer(ctx.bot, update.effective_chat.id, rec["id"], rec["fields"],
                              await _author(repo, rec["fields"]))


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

    if action in (ACTION_CALL_OK, ACTION_CALL_OTHER) and project_id.isdigit():
        reg = await asyncio.to_thread(repo.registration_fields, reg_id)
        label, value, name = _call_text(reg, int(project_id))
        if action == ACTION_CALL_OTHER:
            pending[chat.id] = (ACTION_CALL_OTHER, reg_id, project_id)
            await chat.send_message(f"🛠 «{escape(name)}»: напиши, какое время звонка предложить, — уйдёт партнёру.",
                                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                        "Отмена", callback_data=f"{CB_ADMIN}{ACTION_CANCEL}:")]]))
            return
        who = routing.NAMES.get(routing.who_by_chat(ctx.bot_data, chat.id), "Егор")
        sent = await _tell_partner(ctx, reg, f"📞 {who} подтверждает звонок по «{escape(name)}»: <b>{escape(value)}</b>. "
                                             "До связи!")
        ctx.bot_data.setdefault("calls_confirmed", set()).add((reg_id, int(project_id)))
        await chat.send_message(f"🛠 Звонок по «{escape(name)}» подтверждён: {escape(value)}. "
                                + ("Партнёру написал. Напомню за час." if sent else "⚠️ Партнёра нет в боте — не написал."))
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
        rows = [[InlineKeyboardButton(f"⬆️ На {_TARGET_RU[t]}",
                                      callback_data=f"{CB_ADMIN}{ACTION_UP}:{parts[0]}:{parts[1]}:{t}")] for t in targets]
        if status:
            rows.append([InlineKeyboardButton("↩️ Откатить / аннулировать",
                                              callback_data=f"{CB_ADMIN}{ACTION_RB}:{parts[0]}:{parts[1]}")])
        rows.append([InlineKeyboardButton("Отмена", callback_data=f"{CB_ADMIN}{ACTION_UP_NO}:")])
        missing = project.get(Projects.PPR_NEXT_MISSING) or ""
        hint = f"\nДля следующего этапа не хватает: {escape(missing)}" if missing else ""
        await chat.send_message(f"🛠 {project_line(project)}: на какой статус?{hint}",
                                reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")
        return
    if action == ACTION_RB and len(parts) >= 2:
        project = await asyncio.to_thread(repo.project_fields, parts[0])
        status = dealer_status(project, parts[1])
        if not status:
            await chat.send_message("🛠 У этого менеджера в проекте уже нет статуса.")
            return
        rows = []
        if status in _LOWER_RU:
            rows.append([InlineKeyboardButton(f"↩️ На {_LOWER_RU[status]}",
                                              callback_data=f"{CB_ADMIN}{ACTION_RB_DOWN}:{parts[0]}:{parts[1]}")])
        rows.append([InlineKeyboardButton("✖️ Аннулировать — снять статус",
                                          callback_data=f"{CB_ADMIN}{ACTION_RB_CANCEL}:{parts[0]}:{parts[1]}")])
        rows.append([InlineKeyboardButton("Отмена", callback_data=f"{CB_ADMIN}{ACTION_UP_NO}:")])
        await chat.send_message(f"🛠 {project_line(project)}: что сделать со статусом?",
                                reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")
        return
    if action in _ROLLBACK and len(parts) >= 2:
        pending[chat.id] = (action, parts[0], parts[1])  # только в памяти: ожидание в Airtable — у заявок
        await chat.send_message("🛠 Напиши причину — она уйдёт менеджеру. Передумал — нажми меню внизу.")
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
    if original and action in (ACTION_ACCEPT, ACTION_DECLINE, ACTION_INFO, ACTION_ACCEPT_NOTE, ACTION_DECLINE_BARE):
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
        tail = f"::{reply_id}" if reply_id else ""
        await chat.send_message(
            f"🛠 «{name}» — принимаем. Напиши комментарий партнёру или отправь без него.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Отправить без комментария", callback_data=f"{CB_ADMIN}{ACTION_ACCEPT}:{reg_id}{tail}")],
                [InlineKeyboardButton("Отмена", callback_data=f"{CB_ADMIN}{ACTION_CANCEL}:")]]),
        )
        return

    if action == ACTION_DECLINE_BARE:
        pending[chat.id] = (ACTION_DECLINE, reg_id, reply_id)
        await on_admin_text(update, ctx, "", resume=False)
        return

    if action in (ACTION_DECLINE, ACTION_INFO):
        pending[chat.id] = (action, reg_id, reply_id)
        await asyncio.to_thread(repo.set_awaiting, reg_id, action, reply_id, chat.id)
        rows = [[InlineKeyboardButton("Отмена", callback_data=f"{CB_ADMIN}{ACTION_CANCEL}:")]]
        if action == ACTION_DECLINE:
            text = f"🛠 «{name}» — отклоняем. Напиши причину партнёру или отправь без комментария."
            tail = f"::{reply_id}" if reply_id else ""
            rows.insert(0, [InlineKeyboardButton("Отправить без комментария",
                                                 callback_data=f"{CB_ADMIN}{ACTION_DECLINE_BARE}:{reg_id}{tail}")])
        else:
            text = f"🛠 «{name}»: напиши вопрос — он уйдёт партнёру, ответ придёт сюда."
        await chat.send_message(text, reply_markup=InlineKeyboardMarkup(rows))
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
            await chat.send_message("🛠 Записал. Нажми решение по заявке ещё раз.")
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
            await create_named_project(ctx, project_id, reg_id, reply_id, suggestion, chat)
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

    if action in DOWNGRADE:
        rtype = reg.get(Registrations.REQUEST_TYPE)
        if rtype in _PRE_OF:
            await asyncio.to_thread(repo.update_registration, reg_id, {Registrations.REQUEST_TYPE: _PRE_OF[rtype]})
            missing = "; ".join(w.split(" — «")[0] for w in weak_answers(_payload(reg)))
            notes = ctx.bot_data.setdefault("accept_notes", {})
            note = "Принято как предрегистрация." + (f" Для регистрации не хватает: {missing}." if missing else "")
            notes[reg_id] = (notes[reg_id] + "\n" if notes.get(reg_id) else "") + note
        tail = f":{reply_id}" if reply_id else ""
        await on_admin_callback(update, ctx, f"{CB_ADMIN}{DOWNGRADE[action]}:{reg_id}:{project_id or '-'}{tail}")
        return

    if action == ACTION_NEW:
        name_ready = ctx.bot_data.setdefault("new_names", {}).pop(reg_id, "") if force else ""
        if name_ready:   # название уже выбрано, переспрос по пустым полям подтверждён
            await create_named_project(ctx, ACTION_NEW, reg_id, reply_id, name_ready, chat, force=True)
            return
        pending[chat.id] = (ACTION_NEW, reg_id, reply_id)
        await asyncio.to_thread(repo.set_awaiting, reg_id, ACTION_NEW, reply_id, chat.id)
        await chat.send_message(
            f"🛠 «{name}»: напиши название нового проекта латиницей, как оно будет в Projects "
            "(образец: PVSMART InsaitBC FL3 — клиент, объект с кодом типа, этаж)"
            + (" — или возьми вариант кнопкой" if dedup.latin_name(_payload(reg)) else "")
            + ". Передумал — просто нажми меню внизу.",
            reply_markup=name_markup(reg, reg_id, ACTION_NEW, reply_id),
        )


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
    data = f"{CB_ADMIN}{force_action}:{reg_id}:{project_id}" + (f":{reply_id}" if reply_id else "")
    rows = [[InlineKeyboardButton(f"✅ Да, подтверждаю {REQUEST_LABELS.get(rtype, 'статус')}", callback_data=data)]]
    if rtype in _PRE_OF:
        down = f"{CB_ADMIN}{_DOWN_OF[force_action]}:{reg_id}:{project_id}" + (f":{reply_id}" if reply_id else "")
        rows.append([InlineKeyboardButton("⬇️ Принять как предрегистрацию", callback_data=down)])
    rows += [[InlineKeyboardButton("✏️ Запросить у дилера", callback_data=f"{CB_ADMIN}{ACTION_INFO}:{reg_id}")],
            [InlineKeyboardButton("Отмена", callback_data=f"{CB_ADMIN}{ACTION_CANCEL}:")]]
    name = escape(reg.get(Registrations.DEALER_PROJECT_NAME) or "без названия")
    head = (f"🛠 «{name}» → новый проект <b>{escape(new_name)}</b> (референс присвоится при создании)"
            if new_name else f"🛠 «{name}»")
    await chat.send_message(
        f"{head}: не заполнены поля:\n" + "\n".join(f"• {escape(w)}" for w in weak)
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
    if action == ACTION_CALL_OTHER:
        if not text.strip():
            pending[chat.id] = item
            await chat.send_message("🛠 Пустой текст — напиши время.")
            return True
        reg = await asyncio.to_thread(repo.registration_fields, reg_id)
        _, value, name = _call_text(reg, int(reply_id or 0))
        who = routing.NAMES.get(routing.who_by_chat(ctx.bot_data, chat.id), "Егор")
        sent = await _tell_partner(ctx, reg, f"📞 По звонку по «{escape(name)}» ({escape(value)}) {who} предлагает "
                                             f"другое время: <b>{escape(text.strip())}</b>. Если не подходит — "
                                             f"напиши {who} напрямую.")
        await chat.send_message("🛠 Партнёру отправил." if sent else "🛠 ⚠️ Партнёра нет в боте — не отправил.")
        return True
    if action in _ROLLBACK:
        reason = text.strip()
        if not reason:
            pending[chat.id] = item
            await chat.send_message("🛠 Пустой текст — напиши причину.")
            return True
        who = routing.who_by_chat(ctx.bot_data, chat.id)
        await ctx.bot_data["rollback_status"](ctx.application, reg_id, reply_id, _ROLLBACK[action], reason, who)
        return True
    _mark_decider(ctx, update, reg_id)
    if action != ACTION_REMIND_USER:
        await asyncio.to_thread(repo.clear_awaiting, reg_id)
    text = text.strip()
    if not text and action != ACTION_DECLINE:   # отказ — можно без комментария
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
            await create_named_project(ctx, action, reg_id, reply_id, text, chat)
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
