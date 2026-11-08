"""Карточки ответа по решению: дилеру после Accept и копия админу («Взято в работу»).

Строятся по свежему состоянию проекта после записи: референс, статус дилера,
другие партнёры на объекте (без имён), PPR Next Step Missing.
"""
from __future__ import annotations

import logging
import re
from html import escape

from .airtable_ids import Projects

log = logging.getLogger(__name__)

SEPARATOR = "━━━━━━━━━━━━━━━"
SIMPLIFIED = "Simplified (<30k)"
FULL_TRACK_THRESHOLD = 30_000
EXW_FACTOR = 0.45

STATUS_RU = {"registered": "регистрация", "pre_registered": "предрегистрация", "notified": "уведомление"}
ORDINAL = {1: "первый", 2: "второй", 3: "третий", 4: "четвёртый", 5: "пятый"}
COUNT_GEN = {1: "одного", 2: "двух", 3: "трёх", 4: "четырёх", 5: "пяти"}

CONTACT_RU = {
    "Declared": "контакт есть, подтвердить пока нечем",
    # Старое значение: документы бюро контакт с клиентом не подтверждают (решение Егора 08.11)
    "Indirect (bureau docs)": "документы бюро — это не подтверждение контакта с клиентом",
    "Verifiable (email/contact)": "переписка или прямой контакт с клиентом",
    "Joint meeting with AW": "готов к встрече с клиентом вместе с Andreu World",
}

# PPR Next Step Missing → строка карточки (порядок как в формуле)
NEXT_STEP_LINES = (
    ("Contact Level", "🤝 Подтверждение контакта с клиентом — сейчас «{contact}». "
                      "Нужна переписка с клиентом или его контакт."),
    ("Dossier Files", "📎 Материалы — визуализации, планировки, ведомость."),
    ("Expected Order Date", "📅 Месяц размещения заказа."),
    ("EXW Potential", "💶 Уточнённый объём по прайсу."),
    ("Object Address", "📍 Адрес объекта."),
    ("Specifier", "🏛 Архитектурное бюро."),
    ("AW Alternatives", "🔄 Предлагаешь ли клиенту альтернативы Andreu World."),
    ("Parallel Registrations", "📋 Заявлен ли объём у других производителей мебели."),
    ("Quotation", "🧾 Номер квотации Andreu World."),
)
ARCHITECT_PENDING = "🏛 Архитектурное бюро — как только клиент его выберет."
OTHER_DATA = "📋 Другие данные — уточню и напишу отдельно."


def next_step_lines(raw: str, contact: str) -> list[str]:
    """Ключи формулы → строки карточки; неизвестный ключ — в лог и «другие данные»."""
    out = []
    for key in [k.strip() for k in (raw or "").split(";") if k.strip()]:
        if key.startswith("Simplified"):
            continue
        hit = next((line for k, line in NEXT_STEP_LINES if key.startswith(k)), None)
        if hit is None:
            log.warning("Неизвестный ключ PPR Next Step Missing: %r", key)
            hit = OTHER_DATA
        line = hit.format(contact=contact)
        if line not in out:
            out.append(line)
    return out


def dealer_status(project: dict, dealer: str) -> str | None:
    if dealer in (project.get(Projects.REGISTERED) or []):
        return "registered"
    if dealer in (project.get(Projects.PRE_REGISTERED) or []):
        return "pre_registered"
    if dealer in (project.get(Projects.NOTIFIED) or []):
        return "notified"
    return None


def _money(v: float) -> str:
    return f"{v:,.0f} €".replace(",", " ")


def others_line(project: dict, dealer: str, status: str | None) -> list[str]:
    """Кто ещё на объекте — без имён; при предрегистрации — место в очереди."""
    registered = [c for c in project.get(Projects.REGISTERED) or [] if c != dealer]
    pre_all = list(project.get(Projects.PRE_REGISTERED) or [])
    pre_others = [c for c in pre_all if c != dealer]
    if registered:
        lines = ["🔎 На объекте есть действующая регистрация другого партнёра"]
    elif pre_others:
        lines = ["🔎 На объекте есть действующая предрегистрация другого партнёра"]
    else:
        lines = ["🔎 Других партнёров на объекте нет"]
    if status == "pre_registered" and dealer in pre_all and len(pre_all) > 1:
        n, total = pre_all.index(dealer) + 1, len(pre_all)
        lines.append(f"🔢 Твой номер в предрегистрации — {ORDINAL.get(n, str(n))} "
                     f"из {COUNT_GEN.get(total, str(total))}")
    return lines


def name_ref(name: str, ref: str) -> str:
    """«Kolesa Almaty Reception AW260249» — название и референс (только из поля PPR Ref) одной строкой."""
    return " ".join(x for x in ((name or "").strip(), (ref or "").strip()) if x)


def project_line(project: dict) -> str:
    """Строка проекта моноширинным — копируется целиком одним нажатием."""
    return f"<code>{escape(name_ref(project.get(Projects.NAME) or '', project.get(Projects.PPR_REF) or ''))}</code>"


def header(project: dict, dealer: str, dealer_name: str, title: str = "✅ Заявка принята") -> list[str]:
    status = dealer_status(project, dealer)
    lines = [f"<b>{title}</b>"]
    lines.append(f"🏗 {project_line(project)}")
    status_text = (f" · статус: {'уведомление о плейсменте' if status == 'notified' else STATUS_RU[status]}"
                   if status else "")
    lines.append(f"👤 {escape(dealer_name)}{status_text}")
    lines += others_line(project, dealer, status)
    expiry = project.get(Projects.STATUS_EXPIRY)
    if status in ("pre_registered", "registered") and expiry:
        d = str(expiry)[:10]
        lines.append(f"⏳ Статус действует до {d[8:10]}.{d[5:7]}.{d[:4]}")
    lines.append(SEPARATOR)
    return lines


def next_step_body(project: dict, dealer: str) -> list[str]:
    status = dealer_status(project, dealer)
    if status == "registered":
        return ["🏁 Регистрация — финальный этап программы."]
    others_registered = [c for c in project.get(Projects.REGISTERED) or [] if c != dealer]
    if others_registered:
        return ["ℹ️ Регистрация на объекте уже у другого партнёра — следующий этап сейчас недоступен."]
    raw = project.get(Projects.PPR_NEXT_MISSING) or ""
    target = "регистрацию" if status == "pre_registered" else "предрегистрацию"
    contact = CONTACT_RU.get(project.get(Projects.CONTACT_LEVEL) or "", "не указано")
    lines = next_step_lines(raw, contact)
    if project.get(Projects.PROJECT_STAGE) == "Architect selection":
        lines = [ARCHITECT_PENDING if ln == "🏛 Архитектурное бюро." else ln for ln in lines]
    if not lines:
        what = "регистрации" if status == "pre_registered" else "предрегистрации"
        return [f"✔️ Условия для {what} выполнены — запроси {target}: "
                "«Обновить проект» → «Запросить предрегистрацию / регистрацию»."]
    return [f"➡️ Для перехода на {target} не хватает:", "", *lines, "",
            "👉 Всё это можно добавить через «Обновить проект» → «Дополнить материалы»."]


def simplified_body(pl_volume: float | None) -> list[str]:
    lines = []
    if pl_volume:
        lines.append(f"💶 Объём по прайсу: {_money(pl_volume)} — ниже порога {_money(FULL_TRACK_THRESHOLD)}")
        lines.append("")
    lines += [
        "ℹ️ Для проектов до 30 000 € по прайсу действует регистрация по упрощённой схеме: защита — номер "
        "квотации на срок её действия. Квотацию запрашивай в бэк-офисе, Егор в копии. Номер пришли "
        "кнопкой «🧾 Прислать номер квотации» в карточке проекта («Мои проекты»).",
        "",
        "📈 Если объём вырастет выше 30 000 € — напиши об этом через «Обновить проект» → «Сообщить новость», "
        "и я подскажу, что нужно для предрегистрации.",
    ]
    return lines


def pl_volume_of(payload: dict, project: dict) -> float | None:
    pl = payload.get("PL Volume")
    if isinstance(pl, (int, float)):
        return pl
    exw = project.get(Projects.EXW_POTENTIAL)
    return round(exw / EXW_FACTOR) if exw else None


def subject_lines(project: dict) -> list[str]:
    """Подсказка для темы писем; вторая строка — моноширинная, копируется одним нажатием."""
    return ["✉️ Указывай название проекта и референс в теме писем по этому проекту:", project_line(project)]


def accept_card(project: dict, dealer: str, dealer_name: str, payload: dict, first_time: bool,
                note: str = "", title: str = "✅ Заявка принята") -> str:
    """Ответ дилеру на принятую заявку о статусе (уведомление / предрегистрация / регистрация)."""
    lines = header(project, dealer, dealer_name, title)
    if project.get(Projects.PPR_TRACK) == SIMPLIFIED:
        lines += simplified_body(pl_volume_of(payload, project))
    else:
        lines += next_step_body(project, dealer)
    if first_time and project.get(Projects.PPR_REF):
        lines += ["", *subject_lines(project)]
    if note:
        lines += ["", f"💬 Комментарий: {escape(note)}"]
    return "\n".join(lines)


def declined_upgrade_card(project: dict, dealer: str, dealer_name: str, payload: dict, asked: str,
                          note: str, first_time: bool) -> str:
    """Отказ в пред/регистрации: плейсмент всё равно зафиксирован уведомлением."""
    lines = header(project, dealer, dealer_name, f"❌ {asked.capitalize()} не подтверждена")
    kept = dealer_status(project, dealer)
    if kept in ("pre_registered", "registered"):
        lines += [f"📌 Статус остаётся «{STATUS_RU[kept]}»."]
    else:
        lines += ["📌 Плейсмент зафиксирован как уведомление."]
    if note:
        lines.append(f"💬 Причина: {escape(note)}")
    lines.append("")
    if project.get(Projects.PPR_TRACK) == SIMPLIFIED:
        lines += simplified_body(pl_volume_of(payload, project))
    else:
        lines += next_step_body(project, dealer)
    if first_time and project.get(Projects.PPR_REF):
        lines += ["", *subject_lines(project)]
    return "\n".join(lines)


def update_card(project: dict, dealer: str, dealer_name: str, body: str, note: str = "",
                title: str = "✅ Заявка принята", next_step: bool = False) -> str:
    """Ответ на принятое обновление (данные, новость, квотация) — без смены статуса.
    next_step — показать, что осталось до следующего этапа (после «Дополнить материалы»)."""
    lines = header(project, dealer, dealer_name, title) + [body]
    if next_step and project.get(Projects.PPR_TRACK) != SIMPLIFIED:
        lines += [""] + next_step_body(project, dealer)
    if note:
        lines += ["", f"💬 Комментарий: {escape(note)}"]
    return "\n".join(lines)


# Обращения к дилеру → третье лицо в копии проверяющему (правило 1.11)
_TO_THIRD = (
    ("🔢 Твой номер в предрегистрации", "🔢 Номер дилера в предрегистрации"),
    ("➡️ Для перехода на", "➡️ Дилеру для перехода на"),
    ("📈 Если объём вырастет выше 30 000 € — напиши об этом", "📈 Если объём вырастет выше 30 000 €, дилер напишет об этом"),
)


def admin_copy(card: str, sent: bool, extra: list[str] | None = None) -> str:
    """Та же карточка проверяющему: «🛠 Взято в работу», без обращений к дилеру."""
    text = card.replace("<b>✅ Заявка принята</b>", "🛠 <b>Взято в работу</b>", 1)
    text = text.replace("<b>✅ Статус по проекту изменён</b>", "🛠 <b>Статус по проекту изменён</b>", 1)
    if text.startswith("<b>❌"):
        text = "🛠 " + text
    out, skip_next = [], False
    for line in text.split("\n"):
        if skip_next:
            skip_next = False
            continue
        if line.startswith("👉") or line.startswith("ℹ️ Для проектов до 30 000 €"):
            continue
        if line.startswith("✉️"):
            skip_next = True   # следующая строка — название для темы писем
            continue
        m = re.match(r"✔️ Условия для (\S+) выполнены — запроси (\S+):", line)
        if m:
            line = f"✔️ Условия для {m.group(1)} выполнены — дилер может запросить {m.group(2)}."
        for a, b in _TO_THIRD:
            line = line.replace(a, b)
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    tail = list(extra or [])
    tail.append("📨 Дилеру отправлено" if sent else "📨 Дилеру не отправлено — у него нет Telegram в базе")
    return "\n".join(out) + "\n\n" + "\n".join(tail)
