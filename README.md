# AW PPR Bot

Telegram-бот регистрации продакт плейсмента Andreu World в проектах (PPR), EEU.
Python 3.12 · python-telegram-bot 22 · pyairtable 3 · Groq Whisper · Railway.

## Структура

```
bot/
  main.py           хендлеры Telegram, запуск (long polling)
  engine.py         движок сценария: вопрос → ответ → сводка → отправка
  scenarios/
    base.py         модель вопроса, разбор чисел/дат/вариантов
    new_project.py  сценарий «Новый проект» (фаза 1)
  state.py          состояние диалога ↔ JSON в People → Bot State
  airtable_repo.py  People (поиск по Chat ID / username), Registrations (заявка)
  airtable_ids.py   ID таблиц и полей
  voice.py          голосовые → текст через Groq
  config.py         переменные окружения
tests/              тесты движка (pytest)
```

## Локальный запуск

```bash
cp .env.example .env   # заполнить токены
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m bot.main
```

Тесты: `.venv/bin/python -m pytest -q tests`

## Railway

Сервис из репозитория, старт: `python -m bot.main` (уже в `railway.json`).
В Variables задать `TELEGRAM_BOT_TOKEN`, `AIRTABLE_TOKEN`, `GROQ_API_KEY`;
по желанию `AIRTABLE_BASE_ID`, `ADMIN_CHAT_ID`, `GROQ_WHISPER_MODEL`.
Одна реплика: при двух инстансах polling Telegram вернёт конфликт.

Токен Airtable: scopes `data.records:read`, `data.records:write`, доступ только к базе EEU Ecosystem CRM.
