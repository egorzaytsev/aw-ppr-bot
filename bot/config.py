"""Настройки только из переменных окружения. Локально подхватывается .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    # Минимальный загрузчик .env для локального запуска; на Railway файла нет.
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    airtable_token: str
    airtable_base_id: str
    groq_api_key: str
    groq_model: str
    admin_chat_id: str | None
    # Токен @hareworldbot — сбор Telegram для сводки CRM в Буфер; пусто — сбор выключен
    collector_token: str | None
    log_level: str


def load_settings() -> Settings:
    _load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    return Settings(
        telegram_token=_required("TELEGRAM_BOT_TOKEN"),
        airtable_token=_required("AIRTABLE_TOKEN"),
        airtable_base_id=os.environ.get("AIRTABLE_BASE_ID", "appD8N8DAgcTAqfL1").strip(),
        groq_api_key=_required("GROQ_API_KEY"),
        groq_model=os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3").strip(),
        admin_chat_id=os.environ.get("ADMIN_CHAT_ID", "").strip() or None,
        collector_token=os.environ.get("HAREWORLD_BOT_TOKEN", "").strip() or None,
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper(),
    )
