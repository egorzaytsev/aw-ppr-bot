"""Кто разбирает заявку.

Компания типа Dealer → Лола (аккаунт @aw_eeu); Partner, прочие и пустой тип → Егор.
Пока Лола не написала боту (её Chat ID неизвестен), всё идёт Егору.
Проверяющий, который не принимал решение, получает короткий итог без кнопок.
"""
from __future__ import annotations

from html import escape

from .airtable_ids import Decision

DEALER_TYPE = "Dealer"
LOLA_USERNAME = "aw_eeu"
EGOR, LOLA = "egor", "lola"
NAMES = {EGOR: "Егор", LOLA: "Лола"}
_FEMININE = {LOLA}


def route(company_types: list) -> str:
    return LOLA if DEALER_TYPE in company_types else EGOR


def chats(bot_data: dict) -> dict:
    """Проверяющий → Chat ID (только известные)."""
    out = {}
    if bot_data.get("admin_chat_id"):
        out[EGOR] = str(bot_data["admin_chat_id"])
    if bot_data.get("lola_chat_id"):
        out[LOLA] = str(bot_data["lola_chat_id"])
    return out


def reviewer_chats(bot_data: dict) -> set:
    return set(chats(bot_data).values())


def who_by_chat(bot_data: dict, chat_id) -> str | None:
    return next((who for who, c in chats(bot_data).items() if c == str(chat_id)), None)


def resolve(bot_data: dict, who: str) -> tuple[str, str | None]:
    """(кто на деле получает, его чат): Лолы нет в боте — Егор."""
    known = chats(bot_data)
    if who in known:
        return who, known[who]
    return EGOR, known.get(EGOR)


def summary_line(who: str, decision: str, ref: str, name: str, status_label: str = "",
                 declined_upgrade: bool = False) -> str:
    """«ℹ️ Лола приняла AW-26-0225 как уведомление» — итог для второго проверяющего."""
    def verb(masc: str, fem: str) -> str:
        return fem if who in _FEMININE else masc

    what = escape(ref or f"«{name}»")
    if ref and name:
        what += f" · {escape(name)}"
    person = NAMES.get(who, who)
    if declined_upgrade:
        return f"ℹ️ {person} {verb('отклонил', 'отклонила')} повышение статуса {what} — плейсмент зафиксирован уведомлением"
    if decision.startswith("Accept"):
        tail = f" как {status_label}" if status_label else ""
        return f"ℹ️ {person} {verb('принял', 'приняла')} {what}{tail}"
    if decision == Decision.DECLINE:
        return f"ℹ️ {person} {verb('отклонил', 'отклонила')} {what}"
    return f"ℹ️ {person} {verb('запросил', 'запросила')} информацию по {what}"
