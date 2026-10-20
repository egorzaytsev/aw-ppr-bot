"""Сценарий «Новый проект»: уведомление → предрегистрация → регистрация.

Сценарий Егора от 09.10.2026 (ред. 3).
Этап дилер не выбирает: бот определяет по полноте и объёму.
Объём ниже 30 000 € — трек Simplified, только уведомление.

Ключи Payload = имена полей в Projects там, где поле есть, — при Accept переносятся
напрямую. Остальное (площадь, вопросы 10–13, 17, 19, 21, 23, 24) бот сразу собирает
в строки Project Log. Файлы (15, 18, 22) — во вложениях заявки.
"""
from __future__ import annotations

from ..airtable_ids import RequestType
from .base import DETAIL, Gate, Question, Scenario, format_value

FULL_TRACK_THRESHOLD = 30_000  # € по прайсу
TRACK_FULL = "Full"
TRACK_SIMPLIFIED = "Simplified (<30k)"
OWN_DESIGN = "Собственная проработка"
SOLO_ARCHITECT = "Частный архитектор"
SOLO = "__solo__"
# AW в проекте бюро — значения пишутся в Project Log как есть
ORIGIN_BUREAU = "Да, заложен бюро"
ORIGIN_DEALER = "Нет — AW предлагает дилер взамен продукта бюро"
ORIGIN_OWN = "Бюро нет — проект ведёт дилер"
CONTACT_BUREAU_DOCS = "Indirect (bureau docs)"
CONTACT_CHOICES = (
    ("Пока ничем — только моё слово", "Declared"),
    ("Документы от бюро", CONTACT_BUREAU_DOCS),
    ("Переписка или прямой контакт с клиентом", "Verifiable (email/contact)"),
    ("Готов к встрече с клиентом вместе с AW", "Joint meeting with AW"),
)
WHOLE_OBJECT = "Весь объект"
STAGE_ARCHITECT_SELECTION = "Architect selection"
LOC_MOSCOW, LOC_SPB, LOC_OTHER = "moscow", "spb", "other"
# Кнопка → (Country, City) в Projects; City латиницей, как в Companies
LOCATIONS = {LOC_MOSCOW: ("Russia", "Moscow"), LOC_SPB: ("Russia", "Saint-Petersburg")}
# Стандартная проектная скидка: EXW = PL × 0,45 (как EXW −55% в Line Protocol). Дилеру не показываем.
EXW_FACTOR = 0.45
OTHER_CITY = "Другой город"
# Кнопки городов по стране: (подпись, City латиницей как в Companies). Прочие — «Другой город» текстом.
CITIES = {
    "Russia": (("Краснодар", "Krasnodar"), ("Екатеринбург", "Ekaterinburg"), ("Красноярск", "Krasnoyarsk"),
               ("Казань", "Kazan"), ("Сочи", "Sochi")),
    "Kazakhstan": (("Алматы", "Almaty"), ("Астана", "Astana")),
    "Uzbekistan": (("Ташкент", "Tashkent"), ("Самарканд", "Samarkand")),
    "Georgia": (("Тбилиси", "Tbilisi"), ("Батуми", "Batumi")),
    "Armenia": (("Ереван", "Yerevan"),),
    "Kyrgyzstan": (("Бишкек", "Bishkek"),),
    "Belarus": (("Минск", "Minsk"),),
}
CONFIRM = "__confirm__"
NONE = "нет"  # «Нет» на вопросах-материалах

STAGE_NOTIFICATION = "уведомление"
STAGE_PRE_REGISTRATION = "предрегистрация"
STAGE_REGISTRATION = "регистрация"


def _money(v) -> str:
    return f"{v:,.0f} €".replace(",", " ")


# ---------- фаза 1: уведомление ----------

PHASE_1 = (
    Question(
        key="End Client",
        label="Конечный клиент",
        prompt="Конечный клиент — компания, для которой делается объект.",
        hint="Например: Сбербанк, Hyatt Regency",
    ),
    Question(
        key="Object Name",
        label="Объект",
        prompt="Название объекта — здание, комплекс или кодовое имя.",
        hint="Например: офис на Лесной, ЖК Символ",
    ),
    Question(
        key="Object Part",
        label="Часть объекта / поставки",
        prompt="Какая часть объекта или поставки: зоны, этажи, блок, очередь?",
        hint="Например: 3–5 этажи, кафе и ресепшн; 1-я очередь",
        choices=((WHOLE_OBJECT, WHOLE_OBJECT),),
        optional=True,
    ),
    Question(
        key="Object Location",
        label="Где объект",
        prompt="Где объект?",
        kind="choice",
        choices=(("Москва", LOC_MOSCOW), ("Санкт-Петербург", LOC_SPB), ("Другое", LOC_OTHER)),
    ),
    Question(
        key="Country",
        label="Страна",
        prompt="Страна объекта:",
        kind="choice",
        # Значения = варианты поля Country в Projects
        choices=(
            ("Россия", "Russia"),
            ("Казахстан", "Kazakhstan"),
            ("Узбекистан", "Uzbekistan"),
            ("Грузия", "Georgia"),
            ("Армения", "Armenia"),
            ("Киргизия", "Kyrgyzstan"),
            ("Беларусь", "Belarus"),
            ("Другая", "Other"),
        ),
        resets=("City",),
        ask_if=lambda a: a.get("Object Location") == LOC_OTHER,
    ),
    Question(
        key="City",
        label="Город",
        prompt="Город объекта:",
        kind="choice",
        choices_fn=lambda a: CITIES.get(a.get("Country"), ()) + ((OTHER_CITY, DETAIL),),
        detail_prompt="Напиши город.",
        ask_if=lambda a: a.get("Object Location") == LOC_OTHER,
    ),
    Question(
        key="Object Address",
        label="Адрес",
        prompt="Адрес объекта — улица, дом, корпус.",
        optional=True,
        missing_label="не указан",
    ),
    Question(
        key="Area m2",
        label="Площадь",
        prompt="Площадь объекта, м²?",
        hint="Числом, можно примерно",
        kind="number",
        unit="м²",
        optional=True,
        qualifier_prompt="Откуда цифра площади?",
        qualifier_key="Area Source",
        qualifiers=(("Со слов", "со слов"), ("Из документа", "из документа")),
    ),
    Question(
        key="PL Volume",
        label="Объём AW по прайсу",
        prompt="Предполагаемый объём Andreu World по прайсу, €?",
        hint="Можно примерно: 45к, 120 000",
        kind="number",
        unit="€",
    ),
    Question(
        key="Project Stage",
        label="Стадия",
        prompt="Стадия проекта:",
        kind="choice",
        # Значения = варианты поля Project Stage в Projects
        choices=(
            ("Выбор архитектора", STAGE_ARCHITECT_SELECTION),
            ("Концепция", "Concept"),
            ("Проектирование", "Design"),
            ("Тендер", "Tender"),
            ("Закупка", "Procurement"),
        ),
    ),
    Question(
        key="AW Origin",
        label="AW в проекте бюро",
        prompt="AW заложен в проект бюро?",
        kind="choice",
        # Откуда ждать подтверждение: документы бюро или предложение самого дилера
        choices=(
            ("Да, заложен бюро", ORIGIN_BUREAU),
            ("Нет — AW предлагаем мы взамен", ORIGIN_DEALER),
            ("Бюро нет — проект ведём мы", ORIGIN_OWN),
        ),
        # На выборе архитектора бюро ещё нет — не спрашиваем
        ask_if=lambda a: a.get("Project Stage") != STAGE_ARCHITECT_SELECTION,
    ),
    Question(
        key="Specifier",
        label="Архбюро",
        prompt="Архитектурное бюро — название.",
        hint="Если архитектор работает сам, без бюро — «Частный архитектор».",
        choices=((SOLO_ARCHITECT, SOLO),),
        ask_if=lambda a: a.get("Project Stage") != STAGE_ARCHITECT_SELECTION
        and a.get("AW Origin") in (ORIGIN_BUREAU, ORIGIN_DEALER),
    ),
    Question(
        key="Lead Architect",
        label="ГАП",
        prompt="ГАП — имя главного архитектора проекта.",
        # Частный архитектор: имя — единственное, что о нём известно, поэтому обязательно
        prompt_fn=lambda a: "Имя архитектора." if a.get("Specifier") == SOLO
        else "ГАП — имя главного архитектора проекта.",
        optional_fn=lambda a: a.get("Specifier") != SOLO,
        skip_label="Пока не знаю",
        missing_label="пока не известен",
        # ГАП нужен, только если AW заложил сам архитектор; частному архитектору — всегда (это его имя)
        ask_if=lambda a: a.get("Project Stage") != STAGE_ARCHITECT_SELECTION
        and a.get("Specifier") not in (None, OWN_DESIGN)
        and (a.get("AW Origin") == ORIGIN_BUREAU or a.get("Specifier") == SOLO),
    ),
    Question(
        key="AW Alternatives",
        label="Альтернативы AW",
        prompt="Предлагаешь ли клиенту альтернативы Andreu World в той же номенклатуре?",
        kind="choice",
        # Значения = варианты поля AW Alternatives в Projects
        choices=(
            ("Нет, только AW", "No — AW only"),
            ("Да, по тендеру", "Yes — tender requirement"),
            ("Да, по своей инициативе", "Yes — own initiative"),
        ),
    ),
)

# ---------- фаза 2: предрегистрация ----------

PHASE_2 = (
    Question(
        phase=2,
        key="Decision Maker Company",
        label="Решение по мебели принимает",
        prompt="Какая компания принимает решение по мебели?",
    ),
    Question(
        phase=2,
        key="Influencers",
        label="Влияют на решение",
        prompt="Компании, влияющие на решение: бюро, генподрядчик, технический заказчик, консультанты.",
        optional=True,
    ),
    Question(
        phase=2,
        key="Competing Dealers",
        label="Конкурирующие дилеры",
        prompt="Предлагают ли мебель на этот объект другие дилеры?",
        kind="choice",
        choices=(("Нет", "Нет"), ("Да — укажу, кто", DETAIL)),
        detail_prompt="Какие дилеры?",
    ),
    Question(
        phase=2,
        key="Parallel Registrations",
        label="Заявлен другим производителям",
        prompt="Этот проект уже заявлен у других производителей мебели?",
        kind="choice",
        choices=(("Нет", "Нет"), ("Да — укажу, у кого", DETAIL)),
        detail_prompt="У каких производителей?",
    ),
    Question(
        phase=2,
        key="Contact Level",
        label="Уровень контакта",
        prompt="Чем подтверждён твой контакт с клиентом?",
        kind="choice",
        # Значения = варианты поля Contact Level в Projects
        choices=CONTACT_CHOICES,
        # AW не заложен бюро (или бюро нет) — документы бюро его не подтверждают
        choices_fn=lambda a: tuple(c for c in CONTACT_CHOICES if c[1] != CONTACT_BUREAU_DOCS
                                   or a.get("AW Origin") not in (ORIGIN_DEALER, ORIGIN_OWN)),
        evidence_for=("Indirect (bureau docs)", "Verifiable (email/contact)", "Joint meeting with AW"),
        evidence_key="Contact Evidence",
        evidence_prompt="Пришли подтверждение — файл или ссылку.",
    ),
    Question(
        phase=2,
        key="PL Volume Confirmed",
        label="Актуальный объём",
        prompt="Актуальный объём Andreu World по прайсу, €?",
        prompt_fn=lambda a: (
            f"Было указано {_money(a['PL Volume'])} по прайсу. Актуальный объём?"
            if a.get("PL Volume") is not None else "Актуальный объём Andreu World по прайсу, €?"
        ),
        hint="Числом, можно примерно: 45к, 120 000",
        kind="number",
        unit="€",
        # «Подтверждаю» — только если объём уже называли в этом диалоге
        choices_fn=lambda a: (("Подтверждаю", CONFIRM),) if a.get("PL Volume") is not None else (),
        resolve=lambda v, a: a.get("PL Volume") if v == CONFIRM else v,
    ),
    Question(
        phase=2,
        key="Order Month",
        label="Месяц размещения заказа",
        prompt="Месяц размещения заказа:",
        kind="month",
    ),
    *(
        Question(
            phase=2,
            group="Материалы",
            group_min_one=True,
            key=key,
            label=label,
            prompt=prompt + " Пришли файлы или ссылку — или нажми «Нет».",
            hint=hint,
            kind="files",
            choices=(("Нет", NONE),),
            **extra,
        )
        for key, label, prompt, hint, extra in (
            ("Visualizations", "Визуализации", "Визуализации проекта.",
             "Материалы спрошу по видам, по очереди. Файлы — по одному, больше 5 МБ — ссылкой.", {}),
            ("Layouts", "Планировки", "Планировки.", "", {}),
            ("Partner Presentation", "Презентация партнёра",
             "Твоя презентация для клиента — серии и мебель, которые ты предлагаешь.", "", {}),
            ("Bureau Documents", "Документы бюро", "Документы бюро: ведомость, спецификация бюро.", "", {}),
            ("Dealer Specification", "Твоя спецификация", "Твоя спецификация.",
             "Подтверждением плейсмента она не считается.", {}),
            ("AW Quotation File", "Квотация AW", "Квотация Andreu World, если уже есть.", "", {}),
        )
    ),
    Question(
        phase=2,
        key="Call Slot 15",
        label="Звонок 15 минут",
        prompt="Звонок на 15 минут — выбери день:",
        kind="slot",
    ),
)

# ---------- фаза 3: регистрация ----------

PHASE_3 = (
    Question(
        phase=3,
        key="Quotation / Proforma №",
        label="Квотация AW №",
        prompt="Номер квотации Andreu World.",
    ),
    Question(
        phase=3,
        key="Decision Maker",
        label="Принимает решение",
        prompt="Человек, принимающий решение по мебели: имя, компания, роль.",
    ),
    *(
        Question(
            phase=3,
            group="Документы третьей стороны",
            group_min_one=True,
            key=key,
            label=label,
            prompt=prompt + " Пришли файлы или ссылку — или нажми «Нет».",
            hint=hint,
            kind="files",
            choices=(("Нет", NONE),),
        )
        for key, label, prompt, hint in (
            ("Third-party Statement", "Ведомость", "Ведомость с Andreu World внутри.",
             "Документы третьей стороны с Andreu World спрошу по видам, по очереди. "
             "Файлы по одному, больше 5 МБ — ссылкой."),
            ("Tender Brief", "Тендерное задание", "Тендерное задание.", ""),
            ("Purchase Request", "Запрос на закупку", "Запрос на закупку.", ""),
            ("Other Third-party Document", "Прочее", "Другие документы третьей стороны.", ""),
        )
    ),
    Question(
        phase=3,
        key="Shipment Month",
        label="Месяц отгрузки",
        prompt="Требуемый месяц отгрузки:",
        kind="month",
    ),
    Question(
        phase=3,
        key="Call Slot 30",
        label="Звонок 30 минут",
        prompt="Звонок на 30 минут — выбери день:",
        kind="slot",
    ),
)

QUESTIONS = PHASE_1 + PHASE_2 + PHASE_3
_BY_KEY = {q.key: q for q in QUESTIONS}

GATES = {
    1: Gate(
        prompt="Хочешь подать на предрегистрацию? Это ещё девять вопросов и материалы по проекту.",
        yes="Да",
        no="Пока только уведомление",
        summary_button="Подать на предрегистрацию",
    ),
    2: Gate(
        prompt="Есть квотация Andreu World по этому объёму? Тогда можно подать на регистрацию.",
        yes="Да, есть квотация",
        no="Нет",
        summary_button="Подать на регистрацию",
    ),
}


# ---------- правила ----------

def current_pl(a: dict):
    """Актуальный объём по прайсу: подтверждённый в фазе 2, иначе из уведомления."""
    confirmed = a.get("PL Volume Confirmed")
    return confirmed if confirmed is not None else a.get("PL Volume")


def exw(pl) -> int:
    return round(pl * EXW_FACTOR)


def location(a: dict) -> dict:
    """Country/City для Payload: с кнопки Москва/Петербург или из ответов «Другое»."""
    loc = a.get("Object Location")
    if loc in LOCATIONS:
        country, city = LOCATIONS[loc]
        return {"Country": country, "City": city}
    return {k: a[k] for k in ("Country", "City") if k in a}


def specifier(a: dict) -> dict:
    """Частный архитектор: в Specifier — его имя, с пометкой (в Companies заводится компания его имени).

    Проект ведёт сам дилер — Specifier = «Собственная проработка» (в проект ставится компания дилера).
    """
    if a.get("AW Origin") == ORIGIN_OWN:
        return {"Specifier": OWN_DESIGN}
    if a.get("AW Origin") == ORIGIN_DEALER:
        # Specifier — тот, кто заложил AW: здесь сам дилер; бюро — только строкой в Project Log
        return {"Specifier": OWN_DESIGN, "Specifier Type": None, "Bureau": bureau_name(a)}
    if a.get("Specifier") != SOLO:
        return {}
    # None — убрать служебное «__solo__», пока имя ещё не названо
    return {"Specifier Type": SOLO_ARCHITECT, "Specifier": a.get("Lead Architect")}


def project_name(a: dict) -> str:
    """Черновое имя «Клиент — Объект — Уточнение» в словах дилера.

    Только для Registrations → Dealer Project Name (исходник для поиска дублей).
    Официальное имя проекта (латиницей) ставит Егор при проверке — бот его не утверждает.
    """
    parts = [a.get("End Client"), a.get("Object Name")]
    part = a.get("Object Part")
    if part and part != WHOLE_OBJECT:
        parts.append(part)
    return " — ".join(str(p).strip() for p in parts if p)


def track(a: dict) -> str | None:
    # Трек — по объёму из уведомления: упавший позже объём трек не меняет
    pl = a.get("PL Volume")
    if pl is None:
        return None
    # Порог — по прайсу (описание поля PPR Track)
    return TRACK_FULL if pl >= FULL_TRACK_THRESHOLD else TRACK_SIMPLIFIED


def gate_open(n: int, a: dict, missing_all: list, missing_required: list) -> bool:
    if track(a) != TRACK_FULL:
        return False
    if n == 1:
        return not missing_all  # предрегистрация — только при полностью заполненной фазе 1
    return not missing_required


def stage(phase_reached: int, a: dict, missing_phase1: list) -> tuple[str, str]:
    if track(a) != TRACK_FULL or missing_phase1 or phase_reached == 1:
        return RequestType.NEW_NOTIFICATION, STAGE_NOTIFICATION
    if phase_reached == 2:
        return RequestType.NEW_PRE_REGISTRATION, STAGE_PRE_REGISTRATION
    return RequestType.NEW_REGISTRATION, STAGE_REGISTRATION


# Что уходит строками в Project Log при Accept (полей в Projects нет)
LOG_KEYS = (
    "AW Origin", "Decision Maker Company", "Influencers", "Competing Dealers",
    "Order Month", "Call Slot 15", "Decision Maker", "Shipment Month", "Call Slot 30",
)


def bureau_name(a: dict) -> str | None:
    """Бюро, которое AW не закладывало (дилер предлагает взамен) — для Project Log."""
    if a.get("AW Origin") != ORIGIN_DEALER:
        return None
    name = a.get("Lead Architect") if a.get("Specifier") == SOLO else a.get("Specifier")
    if not name or name == SOLO:
        return None
    return f"{SOLO_ARCHITECT} {name}" if a.get("Specifier") == SOLO else str(name)


def project_log(a: dict) -> list[str]:
    lines = []
    bureau = bureau_name(a)
    if bureau:
        lines.append(f"Архбюро (AW не закладывало): {bureau}")
    if a.get("Area m2") is not None:
        area = format_value(_BY_KEY["Area m2"], a["Area m2"])
        src = a.get("Area Source")
        lines.append(f"Площадь: {area}" + (f" ({src})" if src else ""))
    pl = current_pl(a)
    if pl is not None:
        lines.append(f"Объём по прайсу: {_money(pl)} → EXW {_money(exw(pl))} (−55%)")
    confirmed = a.get("PL Volume Confirmed")
    if confirmed is not None and a.get("PL Volume") is not None and confirmed != a["PL Volume"]:
        lines.append(f"Объём по прайсу при уведомлении: {_money(a['PL Volume'])}")
    for key in LOG_KEYS:
        if a.get(key):
            q = _BY_KEY[key]
            lines.append(f"{q.title}: {format_value(q, a[key])}")
    return lines


def derive(a: dict) -> dict:
    out: dict = {"Dealer Project Name": project_name(a)}
    t = track(a)
    if t:
        out["PPR Track"] = t
    pl = current_pl(a)
    if pl is not None:
        out["PL Volume"] = pl
        out["EXW Potential"] = exw(pl)
    out.update(location(a))
    out.update(specifier(a))
    log = project_log(a)
    if log:
        out["Project Log"] = "\n".join(log)
    return out


def verdict(a: dict, stage_label: str, missing_phase1: list) -> list[str]:
    """Без утверждений: что подаётся и чего не хватает. Решение — за Егором после проверки."""
    lines = [f"Подаётся как: <b>{stage_label}</b>. Решение — после проверки."]
    if missing_phase1 and track(a) == TRACK_FULL:
        lines.append(
            "Для предрегистрации не хватает: " + ", ".join(missing_phase1) + ". "
            "Дозаполни сейчас или позже через «Обновить проект»."
        )
    return lines


SCENARIO = Scenario(
    code="new_project",
    title="Новый проект",
    questions=QUESTIONS,
    phase_titles={1: "Уведомление", 2: "Предрегистрация", 3: "Регистрация"},
    gates=GATES,
    gate_open=gate_open,
    stage=stage,
    derive=derive,
    verdict=verdict,
    project_name_key="Dealer Project Name",
    # Где объект → Country/City; подтверждённый объём → PL Volume
    payload_skip=("Object Location", "PL Volume Confirmed"),
    done_text="Информацию по проекту получили. Ответ дадим в течение 72 часов — придёт сюда.",
)
