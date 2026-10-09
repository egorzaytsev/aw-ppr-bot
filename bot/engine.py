"""Движок сценария: принимает ответ, двигает состояние, отдаёт следующий экран.

Без Telegram и Airtable — чистая логика, проверяется тестами.
Файл приходит сюда как dict: {"kind": "file", "file_id", "file_unique_id", "name", "size", "mime"};
ссылка — {"kind": "link", "url"}.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from html import escape

from . import slots
from .scenarios import SCENARIOS
from .scenarios.base import (
    DETAIL, ParseError, Question, Scenario, describe_items, find_links, format_value,
    match_choice, parse_answer,
)
from .state import (
    AWAIT_ANSWER, AWAIT_CONFIRM, AWAIT_DETAIL, AWAIT_EVIDENCE, AWAIT_GATE, AWAIT_MONTH,
    AWAIT_QUALIFIER, AWAIT_SLOT, DialogState,
)

# callback_data кнопок
CB_SKIP = "skip"
CB_CHOICE = "ch:"      # + индекс варианта в Question.choices
CB_QUALIFIER = "qf:"   # + индекс варианта в Question.qualifiers
CB_DONE = "done"       # конец загрузки файлов
CB_GATE_YES = "gate:y"
CB_GATE_NO = "gate:n"
CB_NEXT_PHASE = "next"  # из сводки: всё-таки перейти в следующую фазу
CB_SUBMIT = "submit"
CB_FILL = "fill"
CB_EDIT = "edit"
CB_EDIT_STEP = "ed:"   # + номер шага
CB_CANCEL = "cancel"
CB_YEAR = "y:"         # + год (пикер месяца)
CB_MONTH = "m:"        # + номер месяца
CB_YEAR_BACK = "y:back"
CB_DAY = "d:"          # + YYYY-MM-DD (пикер слота)
CB_WINDOW = "w:"       # + индекс окна
CB_DAY_BACK = "d:back"

MONTHS = ("Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек")
PICKER_YEARS = 3  # текущий и два следующих

COMPLETENESS_NOTE = "Чем полнее данные, тем выше приоритет при совпадении с другим партнёром."
SUMMARY_VALUE_LIMIT = 300  # символов на ответ в сводке — укладываемся в 4096 Telegram

Buttons = list[list[tuple[str, str]]]


@dataclass
class Screen:
    text: str
    # Ряды кнопок: [(подпись, callback_data), ...]
    buttons: Buttons = field(default_factory=list)


@dataclass
class Result:
    state: DialogState | None
    screens: list[Screen]
    # Сводка подтверждена — вызывающий код создаёт заявку
    submit: bool = False


def scenario_of(state: DialogState) -> Scenario:
    return SCENARIOS[state.scenario]


def _qs(state: DialogState) -> tuple[Question, ...]:
    return scenario_of(state).questions


def _current_q(state: DialogState) -> Question:
    return _qs(state)[state.step].bind(state.answers)


def _country(state: DialogState) -> str | None:
    """Страна объекта с учётом кнопок «Москва / Петербург» (Country берётся из derive)."""
    return scenario_of(state).derive(state.answers).get("Country") or state.answers.get("Country")


def _rows(items: list[tuple[str, str]], per_row: int = 2) -> Buttons:
    return [items[i:i + per_row] for i in range(0, len(items), per_row)]


# ---------- какие вопросы задаются и что не заполнено ----------

def applicable(state: DialogState, phase: int | None = None) -> list[int]:
    """Индексы вопросов достигнутых фаз (или одной фазы), которые задаются при текущих ответах."""
    return [
        i for i, q in enumerate(_qs(state))
        if q.phase <= state.phase and (phase is None or q.phase == phase) and q.applies(state.answers)
    ]


def _is_filled(q: Question, answers: dict) -> bool:
    if q.key not in answers:
        return False
    if q.qualifier_key and q.qualifier_key not in answers:
        return False
    if answers[q.key] in q.evidence_for and not answers.get(q.evidence_key):
        return False
    return True


def missing(state: DialogState, required_only: bool = False, phase: int | None = None) -> list[int]:
    qs = _qs(state)
    return [
        i for i in applicable(state, phase)
        if not _is_filled(qs[i], state.answers) and not (required_only and qs[i].bind(state.answers).optional)
    ]


def _titles(state: DialogState, idxs: list[int]) -> list[str]:
    return [_qs(state)[i].title for i in idxs]


def _purge_inapplicable(state: DialogState) -> None:
    """Ответ изменил ветку (например, стадия) — убираем ответы на ставшие ненужными вопросы."""
    for q in _qs(state):
        if not q.applies(state.answers):
            _clear(state, q)


def _clear(state: DialogState, q: Question) -> None:
    for key in (q.key, q.qualifier_key, q.evidence_key):
        if key:
            state.answers.pop(key, None)


def _gate_open(state: DialogState, n: int) -> bool:
    sc = scenario_of(state)
    if n not in sc.gates or state.phase != n:
        return False
    return sc.gate_open(
        n, state.answers,
        _titles(state, missing(state, phase=n)),
        _titles(state, missing(state, required_only=True, phase=n)),
    )


def stage_of(state: DialogState) -> tuple[str, str]:
    """(Request Type, подпись этапа) по достигнутой фазе и полноте."""
    return scenario_of(state).stage(state.phase, state.answers, _titles(state, missing(state, phase=1)))


# ---------- экраны ----------

def start(scenario_code: str, preset: dict | None = None) -> Result:
    """Новый диалог. preset — служебные ответы (например, _target — проект для обновления)."""
    sc = SCENARIOS[scenario_code]
    state = DialogState(scenario=scenario_code, phase=sc.start_phase, answers=dict(preset or {}))
    first = applicable(state)
    state.step = first[0] if first else 0
    target = state.answers.get("_target")
    head = f"<b>{escape(sc.title)}</b>"
    if target:
        head += f" · {escape(target['name'])}"
    intro = Screen(f"{head}\nМожно отвечать текстом или голосом. Прервать — /cancel.")
    return Result(state, [intro, question_screen(state)])


def _units(state: DialogState, phase: int) -> list[list[int]]:
    """Позиции нумерации фазы: вопросы одной группы — одна позиция."""
    qs = _qs(state)
    units: list[list[int]] = []
    for i in applicable(state, phase):
        if units and qs[i].group and qs[units[-1][0]].group == qs[i].group:
            units[-1].append(i)
        else:
            units.append([i])
    return units


def _group_members(state: DialogState, q: Question) -> list[int]:
    for unit in _units(state, q.phase):
        if _qs(state)[unit[0]].group == q.group:
            return unit
    return []


def question_screen(state: DialogState) -> Screen:
    sc = scenario_of(state)
    q = _current_q(state)
    units = _units(state, q.phase)
    pos = next((n for n, u in enumerate(units, 1) if state.step in u), 1)
    head = f"{sc.phase_titles.get(q.phase, '')} · {pos}/{len(units)}"
    if q.group:
        members = units[pos - 1]
        head += f" · {q.group}: {q.title} ({members.index(state.step) + 1}/{len(members)})"
    lines = [f"<b>{escape(head)}.</b> {escape(q.text(state.answers))}"]
    if q.hint:
        lines.append(f"<i>{escape(q.hint)}</i>")
    choice_buttons = [(lbl, f"{CB_CHOICE}{i}") for i, (lbl, _) in enumerate(q.choices)]
    per_row = 1 if any(len(lbl) > 16 for lbl, _ in q.choices) else 2
    buttons = _rows(choice_buttons, per_row)
    if q.kind == "month":
        lines.append("<i>Выберите год, потом месяц — или напишите: 03.2027</i>")
        this_year = date.today().year
        buttons += _rows([(str(y), f"{CB_YEAR}{y}") for y in range(this_year, this_year + PICKER_YEARS)], 3)
    if q.kind == "slot":
        zone = slots.zone_name(_country(state))
        lines.append(f"<i>Выберите день или напишите текстом удобные дату и время ({escape(zone)})</i>")
        days = slots.next_workdays()
        buttons += _rows([(slots.day_label(d), f"{CB_DAY}{d.isoformat()}") for d in days], 2)
    if q.kind == "files" and isinstance(state.answers.get(q.key), list) and state.answers[q.key]:
        buttons.append([("Готово", CB_DONE)])
    if q.optional:
        buttons.append([(q.skip_label, CB_SKIP)])
    return Screen("\n".join(lines), buttons)


def _files_progress(state: DialogState) -> Screen:
    items = state.answers.get(_current_q(state).key) or []
    return Screen(
        f"Принято: {len(items)}. Пришлите ещё файл или ссылку — или нажмите «Готово».",
        [[("Готово", CB_DONE)]],
    )


def month_screen(state: DialogState) -> Screen:
    items = [(m, f"{CB_MONTH}{i}") for i, m in enumerate(MONTHS, 1)]
    buttons = _rows(items, 3) + [[("← Другой год", CB_YEAR_BACK)]]
    return Screen(f"{state.pick_year} — выберите месяц:", buttons)


def slot_screen(state: DialogState) -> Screen:
    d = date.fromisoformat(state.pick_date)
    country = _country(state)
    items = [(slots.window_label(d, i, country), f"{CB_WINDOW}{i}") for i in range(len(slots.WINDOWS))]
    zone = slots.zone_name(country)
    return Screen(
        f"{escape(slots.day_label(d))} — выберите время ({escape(zone)}) или напишите текстом:",
        _rows(items, 2),
    )


def qualifier_screen(state: DialogState) -> Screen:
    q = _current_q(state)
    items = [(lbl, f"{CB_QUALIFIER}{i}") for i, (lbl, _) in enumerate(q.qualifiers)]
    return Screen(escape(q.qualifier_prompt), _rows(items))


def detail_screen(state: DialogState) -> Screen:
    return Screen(escape(_current_q(state).detail_prompt))


def evidence_screen(state: DialogState) -> Screen:
    return Screen(escape(_current_q(state).evidence_prompt))


def gate_screen(state: DialogState) -> Screen:
    gate = scenario_of(state).gates[state.phase]
    return Screen(escape(gate.prompt), [[(gate.yes, CB_GATE_YES), (gate.no, CB_GATE_NO)]])


def current_screen(state: DialogState) -> Screen:
    healed = heal(state)
    if healed:
        return healed.screens[-1]
    return {
        AWAIT_CONFIRM: summary_screen,
        AWAIT_QUALIFIER: qualifier_screen,
        AWAIT_DETAIL: detail_screen,
        AWAIT_EVIDENCE: evidence_screen,
        AWAIT_GATE: gate_screen,
        AWAIT_MONTH: month_screen,
        AWAIT_SLOT: slot_screen,
    }.get(state.awaiting, question_screen)(state)


def _display(q: Question, answers: dict) -> str:
    q = q.bind(answers)
    if q.key not in answers:
        return q.missing_label
    text = format_value(q, answers[q.key])
    if q.qualifier_key and answers.get(q.qualifier_key):
        qual = q.label_for(answers[q.qualifier_key], q.qualifiers) or answers[q.qualifier_key]
        text += f" ({qual.lower()})"
    if q.evidence_key and answers.get(q.evidence_key):
        text += f" — подтверждение: {describe_items(answers[q.evidence_key])}"
    if len(text) > SUMMARY_VALUE_LIMIT:
        text = text[:SUMMARY_VALUE_LIMIT - 1] + "…"
    return text


def summary_screen(state: DialogState) -> Screen:
    sc = scenario_of(state)
    qs = sc.questions
    # Имя проекта дилеру не показываем: его присваивает Егор при проверке
    lines = ["<b>Проверьте данные</b>"]
    target = state.answers.get("_target")
    if target:
        lines.append(f"Проект: <b>{escape(target['name'])}</b>")
    for phase in range(1, state.phase + 1):
        if not applicable(state, phase):
            continue
        lines += ["", f"<b>{escape(sc.phase_titles.get(phase, ''))}</b>"]
        for n, i in enumerate(applicable(state, phase), 1):
            q = qs[i]
            lines.append(f"{n}. {escape(q.title)}: {escape(_display(q, state.answers))}")

    gaps = _titles(state, missing(state))
    if gaps:
        lines += ["", "Пропущено: " + escape(", ".join(gaps)) + ".", COMPLETENESS_NOTE]
    _, stage_label = stage_of(state)
    verdict = sc.verdict(state.answers, stage_label, _titles(state, missing(state, phase=1)))
    if verdict:
        # verdict — доверенный текст сценария с HTML-разметкой; подставленные значения — названия вопросов
        lines += [""] + verdict

    buttons: Buttons = [[("Отправить", CB_SUBMIT)]]
    if gaps:
        buttons.append([("Дозаполнить", CB_FILL)])
    if _gate_open(state, state.phase):
        buttons.append([(sc.gates[state.phase].summary_button, CB_NEXT_PHASE)])
    buttons.append([("Исправить", CB_EDIT), ("Отменить", CB_CANCEL)])
    return Screen("\n".join(lines), buttons)


def edit_picker(state: DialogState) -> Screen:
    qs = _qs(state)
    items = [(qs[i].title, f"{CB_EDIT_STEP}{i}") for i in applicable(state)]
    return Screen("Что исправить?", _rows(items))


# ---------- переходы ----------

def _ask(state: DialogState, step: int) -> Result:
    state.step = step
    state.awaiting = AWAIT_ANSWER
    q = _qs(state)[step]
    if q.kind == "files" and state.editing:
        # Исправление материалов — собираем заново
        state.answers.pop(q.key, None)
    return Result(state, [question_screen(state)])


def _to_summary(state: DialogState) -> Result:
    state.editing = False
    state.filling = False
    state.awaiting = AWAIT_CONFIRM
    return Result(state, [summary_screen(state)])


def _end_of_questions(state: DialogState) -> Result:
    """Вопросы текущей фазы закончились: предложить следующую фазу или показать сводку."""
    if scenario_of(state).auto_submit and not missing(state, required_only=True):
        return Result(state, [], submit=True)
    if state.declined_gate != state.phase and _gate_open(state, state.phase):
        state.editing = False
        state.filling = False
        state.awaiting = AWAIT_GATE
        return Result(state, [gate_screen(state)])
    return _to_summary(state)


def _enter_next_phase(state: DialogState) -> Result:
    state.phase += 1
    state.declined_gate = 0
    nxt = missing(state, phase=state.phase)
    return _ask(state, nxt[0]) if nxt else _end_of_questions(state)


def _group_empty(state: DialogState, q: Question) -> list[int] | None:
    """Последний шаг группы пройден, а файлов/ссылок нет — вернуть шаги группы."""
    if not q.group_min_one:
        return None
    members = _group_members(state, q)
    if not members or state.step != members[-1]:
        return None
    qs = _qs(state)
    if any(isinstance(state.answers.get(qs[i].key), list) for i in members):
        return None
    return members


def _advance(state: DialogState) -> Result:
    q = _current_q(state)
    empty = _group_empty(state, q)
    if empty:
        note = Screen(f"⚠️ {escape(q.group)}: нужен хотя бы один файл или ссылка.")
        return Result(state, [note] + _ask(state, empty[0]).screens)
    if state.filling:
        # Дозаполнение: только пропущенные, только вперёд — пропуск не зацикливается
        nxt = [i for i in missing(state) if i > state.step]
        return _ask(state, nxt[0]) if nxt else _end_of_questions(state)
    if state.editing:
        # Исправленный ответ открыл зависимый вопрос сразу за ним (бюро → ГАП) — задаём его
        nxt = state.step + 1
        qs = _qs(state)
        if (nxt < len(qs) and qs[nxt].ask_if is not None and nxt in applicable(state)
                and not _is_filled(qs[nxt], state.answers)):
            return _ask(state, nxt)
        # После исправления могла открыться обязательная ветка (сменили стадию)
        req = missing(state, required_only=True)
        return _ask(state, req[0]) if req else _to_summary(state)
    nxt = [i for i in applicable(state) if i > state.step]
    return _ask(state, nxt[0]) if nxt else _end_of_questions(state)


def _accept(state: DialogState, q: Question, value) -> Result:
    """Ответ принят: сохранить, спросить уточнение/подтверждение, иначе дальше."""
    if value is None:
        _clear(state, q)
        return _advance(state)
    if value == DETAIL:
        state.awaiting = AWAIT_DETAIL
        return Result(state, [detail_screen(state)])
    if q.resolve:
        value = q.resolve(value, state.answers)
    if state.answers.get(q.key) != value:
        for key in q.resets:
            state.answers.pop(key, None)
    state.answers[q.key] = value
    _purge_inapplicable(state)
    if q.qualifiers and not (q.kind in ("file", "files") and not isinstance(value, list)):
        state.awaiting = AWAIT_QUALIFIER
        return Result(state, [qualifier_screen(state)])
    if q.qualifier_key and q.kind in ("file", "files") and not isinstance(value, list):
        state.answers.pop(q.qualifier_key, None)
    if q.evidence_key:
        if value in q.evidence_for:
            state.awaiting = AWAIT_EVIDENCE
            return Result(state, [evidence_screen(state)])
        state.answers.pop(q.evidence_key, None)
    return _advance(state)


def _add_items(state: DialogState, q: Question, items: list[dict]) -> Result:
    """Файлы/ссылки на вопрос kind=file|files или на подтверждение."""
    if state.awaiting == AWAIT_EVIDENCE:
        state.answers[q.evidence_key] = items
        return _advance(state)
    if q.kind == "file":
        state.answers[q.key] = items
        return _advance(state)
    if not isinstance(state.answers.get(q.key), list):
        state.answers[q.key] = []
    state.answers[q.key].extend(items)
    return Result(state, [_files_progress(state)])


def _wants_items(state: DialogState) -> bool:
    if state.awaiting == AWAIT_EVIDENCE:
        return True
    return state.awaiting == AWAIT_ANSWER and _current_q(state).kind in ("file", "files")


def handle_file(state: DialogState, item: dict) -> Result:
    if not _wants_items(state):
        msg = Screen("Файл сейчас не нужен — ответьте на вопрос.")
        return Result(state, [msg, current_screen(state)])
    return _add_items(state, _current_q(state), [item])


def _stale(state: DialogState) -> bool:
    """Сценарий поменяли, пока диалог открыт: ждём уточнение, которого у вопроса больше нет."""
    if state.awaiting not in (AWAIT_QUALIFIER, AWAIT_DETAIL, AWAIT_EVIDENCE, AWAIT_MONTH, AWAIT_SLOT):
        return False
    if state.step >= len(_qs(state)):
        return True
    q = _current_q(state)
    return {
        AWAIT_QUALIFIER: not q.qualifiers,
        AWAIT_DETAIL: not q.detail_prompt,
        AWAIT_EVIDENCE: not q.evidence_key,
        AWAIT_MONTH: q.kind != "month",
        AWAIT_SLOT: q.kind != "slot",
    }[state.awaiting]


def heal(state: DialogState) -> Result | None:
    """Вернуть устаревший диалог на рельсы: ответ уже есть — дальше, нет — повторить вопрос."""
    if not _stale(state):
        return None
    state.step = min(state.step, len(_qs(state)) - 1)
    state.awaiting = AWAIT_ANSWER
    if _current_q(state).key in state.answers:
        return _advance(state)
    return Result(state, [question_screen(state)])


def handle_text(state: DialogState, text: str) -> Result:
    healed = heal(state)
    if healed:
        return healed
    if state.awaiting == AWAIT_CONFIRM:
        return Result(state, [Screen("Нажмите кнопку под сводкой."), summary_screen(state)])
    if state.awaiting == AWAIT_GATE:
        return Result(state, [gate_screen(state)])
    q = _current_q(state)
    if _wants_items(state):
        links = find_links(text)
        choice = match_choice(text, q.choices) if q.choices and state.awaiting == AWAIT_ANSWER else None
        if not links and choice is not None:
            return _accept(state, q, choice)
        if not links:
            return Result(state, [Screen("⚠️ Нужен файл или ссылка (https://…)."), current_screen(state)])
        return _add_items(state, q, [{"kind": "link", "url": u} for u in links])
    if state.awaiting == AWAIT_QUALIFIER:
        value = match_choice(text, q.qualifiers)
        if value is None:
            return Result(state, [qualifier_screen(state)])
        state.answers[q.qualifier_key] = value
        return _advance(state)
    if state.awaiting in (AWAIT_MONTH, AWAIT_SLOT):
        state.awaiting = AWAIT_ANSWER  # написали текстом — разбираем как обычный ответ
    if state.awaiting == AWAIT_DETAIL:
        if not text.strip():
            return Result(state, [detail_screen(state)])
        state.answers[q.key] = text.strip()
        return _advance(state)
    try:
        value = parse_answer(q, text)
    except ParseError as err:
        return Result(state, [Screen(f"⚠️ {escape(str(err))}"), question_screen(state)])
    return _accept(state, q, value)


def _index(data: str, prefix: str, size: int) -> int | None:
    try:
        idx = int(data[len(prefix):])
    except ValueError:
        return None
    return idx if 0 <= idx < size else None


def handle_callback(state: DialogState, data: str) -> Result:
    healed = heal(state)
    if healed:
        return healed
    qs = _qs(state)

    if data == CB_CANCEL:
        return Result(None, [Screen("Отменено. Ничего не отправлено.")])

    if state.awaiting == AWAIT_GATE:
        if data == CB_GATE_YES:
            return _enter_next_phase(state)
        if data == CB_GATE_NO:
            state.declined_gate = state.phase
            return _to_summary(state)
        return Result(state, [gate_screen(state)])

    if state.awaiting == AWAIT_ANSWER:
        q = _current_q(state)
        if data == CB_SKIP and q.optional:
            return _accept(state, q, None)
        if data == CB_DONE and q.kind == "files":
            if isinstance(state.answers.get(q.key), list) and state.answers[q.key]:
                if q.qualifiers:
                    state.awaiting = AWAIT_QUALIFIER
                    return Result(state, [qualifier_screen(state)])
                return _advance(state)
            return Result(state, [Screen("⚠️ Нужен минимум один файл или ссылка."), question_screen(state)])
        if data.startswith(CB_DAY) and q.kind == "slot" and data != CB_DAY_BACK:
            try:
                date.fromisoformat(data[len(CB_DAY):])
            except ValueError:
                return Result(state, [question_screen(state)])
            state.pick_date = data[len(CB_DAY):]
            state.awaiting = AWAIT_SLOT
            return Result(state, [slot_screen(state)])
        if data.startswith(CB_YEAR) and q.kind == "month" and data != CB_YEAR_BACK:
            try:
                state.pick_year = int(data[len(CB_YEAR):])
            except ValueError:
                return Result(state, [question_screen(state)])
            state.awaiting = AWAIT_MONTH
            return Result(state, [month_screen(state)])
        if data.startswith(CB_CHOICE):
            idx = _index(data, CB_CHOICE, len(q.choices))
            if idx is not None:
                return _accept(state, q, q.choices[idx][1])
        # Кнопка от старого сообщения — повторяем текущий вопрос
        return Result(state, [question_screen(state)])

    if state.awaiting == AWAIT_QUALIFIER:
        q = _current_q(state)
        if data.startswith(CB_QUALIFIER):
            idx = _index(data, CB_QUALIFIER, len(q.qualifiers))
            if idx is not None:
                state.answers[q.qualifier_key] = q.qualifiers[idx][1]
                return _advance(state)
        return Result(state, [qualifier_screen(state)])

    if state.awaiting == AWAIT_MONTH:
        q = _current_q(state)
        if data == CB_YEAR_BACK:
            state.awaiting = AWAIT_ANSWER
            return Result(state, [question_screen(state)])
        if data.startswith(CB_MONTH):
            idx = _index(data, CB_MONTH, 13)
            if idx:
                state.awaiting = AWAIT_ANSWER
                return _accept(state, q, date(state.pick_year, idx, 1).isoformat())
        return Result(state, [month_screen(state)])

    if state.awaiting == AWAIT_SLOT:
        q = _current_q(state)
        if data == CB_DAY_BACK:
            state.awaiting = AWAIT_ANSWER
            return Result(state, [question_screen(state)])
        if data.startswith(CB_WINDOW):
            idx = _index(data, CB_WINDOW, len(slots.WINDOWS))
            if idx is not None:
                state.awaiting = AWAIT_ANSWER
                d = date.fromisoformat(state.pick_date)
                return _accept(state, q, slots.slot_value(d, idx, _country(state)))
        return Result(state, [slot_screen(state)])

    if state.awaiting in (AWAIT_DETAIL, AWAIT_EVIDENCE):
        return Result(state, [current_screen(state)])

    # AWAIT_CONFIRM
    if data == CB_SUBMIT:
        req = missing(state, required_only=True)
        if req:
            state.editing = True
            first = _ask(state, req[0])
            note = Screen("Не хватает обязательного: " + escape(", ".join(_titles(state, req))))
            return Result(state, [note] + first.screens)
        return Result(state, [], submit=True)
    if data == CB_FILL:
        gaps = missing(state)
        if gaps:
            state.filling = True
            return _ask(state, gaps[0])
        return _to_summary(state)
    if data == CB_NEXT_PHASE and _gate_open(state, state.phase):
        return _enter_next_phase(state)
    if data == CB_EDIT:
        return Result(state, [edit_picker(state)])
    if data.startswith(CB_EDIT_STEP):
        idx = _index(data, CB_EDIT_STEP, len(qs))
        if idx is not None and idx in applicable(state):
            state.editing = True
            return _ask(state, idx)
    return Result(state, [summary_screen(state)])


def build_payload(state: DialogState) -> dict:
    """Payload для Registrations: ответы в порядке вопросов + производные поля."""
    payload: dict = {}
    for i in applicable(state):
        q = _qs(state)[i]
        for key in (q.key, q.qualifier_key, q.evidence_key):
            if key and key in state.answers:
                payload[key] = state.answers[key]
    sc = scenario_of(state)
    payload.update(sc.derive(state.answers))
    for key in sc.payload_skip:
        payload.pop(key, None)
    return {k: v for k, v in payload.items() if v is not None}


def collected_files(state: DialogState) -> list[tuple[str, dict]]:
    """Все загруженные файлы (не ссылки): (ключ вопроса, файл) — для вложений заявки."""
    out = []
    for key in build_payload(state):
        value = state.answers.get(key)
        if isinstance(value, list):
            out += [(key, it) for it in value if isinstance(it, dict) and it.get("kind") == "file"]
    return out
