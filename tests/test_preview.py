"""/preview: каждый сценарий собирается через настоящие обработчики; в текстах нет техники (правило 1.2)."""
import asyncio
import re

import pytest

from bot import preview

TECH = re.compile(r"\brec[A-Za-z0-9]{10,}|\b(Target Project|Decision Note|Reg Status|Need more info|PPR Next Step|"
                  r"Submitted By|Inbox|People|Accept —|Simplified \(|Pre-registered|Registered\b|Payload)")


@pytest.mark.parametrize("scenario", list(preview.SCENARIOS))
def test_preview_scenario_runs_and_is_clean(scenario):
    sent = asyncio.run(preview.record(scenario))
    assert sent, scenario
    for _, text, markup in sent:
        assert not TECH.search(text), (scenario, text)
        for row in (markup.inline_keyboard if markup else []):
            for b in row:
                assert len(b.callback_data.encode()) <= 64, b.callback_data   # лимит Telegram
                assert not TECH.search(b.text), b.text


def test_every_prompt_for_input_has_cancel():
    """Правило 1.6: каждый запрос ввода у проверяющего — с кнопкой «Отмена»."""
    for scenario in preview.SCENARIOS:
        for to, text, markup in asyncio.run(preview.record(scenario)):
            if to == str(preview.ADMIN) and re.search(r"\bНапиши\b", text) and "Дилеру отправлено" not in text:
                labels = [b.text for row in (markup.inline_keyboard if markup else []) for b in row]
                assert any(l.startswith("Отмена") for l in labels), (scenario, text)
