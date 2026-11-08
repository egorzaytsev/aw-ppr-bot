"""Движок сценария: принимает ответ, двигает состояние, отдаёт следующий экран.

Без Telegram и Airtable — чистая логика, проверяется тестами.
Файл приходит сюда как dict: {"kind": "file", "file_id", "file_unique_id", "name", "size", "mime"};
ссылка — {"kind": "link", "url"}.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from html import escape

from . import phrases, slots
from .scenarios import SCENARIOS
from .scenarios.base import (
    DETAIL, Gate, ParseError, Question, Scenario, describe_items, find_links, format_value,
    is_vague, match_choice, parse_answer,
)
from .state import (
    AWAIT_ANSWER, AWAIT_CONFIRM, AWAIT_DETAIL, AWAIT_EVIDENCE, AWAIT_GATE, AWAIT_MONTH,
    AWAIT_QUALIFIER, AWAIT_SLOT, DialogState,
)

# callback_data кнопок
CB_SKIP = "skip"
CB_STOP = "stop"       # обязательный вопрос: «Пока не знаю» — пропустить только его («не указано» в сводке)
CB_BACK = "back"       # из уточнения / подтверждения — назад к вопросу
CB_LATER = "later"     # подтверждение контакта: «Пришлю позже»
CB_TO_SUMMARY = "tosum"  # из «Что исправить?» — назад к сводке
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
UNKNOWN_KEY = "_unknown"     # обязательные вопросы, пропущенные кнопкой «Пока не знаю»
LATER = "пришлю позже"       # подтверждение контакта обещано позже — уровень без подтверждения
# Номер квотации: «Номера пока нет» — выйти без потери ответов
NO_QUOTE_KEYS = ("Simplified Quote №",)
NOTHING_NEW = "Новых материалов нет — ничего не отправил. Можно запросить следующий этап или сообщить новость."
PICK_BUTTON = "Выбери вариант кнопкой."


def nothing_new_buttons(project_id: str | None) -> list:
    if not project_id:
        return []
    return [[("⏭ Запросить следующий этап", f"cd:next:{project_id}")], [("Сообщить новость", f"cd:news:{project_id}")]]

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


def _unknown(state: DialogState) -> list:
    return state.answers.get(UNKNOWN_KEY) or []


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
    # Ключ может принадлежать и отключённому старому вопросу (звонок 15 минут перенесён в конец) — не трогаем его
    live = {k for q in _qs(state) if q.applies(state.answers) for k in (q.key, q.qualifier_key, q.evidence_key) if k}
    for q in _qs(state):
        if not q.applies(state.answers):
            for key in (q.key, q.qualifier_key, q.evidence_key):
                if key and key not in live:
                    state.answers.pop(key, None)


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


def _reached(state: DialogState) -> int:
    reached = 1
    for n in range(2, state.phase + 1):
        if missing(state, required_only=True, phase=n):
            break
        reached = n
    return reached


def stage_of(state: DialogState) -> tuple[str, str]:
    """(Request Type, подпись этапа) по последней фазе, где все обязательные ответы есть."""
    return scenario_of(state).stage(_reached(state), state.answers, _titles(state, missing(state, phase=1)))


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
    ask = state.answers.get("_ask")
    if ask:   # «Дополнить»: сразу видно, чего не хватает
        labels = list(dict.fromkeys(q.label for q in sc.questions if q.key in ask and q.label))
        if labels:
            head += "\nНе хватает: " + escape(", ".join(labels)) + "."
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


def question_screen(state: DialogState) -> Screen:
    sc = scenario_of(state)
    q = _current_q(state)
    if sc.flat_numbering:
        units = [u for ph in sorted({x.phase for x in sc.questions}) for u in _units(state, ph)]
    else:
        units = _units(state, q.phase)
    pos = next((n for n, u in enumerate(units, 1) if state.step in u), 1)
    # Сколько экранов осталось, включая этот: шаги группы считаем по одному
    left = sum(len(u) for u in units[pos:]) + 1
    first = pos == 1
    if q.group:
        members = units[pos - 1]
        idx = members.index(state.step)
        left += len(members) - idx - 1
        first = first and idx == 0
    # Без заголовка «Фаза · N/M» — только вопрос (жирным) и пояснение курсивом под ним
    head, _, rest = q.text(state.answers).partition("\n")
    lines = [f"<b>{escape(head)}</b>"] + ([escape(rest)] if rest else [])  # список под вопросом — обычным
    if q.hint:
        lines.append(f"<i>{escape(q.hint)}</i>")
    # «Последний вопрос» — только если после этой фазы не будет предложения перейти дальше
    gate_ahead = q.phase in sc.gates or (sc.gate_for is not None and sc.gate_for(q.phase, state.answers))
    if not first and left in phrases.LAST_STEPS and not state.editing and not state.filling and not gate_ahead:
        lines.append(f"<i>{phrases.LAST_STEPS[left]}</i>")
    choice_buttons = [(lbl, f"{CB_CHOICE}{i}") for i, (lbl, _) in enumerate(q.choices)]
    per_row = 1 if any(len(lbl) > 16 for lbl, _ in q.choices) else 2
    buttons = _rows(choice_buttons, per_row)
    if q.kind == "month":
        lines.append("<i>Выбери год, потом месяц — или напиши: 03.2027</i>")
        this_year = date.today().year
        buttons += _rows([(str(y), f"{CB_YEAR}{y}") for y in range(this_year, this_year + PICKER_YEARS)], 3)
    if q.kind == "slot":
        zone = slots.zone_label(_country(state))
        lines.append(f"<i>Выбери день или напиши текстом удобные дату и время ({escape(zone)})</i>")
        days = slots.next_workdays()
        buttons += _rows([(slots.day_label(d), f"{CB_DAY}{d.isoformat()}") for d in days], 2)
    if q.kind == "files" and isinstance(state.answers.get(q.key), list) and state.answers[q.key]:
        buttons.append([("Готово", CB_DONE)])
    if q.optional:
        buttons.append([(q.skip_label, CB_SKIP)])
    elif sc.auto_submit and not _custom_exit(q):
        buttons.append([("Отмена", CB_CANCEL)])   # новость / ответ без ответа не отправить — только выйти
    elif q.unknown_label:
        buttons.append([(q.exit_label(), CB_STOP)])
    return Screen("\n".join(lines), buttons)


def _files_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} файл"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} файла"
    return f"{n} файлов"


def _files_progress(state: DialogState) -> Screen:
    items = state.answers.get(_current_q(state).key) or []
    return Screen(
        f"{phrases.pick(phrases.GOT_FILE)} Уже {_files_word(len(items))}. Пришли ещё файл или ссылку — "
        "или нажми «Готово».",
        [[("Готово", CB_DONE)]],
    )


def _first_month(year: int) -> int:
    """Прошедшие месяцы не предлагаем: в текущем году — с текущего месяца."""
    today = date.today()
    return today.month if year == today.year else 1


def month_screen(state: DialogState) -> Screen:
    first = _first_month(state.pick_year)
    items = [(m, f"{CB_MONTH}{i}") for i, m in enumerate(MONTHS, 1) if i >= first]
    buttons = _rows(items, 3) + [[("← Другой год", CB_YEAR_BACK)]]
    return Screen(f"{state.pick_year} — выбери месяц:", buttons)


def slot_screen(state: DialogState) -> Screen:
    d = date.fromisoformat(state.pick_date)
    country = _country(state)
    items = [(slots.window_label(d, i, country), f"{CB_WINDOW}{i}") for i in range(len(slots.WINDOWS))]
    zone = slots.zone_label(country)
    return Screen(
        f"{escape(slots.day_label(d))} — выбери время ({escape(zone)}) или напиши текстом:",
        _rows(items, 2) + [[("← Другой день", CB_DAY_BACK)]],
    )


def qualifier_screen(state: DialogState) -> Screen:
    q = _current_q(state)
    items = [(lbl, f"{CB_QUALIFIER}{i}") for i, (lbl, _) in enumerate(q.qualifiers)]
    return Screen(escape(q.qualifier_prompt), _rows(items))


def detail_screen(state: DialogState) -> Screen:
    return Screen(escape(_current_q(state).detail_prompt), [[("← Назад", CB_BACK)]])


def evidence_screen(state: DialogState) -> Screen:
    return Screen(escape(_current_q(state).evidence_prompt),
                  [[("Пришлю позже", CB_LATER)], [("← Назад к выбору", CB_BACK)]])


def _next_phase_count(state: DialogState) -> int:
    """Сколько вопросов (кроме материалов) в следующей фазе при текущих ответах — для текста гейта."""
    nxt = state.phase + 1
    return sum(1 for q in _qs(state) if q.phase == nxt and q.applies(state.answers) and q.kind != "files")


def gate_screen(state: DialogState) -> Screen:
    gate = _gate(state)
    n = _next_phase_count(state)
    word = "вопрос" if n % 10 == 1 and n % 100 != 11 else \
        "вопроса" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "вопросов"
    text = escape(gate.prompt.format(n=f"{n} {word}"))
    gaps = _titles(state, missing(state, phase=state.phase)) \
        if state.phase == PREFILL_PHASE and not gate.on_yes else []
    if gaps:
        text += "\n<i>Сначала дополним: " + escape(", ".join(gaps)) + ".</i>"
    return Screen(text, [[(gate.yes, CB_GATE_YES), (gate.no, CB_GATE_NO)]])


# Пропуски дозаполняются перед предрегистрацией (фаза 1 → 2); дальше необязательное остаётся необязательным
PREFILL_PHASE = 1
WANTS_QUOTE_KEY = "_simplified_quote"


def _gate(state: DialogState) -> Gate:
    sc = scenario_of(state)
    custom = sc.gate_for(state.phase, state.answers) if sc.gate_for else None
    return custom or sc.gates[state.phase]


def _to_next_phase(state: DialogState) -> Result:
    """Дилер согласился на предрегистрацию: пропуски фазы 1 — сначала, обязательными."""
    gate = _gate(state)
    if gate.on_yes:
        # Остаёмся в фазе: ответ «да» открывает её дополнительные вопросы (номер квотации)
        before = set(missing(state, phase=state.phase))
        state.answers.update(gate.on_yes)
        nxt = [i for i in missing(state, phase=state.phase) if i not in before]   # только открывшиеся вопросы
        return _ask(state, nxt[0]) if nxt else _end_of_questions(state)
    gaps = missing(state, phase=state.phase) if state.phase == PREFILL_PHASE else []
    if gaps:
        state.prefill = True
        state.filling = True
        state.editing = False
        return _ask(state, gaps[0])
    return _enter_next_phase(state)


def _can_stop(state: DialogState, q: Question) -> bool:
    """Обязательный вопрос с кнопкой выхода «Пока не знаю»."""
    q = q.bind(state.answers)
    return not q.optional and bool(q.unknown_label) and (not scenario_of(state).auto_submit or _custom_exit(q))


def _custom_exit(q: Question) -> bool:
    """Своя кнопка выхода («Номера пока нет», «Архитектор пока неизвестен»), а не общее «Пока не знаю»."""
    from .scenarios.base import UNKNOWN
    return bool(q.unknown_label) and q.unknown_label != UNKNOWN


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
        return "не указано" if q.key in (answers.get(UNKNOWN_KEY) or []) else q.missing_label
    text = format_value(q, answers[q.key])
    if q.qualifier_key and answers.get(q.qualifier_key):
        qual = q.label_for(answers[q.qualifier_key], q.qualifiers) or answers[q.qualifier_key]
        text += f" ({qual.lower()})"
    if q.evidence_key and answers.get(q.evidence_key):
        ev = answers[q.evidence_key]
        text += f" — подтверждение: {describe_items(ev) if isinstance(ev, list) else 'пришлю позже (без подтверждения)'}"
    if len(text) > SUMMARY_VALUE_LIMIT:
        text = text[:SUMMARY_VALUE_LIMIT - 1] + "…"
    return text


def summary_screen(state: DialogState) -> Screen:
    sc = scenario_of(state)
    qs = sc.questions
    # Имя проекта дилеру не показываем: его присваивает Егор при проверке
    lines = ["<b>Проверь данные</b>"]
    target = state.answers.get("_target")
    if target:
        lines.append(f"Проект: <b>{escape(target['name'])}</b>")
    if sc.flat_numbering:
        # Сквозной список без деления на этапы — как и нумерация вопросов
        lines.append("")
        for n, i in enumerate(applicable(state), 1):
            lines.append(f"{n}. {escape(qs[i].title)}: {escape(_display(qs[i], state.answers))}")
    for phase in range(1, state.phase + 1) if not sc.flat_numbering else ():
        if not applicable(state, phase):
            continue
        lines += ["", f"<b>{escape(sc.phase_titles.get(phase, ''))}</b>"]
        for n, i in enumerate(applicable(state, phase), 1):
            q = qs[i]
            lines.append(f"{n}. {escape(q.title)}: {escape(_display(q, state.answers))}")

    gaps = _titles(state, missing(state))
    if gaps:
        lines += ["", "Не указано: " + escape(", ".join(gaps)) + ".", COMPLETENESS_NOTE]
    _, stage_label = stage_of(state)
    verdict = sc.verdict(state.answers, stage_label, _titles(state, missing(state, phase=1)))
    if verdict:
        # verdict — доверенный текст сценария с HTML-разметкой; подставленные значения — названия вопросов
        lines += [""] + verdict
    # Р14: из-за «Пока не знаю» заявка уходит этапом ниже — сказать, чего не хватает
    reached = _reached(state)
    if sc.gates and state.phase > reached:
        short = _titles(state, missing(state, required_only=True, phase=reached + 1))
        nxt = {1: "предрегистрации", 2: "регистрации"}.get(reached, "следующего этапа")
        if short:
            lines.append(f"Подаётся как {escape(stage_label)}: для {nxt} не хватает — {escape(', '.join(short))}.")

    buttons: Buttons = [[("Отправить", CB_SUBMIT)]]
    if gaps:
        buttons.append([("Дозаполнить", CB_FILL)])
    if _gate_open(state, state.phase):
        buttons.append([(_gate(state).summary_button, CB_NEXT_PHASE)])
    buttons.append([("Исправить", CB_EDIT), ("Отменить", CB_CANCEL)])
    return Screen("\n".join(lines), buttons)


def edit_picker(state: DialogState) -> Screen:
    qs = _qs(state)
    items = [(qs[i].title, f"{CB_EDIT_STEP}{i}") for i in applicable(state)]
    note = ("\n<i>Если исправишь вопрос с файлами — файлы этого вопроса придётся прислать заново.</i>"
            if any(qs[i].kind == "files" and state.answers.get(qs[i].key) for i in applicable(state)) else "")
    return Screen("Что исправить?" + note, _rows(items) + [[("← К сводке", CB_TO_SUMMARY)]])


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


def _advance(state: DialogState, skipped: bool = False) -> Result:
    if state.filling:
        # Дозаполнение: только пропущенные, только вперёд — пропуск не зацикливается
        nxt = [i for i in missing(state) if i > state.step]
        if state.prefill:
            nxt = [i for i in missing(state, phase=state.phase) if i > state.step]
            if not nxt:
                state.prefill = False
                state.filling = False
                return _enter_next_phase(state)
        return _ask(state, nxt[0]) if nxt else _end_of_questions(state)
    if state.editing:
        # Исправленный ответ открыл зависимый вопрос сразу за ним (бюро → ГАП) — задаём его
        nxt = state.step + 1
        qs = _qs(state)
        if (nxt < len(qs) and qs[nxt].ask_if is not None and nxt in applicable(state)
                and not _is_filled(qs[nxt], state.answers)):
            return _ask(state, nxt)
        # После исправления могла открыться обязательная ветка (сменили стадию)
        req = [i for i in missing(state, required_only=True) if _qs(state)[i].key not in _unknown(state)]
        return _ask(state, req[0]) if req else _to_summary(state)
    # Вперёд — только по своей фазе (поздние вопросы прошлых фаз стоят в конце списка); «Дополнить» — сквозной
    pool = applicable(state) if scenario_of(state).flat_numbering else applicable(state, phase=state.phase)
    nxt = [i for i in pool if i > state.step]
    if not nxt:
        return _end_of_questions(state)
    result = _ask(state, nxt[0])
    ack = None if skipped else phrases.ack()   # после «Пропустить» реакцию не ставим
    if ack and result.screens:
        result.screens[0].text = f"{ack}\n{result.screens[0].text}"
    return result


def _accept(state: DialogState, q: Question, value) -> Result:
    """Ответ принят: сохранить, спросить уточнение/подтверждение, иначе дальше."""
    if value is None:
        _clear(state, q)
        return _advance(state, skipped=True)
    if q.key in _unknown(state):
        state.answers[UNKNOWN_KEY] = [k for k in _unknown(state) if k != q.key]
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
        msg = Screen("Файл сейчас не нужен — ответь на вопрос.")
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
        return Result(state, [Screen("Нажми кнопку под сводкой."), summary_screen(state)])
    if state.awaiting == AWAIT_GATE:
        return Result(state, [Screen(PICK_BUTTON), gate_screen(state)])
    q = _current_q(state)
    if _wants_items(state):
        links = find_links(text)
        choice = match_choice(text, q.choices) if q.choices and state.awaiting == AWAIT_ANSWER else None
        if not links and choice is not None:
            return _accept(state, q, choice)
        if not links:
            hint = " Нет под рукой — нажми «Пришлю позже»." if state.awaiting == AWAIT_EVIDENCE else ""
            return Result(state, [Screen(f"⚠️ Нужен файл или ссылка (https://…).{hint}"), current_screen(state)])
        return _add_items(state, q, [{"kind": "link", "url": u} for u in links])
    if state.awaiting == AWAIT_QUALIFIER:
        value = match_choice(text, q.qualifiers)
        if value is None:
            return Result(state, [Screen(PICK_BUTTON), qualifier_screen(state)])
        state.answers[q.qualifier_key] = value
        return _advance(state)
    if state.awaiting in (AWAIT_MONTH, AWAIT_SLOT):
        state.awaiting = AWAIT_ANSWER  # написали текстом — разбираем как обычный ответ
    if state.awaiting == AWAIT_DETAIL:
        if not text.strip():
            return Result(state, [detail_screen(state)])
        problem = q.validate(text.strip()) if q.validate else None
        if not problem and is_vague(q.key, text):
            problem = "Напиши, что известно. Если пока ничего — нажми «← Назад» и выбери другой вариант."
        if problem:
            return Result(state, [Screen(f"⚠️ {escape(problem)}"), detail_screen(state)])
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
            return _to_next_phase(state)
        if data == CB_GATE_NO:
            state.declined_gate = state.phase
            return _to_summary(state)
        return Result(state, [Screen(PICK_BUTTON), gate_screen(state)])

    if state.awaiting in (AWAIT_DETAIL, AWAIT_EVIDENCE):
        q = _current_q(state)
        if data == CB_BACK:
            if state.awaiting == AWAIT_EVIDENCE:
                _clear(state, q)   # выбрать уровень заново
            state.awaiting = AWAIT_ANSWER
            return Result(state, [question_screen(state)])
        if data == CB_LATER and state.awaiting == AWAIT_EVIDENCE:
            state.answers[q.evidence_key] = LATER
            return _advance(state)
        return Result(state, [current_screen(state)])

    if state.awaiting == AWAIT_ANSWER:
        q = _current_q(state)
        if data == CB_SKIP and q.optional:
            # Ответы, подставленные из проекта, не считаются — только то, что спрашивали сейчас
            asked = set(state.answers.get("_ask") or ()) - {q.key}
            if q.kind == "files" and state.answers.get("_files_only") and not state.answers.get(q.key) \
                    and not any(state.answers.get(k) not in (None, "", []) for k in asked):
                # «Новых материалов нет» при пустой анкете — отправлять нечего
                pid = (state.answers.get("_target") or {}).get("id")
                return Result(None, [Screen(NOTHING_NEW, nothing_new_buttons(pid))])
            return _accept(state, q, None)
        if data == CB_STOP and _can_stop(state, q):
            if state.scenario == "project_quote":
                pid = (state.answers.get("_target") or {}).get("id")
                return Result(None, [Screen("Хорошо, номер пришлёшь позже: «Мои проекты» → проект → "
                                            "«🧾 Прислать номер квотации».",
                                            [[("Открыть проект", f"pc:my:{pid}")]] if pid else [])])
            if q.key in NO_QUOTE_KEYS:
                # «Номера пока нет»: ответы сохраняются, заявка уходит без квотации
                state.answers.pop(WANTS_QUOTE_KEY, None)
                _clear(state, q)
                state.declined_gate = state.phase
                return _to_summary(state)
            # Пропускаем только этот вопрос — в сводке «не указано», остальные задаём дальше (Р14)
            unknown = _unknown(state)
            if q.key not in unknown:
                state.answers[UNKNOWN_KEY] = unknown + [q.key]
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
        return Result(state, [Screen(PICK_BUTTON), question_screen(state)])

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
            if idx and idx >= _first_month(state.pick_year):
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

    # AWAIT_CONFIRM
    if data == CB_TO_SUMMARY:
        return _to_summary(state)
    if data == CB_SUBMIT:
        req = [i for i in missing(state, required_only=True) if qs[i].key not in _unknown(state)]
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
        return _to_next_phase(state)
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
    unknown = [qs.title for qs in _qs(state) if qs.key in _unknown(state)]
    if unknown:
        payload["Not Known Yet"] = ", ".join(dict.fromkeys(unknown))   # проверяющему — что дилер пока не знает
    return {k: v for k, v in payload.items() if v is not None}


def collected_files(state: DialogState) -> list[tuple[str, dict]]:
    """Все загруженные файлы (не ссылки): (ключ вопроса, файл) — для вложений заявки."""
    out = []
    for key in build_payload(state):
        value = state.answers.get(key)
        if isinstance(value, list):
            out += [(key, it) for it in value if isinstance(it, dict) and it.get("kind") == "file"]
    return out
