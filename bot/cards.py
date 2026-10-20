"""Карточки ответа по решению: дилеру после Accept и копия админу («Взято в работу»).

Строятся по свежему состоянию проекта после записи: референс, статус дилера,
другие партнёры на объекте (без имён), PPR Next Step Missing.
"""
from __future__ import annotations

from html import escape

from .airtable_ids import Projects

SEPARATOR = "━━━━━━━━━━━━━━━"
SIMPLIFIED = "Simplified (<30k)"
FULL_TRACK_THRESHOLD = 30_000
EXW_FACTOR = 0.45

STATUS_RU = {"registered": "регистрация", "pre_registered": "предрегистрация", "notified": "уведомление"}
ORDINAL = {1: "первый", 2: "второй", 3: "третий", 4: "четвёртый", 5: "пятый"}
COUNT_GEN = {1: "одного", 2: "двух", 3: "трёх", 4: "четырёх", 5: "пяти"}

CONTACT_RU = {
    "Declared": "заявляю, без подтверждения",
    "Indirect (bureau docs)": "документы от бюро",
    "Verifiable (email/contact)": "переписка или контакт",
    "Joint meeting with AW": "готов к совместной встрече",
}

# PPR Next Step Missing → строка карточки (порядок как в формуле)
NEXT_STEP_LINES = (
    ("Contact Level", "🤝 Подтверждение контакта с клиентом или бюро — сейчас «{contact}». "
                      "Нужен документ от бюро, переписка или контакт."),
    ("Dossier Files", "📎 Файлы досье — визуализации, планировки, список серий."),
    ("Expected Order Date", "📅 Месяц размещения заказа."),
    ("EXW Potential", "💶 Уточнённый объём."),
    ("Object Address", "📍 Адрес объекта."),
    ("Specifier", "🏛 Архитектурное бюро."),
    ("AW Alternatives", "🔄 Декларация об альтернативах."),
    ("Parallel Registrations", "📋 Параллельные регистрации."),
    ("Quotation №", "🧾 Номер квотации."),
)
ARCHITECT_PENDING = "🏛 Архитектурное бюро — как только клиент его выберет."


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
        lines = ["🔎 Других статусов на объекте нет"]
    if status == "pre_registered" and dealer in pre_all and len(pre_all) > 1:
        n, total = pre_all.index(dealer) + 1, len(pre_all)
        lines.append(f"🔢 Твой номер в предрегистрации — {ORDINAL.get(n, str(n))} "
                     f"из {COUNT_GEN.get(total, str(total))}")
    return lines


def header(project: dict, dealer: str, dealer_name: str, title: str = "✅ Заявка принята") -> list[str]:
    status = dealer_status(project, dealer)
    lines = [f"<b>{title}</b>"]
    ref = project.get(Projects.PPR_REF)
    if ref:
        lines.append(f"🔖 Референс: <b>{escape(ref)}</b>")
    lines.append(f"🏗 Название проекта: <b>{escape(project.get(Projects.NAME) or '—')}</b>")
    status_text = f" · статус {STATUS_RU[status]}" if status else ""
    lines.append(f"👤 {escape(dealer_name)}{status_text}")
    lines += others_line(project, dealer, status)
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
    lines = [line.format(contact=contact) for key, line in NEXT_STEP_LINES if key in raw]
    if project.get(Projects.PROJECT_STAGE) == "Architect selection":
        lines = [ARCHITECT_PENDING if ln == "🏛 Архитектурное бюро." else ln for ln in lines]
    if not lines:
        return ["✔️ Все данные для следующего этапа есть. Нажми «Обновить проект» → «Запросить следующий этап»."]
    return [f"➡️ Для перехода на {target} не хватает:", "", *lines, "",
            "👉 Всё это можно добавить через «Обновить проект» → «Дополнить материалы»."]


def simplified_body(pl_volume: float | None) -> list[str]:
    lines = []
    if pl_volume:
        lines.append(f"💶 Объём по прайсу: {_money(pl_volume)} — ниже порога {_money(FULL_TRACK_THRESHOLD)}")
        lines.append("")
    lines += [
        "ℹ️ Для таких проектов предрегистрация и регистрация не предусмотрены. Квотацию запрашивай "
        "напрямую в бэк-офисе, Егор в копии. Первый запросивший получает проектные условия, защита "
        "действует в течение срока квотации.",
        "",
        "📈 Если объём вырастет выше 30 000 € — обнови его через «Обновить проект», и я подскажу, "
        "что нужно для предрегистрации.",
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
    subject = f"{project.get(Projects.NAME) or ''} {project.get(Projects.PPR_REF) or ''}".strip()
    return ["✉️ Указывай название проекта и референс в теме писем по этому проекту:",
            f"<code>{escape(subject)}</code>"]


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
                title: str = "✅ Заявка принята") -> str:
    """Ответ на принятое обновление (данные, новость, выигран/проигран) — без смены статуса."""
    lines = header(project, dealer, dealer_name, title) + [body]
    if note:
        lines += ["", f"💬 Комментарий: {escape(note)}"]
    return "\n".join(lines)


def admin_copy(card: str, sent: bool, extra: list[str] | None = None) -> str:
    """Та же карточка админу: «🛠 Взято в работу» вместо «✅ Заявка принята»."""
    text = card.replace("<b>✅ Заявка принята</b>", "🛠 <b>Взято в работу</b>", 1)
    if text.startswith("<b>❌"):
        text = "🛠 " + text
    tail = list(extra or [])
    tail.append("📨 Дилеру отправлено" if sent else "📨 Дилеру не отправлено — нет Telegram")
    return text + "\n\n" + "\n".join(tail)
