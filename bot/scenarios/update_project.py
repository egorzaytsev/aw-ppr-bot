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
    ORIGIN_BUREAU, ORIGIN_OWN, OWN_DESIGN, QUESTIONS, SOLO, STAGE_ARCHITECT_SELECTION, current_pl, exw, location,
    project_log,
    specifier,
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
            prompt="Что нового по проекту? Напиши или запиши голосовое.",
            hint="Стадия, решения клиента, сроки, конкуренты — всё, что изменилось",
        ),
    ),
    phase_titles={1: "Новость"},
    stage=lambda p, a, m: (RequestType.UPDATE_NEWS, "новость по проекту"),
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
            prompt="Напиши ответ или запиши голосовое.",
        ),
        Question(
            key="Extra Materials",
            label="Файлы",
            prompt="Есть файлы к ответу? Пришли файлы или ссылку — или нажми «Нет».",
            kind="files",
            choices=(("Нет", "нет"),),
        ),
    ),
    phase_titles={1: "Ответ"},
    stage=lambda p, a, m: (RequestType.UPDATE_NEWS, "ответ на запрос"),
    derive=_reply_derive,
    verdict=lambda a, s, m: _plain_verdict(s),
    project_name_key="Dealer Project Name",
    done_text="Принято, ответ передан Егору.",
    auto_submit=True,
)


# ---------- Запрос квотации (упрощённая схема, до 30 000 €) ----------

def _quote_derive(a: dict) -> dict:
    out: dict = {"Dealer Project Name": _target_name(a)}
    line = "Запрос квотации по упрощённой схеме — спецификация во вложениях"
    if a.get("Quote Comment"):
        line += f". Комментарий дилера: {a['Quote Comment']}"
    out["Project Log"] = line
    return out


QUOTE = Scenario(
    code="project_quote",
    title="Запросить квотацию",
    questions=(
        Question(
            key="Extra Materials",
            label="Спецификация",
            prompt=("Пришли спецификацию для квотации — файлом или ссылкой. "
                    "Файлы по одному, больше 5 МБ — ссылкой. Когда закончишь — «Готово»."),
            kind="files",
        ),
        Question(
            key="Quote Comment",
            label="Комментарий",
            prompt="Комментарий к запросу: сроки, количество, особые условия.",
            optional=True,
        ),
    ),
    phase_titles={1: "Квотация"},
    stage=lambda p, a, m: (RequestType.QUOTE_SIMPLIFIED, "запрос квотации"),
    derive=_quote_derive,
    verdict=lambda a, s, m: _plain_verdict(s),
    project_name_key="Dealer Project Name",
    done_text="Спецификацию получил — передал Егору на квотацию.",
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
            detail_prompt="Напиши причину.",
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


# «Бюро ещё не выбрано» — только в «Дополнить»: формула пропусков требует бюро и на выборе архитектора
NO_BUREAU_YET = "__no_bureau__"
# Ответы предрегистрации без полей в Projects — живут в Project Log строкой «Заголовок: …»
LOG_GAP_KEYS = ("Decision Maker Company", "Influencers", "Competing Dealers")


def _ask_only(q: Question) -> Question:
    """Вопрос сценария «Новый проект», который задаётся, только если поле пустое в проекте."""
    if q.key == "AW Origin":
        return replace(q, ask_if=lambda a: "AW Origin" in a.get("_ask", ()))
    if q.key == "Specifier":
        return replace(q, choices=q.choices + (("Бюро ещё не выбрано", NO_BUREAU_YET),),
                       ask_if=lambda a: "Specifier" in a.get("_ask", ()) and a.get("AW Origin") != ORIGIN_OWN)
    if q.key == "Lead Architect":
        # ГАП — только если AW заложил архитектор (или ответа о бюро в этот раз не было); частному — всегда
        return replace(q, ask_if=lambda a: "Lead Architect" in a.get("_ask", ())
                       and a.get("Specifier") not in (None, OWN_DESIGN, NO_BUREAU_YET)
                       and (a.get("AW Origin") in (None, ORIGIN_BUREAU) or a.get("Specifier") == SOLO))

    def ask_if(a: dict, q: Question = q) -> bool:
        return q.key in a.get("_ask", ()) and q.applies(a)
    return replace(q, ask_if=ask_if)


FILL_QUESTIONS = tuple(_ask_only(q) for q in QUESTIONS) + (
    Question(
        key=EXTRA_FILES_KEY,
        label="Материалы",
        prompt=(
            "Все данные по проекту на месте. Пришли новые материалы — визуализации, планировки, "
            "документы бюро. Файлы по одному, больше 5 МБ — ссылкой. Когда закончишь — «Готово»."
        ),
        kind="files",
        ask_if=lambda a: bool(a.get("_files_only")),
    ),
)
_FILL_TITLES = {q.key: q.title for q in QUESTIONS}


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
    # AW в проекте бюро — поля в Projects нет, ответ живёт в Project Log
    if stage != STAGE_ARCHITECT_SELECTION and f"{_FILL_TITLES['AW Origin']}:" not in (f.get(Projects.PROJECT_LOG) or ""):
        need("AW Origin")
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
        "Specifier": ("Specifier", "Lead Architect"),
        "Contact Level": ("Contact Level",),
        "AW Alternatives": ("AW Alternatives",),
        "Parallel Registrations": ("Parallel Registrations",),
        "Dossier Files": MATERIAL_KEYS,
    }
    for name, qkeys in by_name.items():
        if name in missing_names:
            for k in qkeys:
                need(k)
    # Ответы предрегистрации из Project Log (Simplified до предрегистрации не доходит)
    if f.get(Projects.PPR_TRACK) != "Simplified (<30k)":
        log = f.get(Projects.PROJECT_LOG) or ""
        for k in LOG_GAP_KEYS:
            if f"{_FILL_TITLES[k]}:" not in log:
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
    if a.get("Specifier") == NO_BUREAU_YET:
        out["Specifier"] = None
    pl = current_pl(a)
    if pl is not None:
        out["PL Volume"] = pl
        out["EXW Potential"] = exw(pl)
    log = project_log(a)
    if a.get("Specifier") == NO_BUREAU_YET:
        log.append("Архбюро: ещё не выбрано")
    if log:
        out["Project Log"] = "\n".join(log)
    return out


FILL = Scenario(
    code="project_fill",
    title="Дополнить",
    questions=FILL_QUESTIONS,
    flat_numbering=True,
    phase_titles={1: "Уведомление", 2: "Предрегистрация", 3: "Регистрация"},
    stage=lambda p, a, m: (RequestType.UPDATE_FIELDS, "дополнение по проекту"),
    derive=_fill_derive,
    verdict=lambda a, s, m: _plain_verdict(s),
    project_name_key="Dealer Project Name",
    done_text=FILL_DONE,
    start_phase=3,
    payload_skip=("Object Location", "PL Volume Confirmed"),
)

