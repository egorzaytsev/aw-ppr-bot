"""Поиск возможных дублей заявки среди проектов: объект, клиент, адрес.

Дилер пишет по-русски, проекты в базе — латиницей, поэтому всё сводится к одному
«звучанию»: транслитерация кириллицы + упрощение английской транскрипции
(Символ → simvol, Symbol → simbol, Petrovitch → petrovich) и нечёткое сравнение.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y",
    "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
# Транскрипция: разные латинские записи одного звучания → одна форма
_SQUASH = (
    ("shch", "sh"), ("sch", "sh"), ("tch", "ch"), ("dzh", "j"), ("zh", "j"), ("dj", "j"),
    ("kh", "h"), ("ph", "f"), ("th", "t"), ("ck", "k"), ("ts", "c"), ("tz", "c"), ("x", "ks"),
    ("w", "v"), ("q", "k"), ("yu", "u"), ("ya", "a"), ("y", "i"), ("ee", "i"), ("oo", "u"),
)
_STOP = {
    "ooo", "pao", "zao", "oao", "ao", "ip", "llc", "ltd", "inc", "group", "grupp", "gruppa", "kompania",
    "the", "and", "ofis", "ofic", "office", "proekt", "project", "obekt", "bc", "jk", "jilo", "ul", "ulica",
    "prospekt", "pr", "dom", "korp", "korpus", "str", "stroenie", "lit", "moskva", "moscow", "gorod",
    "ves", "vsego", "obekta", "etaj", "etaji", "etaja",
}
_MIN_LEN = 4
_SIMILAR = 0.85  # «Тайерс» ≠ «Тауэрс» (0.83), «Петрович» = «Petrovitch»


def sound(text: str) -> str:
    t = "".join(_TRANSLIT.get(ch, ch) for ch in (text or "").lower())
    for a, b in _SQUASH:
        t = t.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", t)  # двойные буквы


def tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", sound(text)) if len(w) >= _MIN_LEN and w not in _STOP]


def _similar(a: str, b: str) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) >= 5 and (a.startswith(b) or b.startswith(a)):
        return True
    return SequenceMatcher(None, a, b).ratio() >= _SIMILAR


def overlap(left: str, right: str) -> bool:
    lt, rt = tokens(left), tokens(right)
    return any(_similar(a, b) for a in lt for b in rt)


def _house_numbers(address: str) -> set[str]:
    return set(re.findall(r"\d+[a-zа-я]?", (address or "").lower()))


def same_address(left: str, right: str) -> bool:
    """Одна улица (по основе слова — Лесная/Лесной) и общий номер дома."""
    if not left or not right or not (_house_numbers(left) & _house_numbers(right)):
        return False
    lt, rt = tokens(left), tokens(right)
    return any(a[:4] == b[:4] for a in lt for b in rt)


@dataclass
class Candidate:
    project_id: str
    name: str
    score: int
    reasons: list[str] = field(default_factory=list)


def find(payload: dict, dealer_project_name: str, projects: list[dict], limit: int = 3) -> list[Candidate]:
    """projects: [{"id", "name", "object", "address", "clients": [имена]}]."""
    obj = " ".join(str(payload.get(k) or "") for k in ("Object Name",))
    client = str(payload.get("End Client") or "")
    address = str(payload.get("Object Address") or "")
    if not obj.strip():
        obj = dealer_project_name or ""
    out: list[Candidate] = []
    for p in projects:
        reasons: list[str] = []
        score = 0
        if address and same_address(address, p.get("address") or ""):
            score += 3
            reasons.append("адрес")
        if obj and overlap(obj, f"{p.get('name') or ''} {p.get('object') or ''}"):
            score += 2
            reasons.append("объект")
        if client and overlap(client, " ".join(p.get("clients") or []) + " " + (p.get("name") or "")):
            score += 2
            reasons.append("клиент")
        if score >= 2:
            out.append(Candidate(p["id"], p.get("name") or "Без названия", score, reasons))
    out.sort(key=lambda c: (-c.score, c.name.lower()))
    return out[:limit]


def analysis_text(candidates: list[Candidate]) -> str:
    if not candidates:
        return "Совпадений с проектами в базе не найдено (проверены объект, клиент, адрес)."
    lines = ["Возможные совпадения:"]
    lines += [f"• {c.name} — совпадает: {', '.join(c.reasons)} (баллы {c.score})" for c in candidates]
    return "\n".join(lines)


_LEGAL = re.compile(r"^(ооо|пао|зао|оао|ао|ип|llc|ltd)\s+", re.IGNORECASE)


MAX_NAME_LEN = 80


def has_cyrillic(text: str) -> bool:
    return bool(re.search(r"[а-яё]", text or "", re.I))


# Частые слова названий — переводим, остальное транслитом («где можно — переводим»)
_WORDS = [
    (r"бизнес[- ]?центр[а-я]*|\bбц\b", "BC"), (r"торгов[а-я]* центр[а-я]*|\bтц\b|\bтрц\b", "TC"),
    (r"жил[а-я]* комплекс[а-я]*|\bжк\b", "RC"), (r"штаб[- ]квартир[а-я]*", "HQ"),
    (r"(\d+)[- ]?(?:й|ий|ый)?\s*этаж[а-я]*", r"FL\1"), (r"этаж[а-я]*\s*(\d+)", r"FL\1"),
    (r"гостиниц(?:а|ы|е|у|ей)|отел(?:ь|я|е|ю|ем)", "Hotel"), (r"офис(?:а|е|у|ом|ы|ов)?", "Office"), (r"ресторан(?:а|е|у|ом|ы|ов)?", "Restaurant"),
    (r"банк(?:а|е|у|ом|и|ов)?", "Bank"), (r"школ(?:а|ы|е|у|ой|ам)", "School"), (r"университет[а-я]*", "University"),
    (r"музе(?:й|я|е|ю|ем|и|ев)", "Museum"), (r"больниц(?:а|ы|е|у|ей)", "Hospital"), (r"клиник(?:а|и|е|у|ой)", "Clinic"),
    (r"аэропорт[а-я]*", "Airport"), (r"библиотек(?:а|и|е|у|ой)", "Library"), (r"театр(?:а|е|у|ом|ы|ов)?", "Theatre"),
    (r"башн(?:я|и|е|ю|ей)", "Tower"), (r"апартамент[а-я]*", "Apartments"), (r"кампус[а-я]*", "Campus"),
    (r"кафе", "Cafe"), (r"штаб", "HQ"), (r"центр(?:а|е|у|ом|ы|ов)?", "Center"), (r"здани[ея]", "Building"),
]
_WORDS_RE = [(re.compile(rf"(?<![а-яё]){pat}(?![а-яё])", re.I), repl) for pat, repl in _WORDS]


def translate_words(text: str) -> str:
    for rx, repl in _WORDS_RE:
        text = rx.sub(repl, text)
    return re.sub(r"\s*[·•|—–]\s*|\s+-\s+", " ", text)


def to_latin(text: str) -> str:
    """Название латиницей: частые слова переводим, остальное транслитом."""
    text = translate_words(text or "")
    out = "".join(_TRANSLIT.get(ch.lower(), ch) if ch.lower() in _TRANSLIT else ch for ch in text or "")
    out = " ".join(w[:1].upper() + w[1:] for w in out.split())
    return out[:MAX_NAME_LEN].strip()


_DASHES = re.compile(r"\s*[—–]\s*|\s+-\s+|^-+|-+$")


def name_problem(name: str) -> str | None:
    """Названия проектов — только латиницей, через пробелы без тире, не длиннее 80 знаков."""
    if has_cyrillic(name):
        return "название только латиницей: где можно — переводим, где нельзя — транслитерация"
    if _DASHES.search(name):
        return "название без тире, по образцу «PVSMART InsaitBC FL3»"
    if len(name) > MAX_NAME_LEN:
        return f"не длиннее {MAX_NAME_LEN} знаков (сейчас {len(name)})"
    return None


# Техническая информация — короткими кодами (образец Егора: «PVSMART InsaitBC FL3»)
_CODES = [
    (r"бизнес[- ]?центр[а-я]*|\bбц\b", "BC"), (r"торгов[а-я]*[- ]?(?:развлекательн[а-я]*\s*)?центр[а-я]*|\bтр?ц\b", "TC"),
    (r"жил[а-я]* комплекс[а-я]*|\bжк\b", "RC"), (r"штаб[- ]квартир[а-я]*", "HQ"),
    (r"этаж[а-я]*", "FL"),   # без номера: «VIP этаж» → VIPFL
]
_PARTS = [
    (r"(\d+)[- ]?(?:й|ий|ый)?\s*этаж[а-я]*|этаж[а-я]*\s*(\d+)", "FL"),
    (r"корпус[а-я]*\s*(\d+)|(\d+)[- ]?(?:й|ий|ый)?\s*корпус[а-я]*", "K"),
    (r"блок[а-я]*\s*(\w+)", "BL"),
]
_CODES_RE = [(re.compile(rf"(?<![а-яё]){pat}(?![а-яё])", re.I), code) for pat, code in _CODES]
_PARTS_RE = [(re.compile(rf"(?<![а-яё]){pat}(?![а-яё])", re.I), code) for pat, code in _PARTS]


def _translit(text: str) -> str:
    return "".join(_TRANSLIT.get(ch.lower(), ch) if ch.lower() in _TRANSLIT else ch for ch in text)


def _compact(raw: str, parts: list[str]) -> str:
    """Объект слитно, тип кодом в конце: «Бизнес центр Инсайт» → «InsaitBC»; этаж/корпус — в parts."""
    for rx, code in _PARTS_RE:
        for m in rx.finditer(raw):
            num = next((g for g in m.groups() if g), "")
            tag = f"{code}{_translit(num).upper()}"
            if tag not in parts:
                parts.append(tag)
        raw = rx.sub(" ", raw)
    codes = []
    for rx, code in _CODES_RE:
        if rx.search(raw):
            codes.append(code)
            raw = rx.sub(" ", raw)
    words = re.findall(r"[0-9a-z]+", _translit(translate_words(raw)), re.I)
    return "".join(w if w.isupper() and len(w) > 1 else w[:1].upper() + w[1:] for w in words) + "".join(codes)


def latin_name(payload: dict) -> str:
    """Черновик названия по образцу «PVSMART InsaitBC FL3»: КЛИЕНТ слитно заглавными, объект слитно
    с кодом типа, этаж / корпус / блок — коротко. Минимум пробелов, без ООО, кавычек и тире."""
    clean = lambda key: _LEGAL.sub("", re.sub(r"[«»\"'“”]", "", str(payload.get(key) or "")).strip())
    parts: list[str] = []
    client = "".join(re.findall(r"[0-9a-z]+", _translit(clean("End Client")), re.I)).upper()
    obj = _compact(" ".join(x for x in (clean("Object Name"), clean("Object Part")) if x), parts)
    return " ".join(x for x in (client, obj, *parts) if x)[:MAX_NAME_LEN].strip()


def _company_key(name: str) -> str:
    key = " ".join(tokens(name))
    return key or sound(name).strip()


_LEGAL_WORDS = re.compile(r"(?<![a-zа-яё])(ооо|пао|зао|оао|ао|ип|llc|ltd|inc|gmbh|s\.?l\.?|s\.?a\.?|plc)(?![a-zа-яё])\.?",
                          re.IGNORECASE)


def _full_key(name: str) -> str:
    """Название целиком (все слова, и короткие — «PV»), без ООО/LLC, кавычек и регистра."""
    return " ".join(re.findall(r"[a-z0-9]+", sound(_LEGAL_WORDS.sub(" ", name or ""))))


def match_company(name: str, companies: dict) -> str | None:
    """id компании с тем же названием: ООО/LLC, регистр, транслит и опечатка не мешают.

    «PV Smart» ≠ «Smart Group»: похожие, но не те же — это company_candidates, решает проверяющий."""
    key = _full_key(name)
    if not key:
        return None
    best, best_ratio = None, 0.0
    for cid, cname in companies.items():
        other = _full_key(cname or "")
        if not other:
            continue
        if other == key:
            return cid
        ratio = SequenceMatcher(None, key, other).ratio()
        if ratio > best_ratio:
            best, best_ratio = cid, ratio
    return best if best_ratio >= 0.92 else None


def company_candidates(name: str, companies: dict, limit: int = 3) -> list[tuple[str, str]]:
    """Похожие компании (общее значимое слово), когда точного совпадения нет: [(id, название)]."""
    if not _full_key(name) or match_company(name, companies):
        return []
    key = _company_key(name)
    out = [(cid, cname) for cid, cname in companies.items()
           if cname and (_company_key(cname) == key or overlap(name, cname))]
    return out[:limit]
