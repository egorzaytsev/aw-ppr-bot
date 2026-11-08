"""Память ожиданий бота, переживающая перезапуск (Railway выкладка).

Хранится в одной служебной записи Airtable (repo.memo_load / memo_save); в bot_data — копия.
Ключи:
- waits: {chat_id: [действие, a, b]} — проверяющий вводит текст (откат, другое время звонка, …);
- calls_confirmed: ["<заявка>:<№ звонка>"], calls_reminded: ["<начало>|<проект>"];
- ready_offered: {"<проект>:<менеджер>": "YYYY-MM-DD"};
- stepwise: {<заявка>: {...}}, stepwise_next: {"<проект>:<менеджер>": {...}}.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_KEY = "memo"


async def _data(bot_data: dict) -> dict:
    data = bot_data.get(_KEY)
    if data is None:
        repo = bot_data.get("repo")
        data = {}
        if repo is not None and hasattr(repo, "memo_load"):
            try:
                data = await asyncio.to_thread(repo.memo_load)
            except Exception:
                log.warning("Память бота не загрузилась — работаю с пустой", exc_info=True)
        bot_data[_KEY] = data
    return data


async def get(bot_data: dict, key: str, default=None):
    data = await _data(bot_data)
    if key not in data and default is not None:
        data[key] = default
    return data.get(key, default)


async def save(bot_data: dict) -> None:
    repo = bot_data.get("repo")
    if repo is None or not hasattr(repo, "memo_save"):
        return
    try:
        await asyncio.to_thread(repo.memo_save, await _data(bot_data))
    except Exception:
        log.warning("Память бота не сохранилась", exc_info=True)


async def put(bot_data: dict, key: str, value) -> None:
    (await _data(bot_data))[key] = value
    await save(bot_data)


async def pop(bot_data: dict, key: str, sub: str, default=None):
    """Достать и удалить элемент словаря key[sub]."""
    data = await _data(bot_data)
    box = data.get(key) or {}
    if sub not in box:
        return default
    value = box.pop(sub)
    data[key] = box
    await save(bot_data)
    return value


async def set_item(bot_data: dict, key: str, sub: str, value) -> None:
    data = await _data(bot_data)
    box = data.get(key) or {}
    box[sub] = value
    data[key] = box
    await save(bot_data)
