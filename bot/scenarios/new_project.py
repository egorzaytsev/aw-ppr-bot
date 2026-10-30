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
MATERIALS_KEY = "Materials"
THIRD_PARTY_KEY = "Third-party Statement"  # все документы третьей стороны одним вопросом
# Месяц размещения заказа на регистрации — заменяет Expected Order Date
ORDER_MONTH_FINAL = "Order Month Final"
# Упрощённая схема (до 30 000 €): «зарегистрировать» = прислать номер квотации бэк-офиса
SIMPLIFIED_QUOTE = "Simplified Quote №"
WANTS_QUOTE = "_simplified_quote"


_REGION_WORDS = ("област", "обл.", "край", "район", "республик", "округ")
_COMMENT_WORDS = ("возможно", "будет", "так как", "уточн", "но ", "пока ", "примерно")
MAX_ADDRESS_LEN = 80


def check_city(text: str) -> str | None:
    """Регион вместо города — переспросить."""
    if any(w in text.lower() for w in _REGION_WORDS):
        return "Это регион. Напиши город — например, Всеволожск."
    return None


def check_address(text: str) -> str | None:
    """Только адрес, без пояснений: длинный текст или слова-оговорки — переспросить."""
    low = f" {text.lower()} "
    if len(text) > MAX_ADDRESS_LEN or any(w in low for w in _COMMENT_WORDS):
        return ("Здесь только адрес — улица, дом, корпус. "
                "Пояснения напиши потом в «Обновить проект» → «Сообщить новость».")
    return None


def materials_prompt(a: dict) -> str:
    """Все материалы — одним экраном; документы бюро — только если AW заложен бюро (или ответа нет)."""
    items = ["визуализации", "планировки", "твоя презентация для клиента"]
    if a.get("AW Origin") not in (ORIGIN_DEALER, ORIGIN_OWN):
        items.append("ведомость или спецификация бюро")
    items += ["твоя спецификация", "квотация Andreu World"]
    return "Пришли материалы по проекту — всё, что есть:\n" + "\n".join(f"• {i}" for i in items)
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
STAGE_SIMPLIFIED = "регистрация по упрощённой схеме (квотация)"


def _month(v) -> str:
    """2027-03-01 → 03.2027"""
    return f"{v[5:7]}.{v[:4]}" if isinstance(v, str) and len(v) >= 7 else str(v)


def _money(v) -> str:
    return f"{v:,.0f} €".replace(",", " ")


# ---------- фаза 1: уведомление ----------

PHASE_1 = (
    Question(
        key="End Client",
        label="Конечный клиент",
        prompt="Кто конечный клиент?",
        hint="Компания, для которой делается объект. Например: Сбербанк, Hyatt Regency",
    ),
    Question(
        key="Object Name",
        label="Объект",
        prompt="Как называется объект?",
        hint="Здание, комплекс или кодовое имя. Например: офис на Лесной, ЖК Символ",
    ),
    Question(
        key="Object Part",
        label="Часть объекта / поставки",
        prompt="Какая часть объекта?",
        hint="Зоны, этажи, блок, очередь. Например: 3–5 этажи, кафе и ресепшн",
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
        prompt="В какой стране?",
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
        prompt="В каком городе?",
        kind="choice",
        choices_fn=lambda a: CITIES.get(a.get("Country"), ()) + ((OTHER_CITY, DETAIL),),
        detail_prompt="Напиши город.",
        validate=check_city,
        ask_if=lambda a: a.get("Object Location") == LOC_OTHER,
    ),
    Question(
        key="Object Address",
        label="Адрес",
        prompt="Адрес объекта?",
        hint="Улица, дом, корпус",
        validate=check_address,
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
        prompt="Объём Andreu World по прайсу, €?",
        hint="Можно примерно: 45к, 120 000",
        kind="number",
        unit="€",
    ),
    Question(
        key="Project Stage",
        label="Стадия",
        prompt="На какой стадии проект?",
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
        prompt="Как называется бюро?",
        hint="Архитектор работает сам, без бюро — нажми «Частный архитектор»",
        choices=((SOLO_ARCHITECT, SOLO),),
        ask_if=lambda a: a.get("Project Stage") != STAGE_ARCHITECT_SELECTION
        and a.get("AW Origin") in (ORIGIN_BUREAU, ORIGIN_DEALER),
    ),
    Question(
        key="Lead Architect",
        label="ГАП",
        prompt="Кто ГАП проекта?",
        hint="Имя главного архитектора",
        # Частный архитектор: имя — единственное, что о нём известно, поэтому обязательно
        prompt_fn=lambda a: "Как зовут архитектора?" if a.get("Specifier") == SOLO else "Кто ГАП проекта?",
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
        prompt="Предлагаешь клиенту альтернативы Andreu World?",
        hint="Другие бренды в той же номенклатуре",
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
        prompt="Кто ещё влияет на решение?",
        hint="Бюро, генподрядчик, технический заказчик, консультанты",
        optional=True,
    ),
    Question(
        phase=2,
        key="Competing Dealers",
        label="Конкурирующие дилеры",
        prompt="Другие дилеры предлагают мебель на этот объект?",
        kind="choice",
        choices=(("Нет", "Нет"), ("Да — укажу, кто", DETAIL)),
        detail_prompt="Какие дилеры?",
    ),
    Question(
        phase=2,
        key="Parallel Registrations",
        label="Заявлен другим производителям",
        prompt="Запрашиваемый объём заявлен тобой у других производителей мебели?",
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
        hint="Числом, можно примерно: 45к, 120 000",
        hint_fn=lambda a: (f"Было указано {_money(a['PL Volume'])}. Не изменился — нажми «Подтверждаю»"
                           if a.get("PL Volume") is not None else "Числом, можно примерно: 45к, 120 000"),
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
        prompt="В каком месяце размещение заказа?",
        kind="month",
    ),
    Question(
        phase=2,
        key=MATERIALS_KEY,
        label="Материалы",
        prompt=materials_prompt({}),
        prompt_fn=materials_prompt,
        hint="Файлы по одному, больше 5 МБ — ссылкой.",
        kind="files",
        optional=True,
        missing_label="не присланы",
    ),
    Question(
        phase=2,
        key="Call Slot 15",
        label="Звонок 15 минут",
        prompt="Когда удобен звонок на 15 минут?",
        kind="slot",
    ),
)

# ---------- фаза 3: регистрация ----------

PHASE_3 = (
    Question(
        phase=3,
        key="Quotation / Proforma №",
        label="Квотация AW №",
        prompt="Номер квотации Andreu World?",
    ),
    Question(
        phase=3,
        key="Decision Maker",
        label="Принимает решение",
        prompt="Кто лично принимает решение по мебели?",
        hint="Имя, компания, роль",
    ),
    Question(
        phase=3,
        key=THIRD_PARTY_KEY,
        label="Документы заказчика и бюро",
        prompt=("Пришли документы заказчика или бюро — всё, что есть:\n• ведомость с Andreu World\n"
                "• тендерное задание\n• запрос КП от заказчика\n• другое"),
        hint="Файлы по одному, больше 5 МБ — ссылкой.",
        kind="files",
        optional=True,
        missing_label="не присланы",
    ),
    # Бывшие вопросы по видам документов — не задаются; оставлены, чтобы не сдвигать индексы вопросов
    *(
        Question(phase=3, key=key, label=label, prompt=label, kind="files", ask_if=lambda a: False)
        for key, label in (("Tender Brief", "Тендерное задание"), ("Purchase Request", "Запрос КП"),
                           ("Other Third-party Document", "Прочее"))
    ),
    Question(
        phase=3,
        key=ORDER_MONTH_FINAL,
        label="Месяц размещения заказа",
        prompt="В каком месяце размещение заказа?",
        hint_fn=lambda a: (f"Раньше был {_month(a['Order Month'])}. Не изменился — выбери его же"
                           if a.get("Order Month") else ""),
        kind="month",
    ),
    Question(
        phase=3,
        key="Call Slot 30",
        label="Звонок 30 минут",
        prompt="Когда удобен звонок на 30 минут?",
        kind="slot",
    ),
)

# Номер квотации упрощённой схемы — вопрос фазы 1, но в конце списка: индексы остальных вопросов
# (и состояние начатых диалогов) не сдвигаются
SIMPLIFIED_EXTRA = (
    Question(
        phase=1,
        key=SIMPLIFIED_QUOTE,
        label="Квотация AW №",
        prompt="Номер квотации?",
        hint="Квотацию запрашивай в бэк-офисе, Егор в копии",
        # Только после «Номер квотации есть» на предложении зарегистрировать
        ask_if=lambda a: bool(a.get(WANTS_QUOTE)) and track(a) == TRACK_SIMPLIFIED,
    ),
)

QUESTIONS = PHASE_1 + PHASE_2 + PHASE_3 + SIMPLIFIED_EXTRA
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
    """Country/City для Payload: с кнопки Москва/Петербург или из ответов «Другое».
    Город в Projects — латиницей (описание поля City): «Всеволожск» → «Vsevolozhsk»."""
    loc = a.get("Object Location")
    if loc in LOCATIONS:
        country, city = LOCATIONS[loc]
        return {"Country": country, "City": city}
    out = {k: a[k] for k in ("Country", "City") if k in a}
    if out.get("City"):
        out["City"] = latin(str(out["City"]))
    return out


def latin(text: str) -> str:
    """Транслит кириллицы, каждое слово с заглавной; латиница остаётся как есть."""
    from ..dedup import _TRANSLIT  # без цикла импортов
    words = "".join(_TRANSLIT.get(ch.lower(), ch) if ch.lower() in _TRANSLIT else ch for ch in text).split()
    return " ".join(w[:1].upper() + w[1:] for w in words)




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


SIMPLIFIED_GATE = Gate(
    prompt="Давай зарегистрируем! Для этого отправь запрос на квотацию и пришли мне номер квотации.",
    yes="Номер квотации есть",
    no="Пока только уведомление",
    summary_button="Прислать номер квотации",
    on_yes={WANTS_QUOTE: True},
)


def gate_for(n: int, a: dict) -> Gate | None:
    return SIMPLIFIED_GATE if n == 1 and track(a) == TRACK_SIMPLIFIED else None


def gate_open(n: int, a: dict, missing_all: list, missing_required: list) -> bool:
    if n == 1 and track(a) == TRACK_SIMPLIFIED:
        return not a.get(SIMPLIFIED_QUOTE)  # номер квотации уже прислал — предлагать нечего
    if track(a) != TRACK_FULL:
        return False
    if n == 1:
        return True  # пропуски фазы 1 дилер дозаполнит сразу после «Да» (engine: prefill)
    return not missing_required


def stage(phase_reached: int, a: dict, missing_phase1: list) -> tuple[str, str]:
    if track(a) == TRACK_SIMPLIFIED and a.get(SIMPLIFIED_QUOTE):
        return RequestType.QUOTE_SIMPLIFIED, STAGE_SIMPLIFIED
    if track(a) != TRACK_FULL or missing_phase1 or phase_reached == 1:
        return RequestType.NEW_NOTIFICATION, STAGE_NOTIFICATION
    if phase_reached == 2:
        return RequestType.NEW_PRE_REGISTRATION, STAGE_PRE_REGISTRATION
    return RequestType.NEW_REGISTRATION, STAGE_REGISTRATION


# Что уходит строками в Project Log при Accept (полей в Projects нет)
LOG_KEYS = (
    "AW Origin", "Decision Maker Company", "Influencers", "Competing Dealers",
    "Order Month", "Call Slot 15", "Decision Maker", ORDER_MONTH_FINAL, "Call Slot 30",
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
    if a.get(SIMPLIFIED_QUOTE):
        out["Quotation / Proforma №"] = a[SIMPLIFIED_QUOTE]
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
    gate_for=gate_for,
    gate_open=gate_open,
    stage=stage,
    derive=derive,
    verdict=verdict,
    project_name_key="Dealer Project Name",
    # Где объект → Country/City; подтверждённый объём → PL Volume
    payload_skip=("Object Location", "PL Volume Confirmed", SIMPLIFIED_QUOTE),
    done_text="Информацию по проекту получили. Ответ дадим в течение 72 часов — придёт сюда.",
)
