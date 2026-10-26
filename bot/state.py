"""Состояние диалога, хранится JSON-ом в People → Bot State."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Что бот ждёт от пользователя
AWAIT_ANSWER = "answer"    # ответ на текущий вопрос
AWAIT_QUALIFIER = "qualifier"  # уточнение к ответу (кнопки)
AWAIT_DETAIL = "detail"  # текст после «Да, укажу»
AWAIT_EVIDENCE = "evidence"  # файл/ссылка-подтверждение к ответу
AWAIT_GATE = "gate"  # переход в следующую фазу: Да / Нет
AWAIT_MONTH = "month"  # выбран год, ждём месяц (кнопки)
AWAIT_SLOT = "slot"  # выбран день, ждём окно времени (кнопки)
AWAIT_CONFIRM = "confirm"  # подтверждение сводки


@dataclass
class DialogState:
    scenario: str
    phase: int = 1
    step: int = 0
    awaiting: str = AWAIT_ANSWER
    answers: dict = field(default_factory=dict)
    # Если задан — после ответа на этот шаг вернуться к сводке (режим «Исправить»)
    editing: bool = False
    # Режим «Дозаполнить»: идём только по пропущенным вопросам
    filling: bool = False
    # Фаза, отказ от перехода из которой дилер уже выбрал (0 — не отказывался)
    declined_gate: int = 0
    # Год, выбранный в пикере месяца
    pick_year: int = 0
    # День, выбранный в пикере слота (YYYY-MM-DD)
    pick_date: str = ""
    # Напоминание о брошенной заявке уже отправлено
    nudged: bool = False
    # Дилер выбрал следующий этап, но в текущей фазе есть пропуски: сначала дозаполняем их
    # (без «Пропустить»), потом — вопросы следующей фазы
    prefill: bool = False
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | None) -> DialogState | None:
        if not raw or not raw.strip():
            return None
        try:
            data = json.loads(raw)
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            return cls(**known)
        except (ValueError, TypeError) as exc:
            log.warning("Битый Bot State, игнорирую: %s", exc)
            return None
