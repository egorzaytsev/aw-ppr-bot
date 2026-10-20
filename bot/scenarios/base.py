"""Описание вопросов сценария и разбор ответов."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Callable, Optional, Union

Value = Union[str, float, list, None]

# Значение варианта «Да, укажу»: после кнопки бот спрашивает текст, текст и есть ответ
DETAIL = "__detail__"

_URL_RE = re.compile(r"https?://\S+", re.I)


def find_links(text: str) -> list[str]:
    return [u.rstrip(".,;)") for u in _URL_RE.findall(text or "")]

# Вариант кнопки: (подпись для дилера, значение в Payload)
Choice = tuple[str, str]


PAST_MONTH = "Этот месяц уже прошёл — выбери текущий или будущий"


class ParseError(ValueError):
    """Ответ не подходит — текст ошибки уходит пользователю."""


@dataclass(frozen=True)
class Question:
    # Ключ в Payload. Где есть поле в Projects — его имя, чтобы перенос был прямым.
    key: str
    prompt: str
    # text | choice | number | month | file (один файл/ссылка) | files (несколько, до «Готово»)
    kind: str = "text"
    phase: int = 1
    # Шаги одного вопроса (например, материалы по видам): в нумерации — одна позиция
    group: str = ""
    # Для group: в группе нужен хотя бы один файл или ссылка
    group_min_one: bool = False
    # choice: только кнопки. text/number: кнопки-заготовки рядом со свободным ответом.
    choices: tuple[Choice, ...] = ()
    # Можно пропустить кнопкой «Пропустить» (подпись — skip_label)
    optional: bool = False
    skip_label: str = "Пропустить"
    # Обязательность и текст вопроса, зависящие от прошлых ответов
    optional_fn: Optional[Callable[[dict], bool]] = None
    # Задавать ли вопрос при текущих ответах; None — всегда
    ask_if: Optional[Callable[[dict], bool]] = None
    # Ответы, которые сбрасываются, если этот ответ изменился (страна → город)
    resets: tuple[str, ...] = ()
    # Варианты, зависящие от прошлых ответов (например, города страны)
    choices_fn: Optional[Callable[[dict], tuple]] = None
    hint: str = ""
    label: str = ""
    unit: str = ""
    # Уточнение после ответа: отдельные кнопки, значение пишется в qualifier_key
    qualifier_prompt: str = ""
    qualifier_key: str = ""
    qualifiers: tuple[Choice, ...] = ()
    # choice с вариантом DETAIL: текст вопроса-уточнения
    detail_prompt: str = ""
    # После этих вариантов обязателен файл или ссылка → evidence_key
    evidence_for: tuple[str, ...] = ()
    evidence_key: str = ""
    evidence_prompt: str = ""
    # Подпись в сводке, если вопрос пропущен
    missing_label: str = "пропущено"
    # Текст вопроса из ответов (например, «Вы указывали N €»)
    prompt_fn: Optional[Callable[[dict], str]] = None
    # Превращение значения кнопки в итоговое (например, «Подтверждаю» → прежнее число)
    resolve: Optional[Callable[[Value, dict], Value]] = None

    def bind(self, answers: dict) -> "Question":
        """Вопрос с вариантами, обязательностью и текстом под текущие ответы."""
        changes: dict = {}
        if self.choices_fn is not None:
            changes["choices"] = tuple(self.choices_fn(answers))
        if self.optional_fn is not None:
            changes["optional"] = bool(self.optional_fn(answers))
        if self.prompt_fn is not None:
            changes["prompt"] = self.prompt_fn(answers)
        return replace(self, **changes) if changes else self

    def text(self, answers: dict) -> str:
        return self.prompt_fn(answers) if self.prompt_fn else self.prompt

    def applies(self, answers: dict) -> bool:
        return self.ask_if is None or self.ask_if(answers)

    @property
    def title(self) -> str:
        return self.label or self.key

    def label_for(self, value: Value, choices: tuple[Choice, ...] | None = None) -> str | None:
        for lbl, val in (choices if choices is not None else self.choices):
            if val == value:
                return lbl
        return None


@dataclass(frozen=True)
class Gate:
    """Переход в следующую фазу: вопрос с кнопками Да / Нет."""
    prompt: str
    yes: str
    no: str
    # Кнопка в сводке, если от перехода отказались, а потом передумали
    summary_button: str


@dataclass(frozen=True)
class Scenario:
    code: str
    title: str
    questions: tuple[Question, ...]
    phase_titles: dict[int, str]
    # gates[n] — переход из фазы n в n+1
    gates: dict[int, Gate] = field(default_factory=dict)
    # Можно ли предложить переход из фазы n: (n, ответы, незаполненные индексы фазы n) → bool
    gate_open: Callable[[int, dict, list], bool] = lambda n, a, m: False
    # Этап заявки: (достигнутая фаза, ответы, полнота) → (Request Type, подпись этапа)
    stage: Callable[[int, dict, dict], tuple] = lambda p, a, c: ("", "")
    derive: Callable[[dict], dict] = lambda answers: {}
    # Строки вердикта: (ответы, этап, незаполненные названия фазы 1) → HTML
    verdict: Callable[[dict, str, list], list] = lambda a, s, m: []
    project_name_key: Optional[str] = None
    done_text: str = ""
    # С какой фазы начинается диалог (обновление проекта стартует сразу с фазы 2 или 3)
    start_phase: int = 1
    # Без сводки: после последнего вопроса заявка уходит сразу (новость, выигран, проиграл)
    auto_submit: bool = False
    # Одна сквозная нумерация с заголовком сценария («Дополнить · 2/5»), без фаз
    flat_numbering: bool = False
    # Служебные ответы, которые derive уже перевёл в поля Projects, — в Payload не пишем
    payload_skip: tuple[str, ...] = ()


# ---------- разбор ответов ----------

_NUM_RE = re.compile(
    r"^\s*(?:около|примерно|~)?\s*([\d\s.,]+)\s*(к|k|тыс\.?|т|м|m|млн\.?)?\s*(€|eur|евро|м2|м²|кв\.?\s*м)?\s*$",
    re.I,
)


def parse_number(text: str) -> float:
    m = _NUM_RE.match(text.replace(" ", " "))
    if not m:
        raise ParseError("Нужно число, например: 45000 или 45к")
    raw = m.group(1).replace(" ", "")
    # 1,5 / 1.5 — дробь; 45.000 / 45,000 — разделитель тысяч
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", raw):
        raw = re.sub(r"[.,]", "", raw)
    else:
        raw = raw.replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        raise ParseError("Нужно число, например: 45000 или 45к") from None
    mult = (m.group(2) or "").lower().rstrip(".")
    if mult in ("к", "k", "тыс", "т"):
        value *= 1_000
    elif mult in ("м", "m", "млн"):
        value *= 1_000_000
    if value <= 0:
        raise ParseError("Число должно быть больше нуля")
    value = round(value, 2)
    # Целые без «.0» — так чище в Payload
    return int(value) if value.is_integer() else value


_MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}


def parse_month(text: str) -> str:
    """ММ.ГГГГ / ДД.ММ.ГГГГ / «март 2027» / «Q3 2027» → YYYY-MM-DD."""
    t = text.strip().lower()
    m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", t)
    if m:
        d, mo, y = map(int, m.groups())
        return _iso(y, mo, d)
    m = re.fullmatch(r"(\d{1,2})[./-](\d{4})", t)
    if m:
        mo, y = map(int, m.groups())
        return _iso(y, mo, 1)
    m = re.fullmatch(r"(?:q|кв\.?\s*)([1-4])\s*[\s/-]?\s*(\d{4})", t)
    if m:
        q, y = map(int, m.groups())
        return _iso(y, (q - 1) * 3 + 1, 1)
    m = re.fullmatch(r"([а-я]+)\s+(\d{4})", t)
    if m and m.group(1)[:3] in _MONTHS:
        return _iso(int(m.group(2)), _MONTHS[m.group(1)[:3]], 1)
    raise ParseError("Не понял дату. Примеры: 03.2027, март 2027, Q2 2027")


def _iso(y: int, mo: int, d: int) -> str:
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        raise ParseError("Такой даты нет — проверь месяц и день") from None


def match_choice(text: str, choices: tuple[Choice, ...]) -> str | None:
    t = text.strip().lower().rstrip(".")
    for lbl, val in choices:
        if t in (lbl.lower(), val.lower()):
            return val
    hits = [val for lbl, val in choices if t and t in lbl.lower()]
    return hits[0] if len(hits) == 1 else None


def parse_answer(q: Question, text: str) -> Value:
    text = (text or "").strip()
    if not text:
        raise ParseError("Пустой ответ")
    if q.kind == "choice":
        value = match_choice(text, q.choices)
        if value is None:
            # Есть «Да, укажу» — свободный текст и есть уточнение
            if any(v == DETAIL for _, v in q.choices):
                return text
            raise ParseError("Выбери вариант кнопкой ниже")
        return value
    # Текстом набрали подпись кнопки-заготовки — считаем нажатием
    preset = match_choice(text, q.choices) if q.choices else None
    if preset is not None and text.lower() == q.label_for(preset, q.choices).lower():
        return preset
    if q.kind == "number":
        return parse_number(text)
    if q.kind == "month":
        value = parse_month(text)
        if value < date.today().replace(day=1).isoformat():
            raise ParseError(PAST_MONTH)
        return value
    return text


def describe_items(items: list) -> str:
    names = []
    for it in items:
        names.append(it.get("name") or "файл" if it.get("kind") == "file" else it.get("url", ""))
    return "; ".join(names)


def format_value(q: Question, value: Value) -> str:
    if value is None:
        return "—"
    if isinstance(value, list):
        return describe_items(value)
    lbl = q.label_for(value)
    if lbl is not None:
        return lbl
    if q.kind == "number" and isinstance(value, (int, float)):
        s = f"{value:,.0f}".replace(",", " ")
        return f"{s} {q.unit}".strip()
    if q.kind == "month":
        m = re.fullmatch(r"(\d{4})-(\d{2})-\d{2}", str(value))
        # Старые ответы могли сохраниться свободным текстом — показываем как есть
        return f"{m.group(2)}.{m.group(1)}" if m else str(value)
    return str(value)
