"""Действия по проекту из Projects: новость, «Выигран», «Проиграл», «Дополнить материалы».

Проект выбирается кнопкой; его id и название лежат в ответах под служебным ключом
_target. Всё уходит в Inbox (Registrations) с Target Project — в Projects бот не пишет:
проект может быть у нескольких дилеров, решение за Егором.
"""
from __future__ import annotations

from dataclasses import replace

from ..airtable_ids import Projects, RequestType
from .base import DETAIL, Question, Scenario
from .new_project import (
    QUESTIONS, STAGE_ARCHITECT_SELECTION, current_pl, exw, location, project_log, specifier,
)

CONFIRMED_BY_EGOR = "Принято, Егор подтвердит."
NEWS_DONE = "Принято, разберу и свяжу с проектом."
FILL_DONE = "Информацию по проекту получили. Ответ дадим в течение 72 часов — придёт сюда."


def _target_name(a: dict) -> str:
    return (a.get("_target") or {}).get("name", "")


def _plain_verdict(stage_label: str) -> list[str]:
    return [f"Подаётся как: <b>{stage_label}</b>. Решение — после проверки."]


# ---------- Сообщить новость ----------

def _news_derive(a: dict) -> dict:
    out: dict = {"Dealer Project Name": _target_name(a)}
    if a.get("Dealer News"):
        out["Project Log"] = f"Новости от дилера: {a['Dealer News']}"
    if a.get("Dealer News Audio"):
        # Голосовое — во вложениях заявки; здесь ссылка на файл по имени
        out["Dealer News Audio"] = a["Dealer News Audio"]
    return out


NEWS = Scenario(
    code="project_news",
    title="Сообщить новость",
    questions=(
        Question(
            key="Dealer News",
            label="Новость",
            prompt="Что нового по проекту? Напишите или запишите голосовое.",
            hint="Стадия, решения клиента, сроки, конкуренты — всё, что изменилось",
        ),
    ),
    phase_titles={1: "Новость"},
    stage=lambda p, a, m: (RequestType.UPDATE_INFO, "новость по проекту"),
    derive=_news_derive,
    verdict=lambda a, s, m: _plain_verdict(s),
    project_name_key="Dealer Project Name",
    done_text=NEWS_DONE,
    auto_submit=True,
)


# ---------- Ответ на запрос доп. информации ----------

def _reply_derive(a: dict) -> dict:
    out: dict = {"Dealer Project Name": _target_name(a), "In Reply To": a.get("_reply_to")}
    if a.get("Dealer News"):
        out["Project Log"] = f"Ответ дилера на запрос: {a['Dealer News']}"
    if a.get("Dealer News Audio"):
        out["Dealer News Audio"] = a["Dealer News Audio"]
    return out


INFO_REPLY = Scenario(
    code="info_reply",
    title="Ответ на запрос",
    questions=(
        Question(
            key="Dealer News",
            label="Ответ",
            prompt="Напишите ответ или запишите голосовое.",
        ),
        Question(
            key="Extra Materials",
            label="Файлы",
            prompt="Есть файлы к ответу? Пришлите файлы или ссылку — или нажмите «Нет».",
            kind="files",
            choices=(("Нет", "нет"),),
        ),
    ),
    phase_titles={1: "Ответ"},
    stage=lambda p, a, m: (RequestType.UPDATE_INFO, "ответ на запрос"),
    derive=_reply_derive,
    verdict=lambda a, s, m: _plain_verdict(s),
    project_name_key="Dealer Project Name",
    done_text="Принято, ответ передан Егору.",
    auto_submit=True,
)


# ---------- Выигран ----------

WON = Scenario(
    code="project_won",
    title="Проект выигран",
    questions=(
        Question(
            key="Order / Proforma №",
            label="Заказ / проформа №",
            prompt="Номер заказа или проформы, если известен.",
            optional=True,
        ),
    ),
    phase_titles={1: "Выигран"},
    stage=lambda p, a, m: (RequestType.UPDATE_WON, "проект выигран"),
    derive=lambda a: {"Dealer Project Name": _target_name(a)},
    verdict=lambda a, s, m: _plain_verdict(s),
    project_name_key="Dealer Project Name",
    done_text=CONFIRMED_BY_EGOR,
    auto_submit=True,
)


# ---------- Проиграл ----------

# Значения = варианты поля Lost Reason в Projects
LOST_REASONS = (
    ("Ушли к равному конкуренту", "Lost to equal competitor"),
    ("Ушли к более дешёвому предложению", "Lost to cheaper offer"),
    ("Нет бюджета", "No budget"),
    ("Проект отменён", "Project cancelled"),
)


def _lost_derive(a: dict) -> dict:
    out: dict = {"Dealer Project Name": _target_name(a)}
    reason = a.get("Lost Reason Answer")
    if reason in {v for _, v in LOST_REASONS}:
        out["Lost Reason"] = reason
    elif reason:
        out["Project Log"] = f"Причина проигрыша (со слов дилера): {reason}"
    return out


LOST = Scenario(
    code="project_lost",
    title="Проект проигран",
    questions=(
        Question(
            key="Lost Reason Answer",
            label="Причина",
            prompt="Почему проект проигран?",
            kind="choice",
            choices=LOST_REASONS + (("Другое", DETAIL),),
            detail_prompt="Напишите причину.",
        ),
    ),
    phase_titles={1: "Проигран"},
    stage=lambda p, a, m: (RequestType.UPDATE_LOST, "проект проигран"),
    derive=_lost_derive,
    verdict=lambda a, s, m: _plain_verdict(s),
    project_name_key="Dealer Project Name",
    done_text=CONFIRMED_BY_EGOR,
    auto_submit=True,
    payload_skip=("Lost Reason Answer",),  # → Lost Reason или строка Project Log
)


# ---------- Дополнить материалы ----------

MATERIAL_KEYS = tuple(q.key for q in QUESTIONS if q.group == "Материалы")
EXTRA_FILES_KEY = "Extra Materials"


def _ask_only(q: Question) -> Question:
    """Вопрос сценария «Новый проект», который задаётся, только если поле пустое в проекте."""
    def ask_if(a: dict, q: Question = q) -> bool:
        return q.key in a.get("_ask", ()) and q.applies(a)
    return replace(q, ask_if=ask_if)


FILL_QUESTIONS = tuple(_ask_only(q) for q in QUESTIONS) + (
    Question(
        key=EXTRA_FILES_KEY,
        label="Материалы",
        prompt=(
            "Все данные по проекту на месте. Пришлите новые материалы — визуализации, планировки, "
            "документы бюро. Файлы по одному, больше 5 МБ — ссылкой. Когда закончите — «Готово»."
        ),
        kind="files",
        ask_if=lambda a: bool(a.get("_files_only")),
    ),
)


def _empty(v) -> bool:
    return v in (None, "", [], 0)


def gaps_for(project_fields: dict, ppr_status: str) -> tuple[list[str], dict]:
    """Какие вопросы задать по проекту: (ключи вопросов, подсказки-ответы из проекта).

    Фаза 1 — по пустым полям проекта. Следующая фаза — по PPR Missing; для уведомления
    формула пустая (считает только при пред/регистрации), поэтому те же поля проверяем сами.
    """
    f = project_fields
    keys: list[str] = []
    known: dict = {}
    stage = f.get(Projects.PROJECT_STAGE)
    if stage:
        known["Project Stage"] = stage
    if f.get(Projects.COUNTRY):
        known["Country"] = f[Projects.COUNTRY]

    def need(key: str) -> None:
        if key not in keys:
            keys.append(key)

    # Фаза 1
    for field, key in (
        (Projects.END_CLIENT, "End Client"),
        (Projects.OBJECT_NAME, "Object Name"),
        (Projects.OBJECT_PART, "Object Part"),
        (Projects.OBJECT_ADDRESS, "Object Address"),
        (Projects.PROJECT_STAGE, "Project Stage"),
        (Projects.EXW_POTENTIAL, "PL Volume"),
        (Projects.AW_ALTERNATIVES, "AW Alternatives"),
    ):
        if _empty(f.get(field)):
            need(key)
    if _empty(f.get(Projects.COUNTRY)) or _empty(f.get(Projects.CITY)):
        for k in ("Object Location", "Country", "City"):  # страна и город — только если «Другое»
            need(k)
    if _empty(f.get(Projects.SPECIFIER)) and stage != STAGE_ARCHITECT_SELECTION:
        need("Specifier")
        need("Lead Architect")  # ГАП в Projects отдельного поля нет — спрашиваем вместе с бюро

    # Следующая фаза
    if ppr_status == "registered":
        return keys, known
    missing_names = f.get(Projects.PPR_MISSING) or ""
    if not missing_names:
        checks = (
            (Projects.EXW_POTENTIAL, "EXW Potential"), (Projects.EXPECTED_ORDER_DATE, "Expected Order Date"),
            (Projects.OBJECT_ADDRESS, "Object Address"), (Projects.SPECIFIER, "Specifier"),
            (Projects.CONTACT_LEVEL, "Contact Level"), (Projects.AW_ALTERNATIVES, "AW Alternatives"),
            (Projects.PARALLEL_REGISTRATIONS, "Parallel Registrations"), (Projects.DOSSIER_FILES, "Dossier Files"),
        )
        missing_names = "; ".join(name for field, name in checks if _empty(f.get(field)))
    by_name = {
        "EXW Potential": ("PL Volume",),
        "Expected Order Date": ("Order Month",),
        "Object Address": ("Object Address",),
        "Specifier": ("Specifier", "Lead Architect") if stage != STAGE_ARCHITECT_SELECTION else (),
        "Contact Level": ("Contact Level",),
        "AW Alternatives": ("AW Alternatives",),
        "Parallel Registrations": ("Parallel Registrations",),
        "Dossier Files": MATERIAL_KEYS,
    }
    for name, qkeys in by_name.items():
        if name in missing_names:
            for k in qkeys:
                need(k)
    if ppr_status == "pre_registered" and _empty(f.get(Projects.QUOTATION)):
        need("Quotation / Proforma №")  # для регистрации нужна квотация
    return keys, known


def fill_preset(target: dict, project_fields: dict, ppr_status: str) -> dict:
    keys, known = gaps_for(project_fields, ppr_status)
    preset = {"_target": target, **known}
    if keys:
        preset["_ask"] = keys
    else:
        preset["_files_only"] = True
    return preset


def _fill_derive(a: dict) -> dict:
    out: dict = {"Dealer Project Name": _target_name(a)}
    if "Object Location" in a:
        out.update(location(a))
    out.update(specifier(a))
    pl = current_pl(a)
    if pl is not None:
        out["PL Volume"] = pl
        out["EXW Potential"] = exw(pl)
    log = project_log(a)
    if log:
        out["Project Log"] = "\n".join(log)
    return out


FILL = Scenario(
    code="project_fill",
    title="Дополнить материалы",
    questions=FILL_QUESTIONS,
    phase_titles={1: "Уведомление", 2: "Предрегистрация", 3: "Регистрация"},
    stage=lambda p, a, m: (RequestType.UPDATE_INFO, "дополнение по проекту"),
    derive=_fill_derive,
    verdict=lambda a, s, m: _plain_verdict(s),
    project_name_key="Dealer Project Name",
    done_text=FILL_DONE,
    start_phase=3,
    payload_skip=("Object Location", "PL Volume Confirmed"),
)

