from bot import dedup
from bot.admin import weak_answers


K5 = {
    "End Client": "Музей Московского Кремля", "Lead Architect": "нет архиектора",
    "AW Alternatives": "No — AW only", "Competing Dealers": "пока не получили данные, кто именно",
    "Parallel Registrations": "Нет", "Decision Maker": "пока неизвестно", "Third-party Statement": "нет",
    "Quotation / Proforma №": "20101422", "Area Source": "со слов",
}


def test_weak_answers_lists_vague_and_skipped():
    weak = weak_answers(K5)
    text = "\n".join(weak)
    assert "нет архиектора" in text and "пока неизвестно" in text and "пока не получили" in text
    assert any("«нет»" in w for w in weak)                 # документы не присланы
    assert "Нет»" not in text                              # «Нет» по параллельным регистрациям — ответ
    assert not any("20101422" in w for w in weak)


def test_name_rules():
    assert dedup.name_problem("K5 Музей Московского Кремля")
    assert dedup.name_problem("A" * 81)
    assert dedup.name_problem("K5 Moscow Kremlin Museums") is None
    assert dedup.to_latin("K5 Музей Московского Кремля") == "K5 Muzei Moskovskogo Kremlya"
