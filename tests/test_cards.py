import re

from bot import cards
from bot.airtable_ids import Projects as P

D, O1, O2 = "recSTATIO", "recOTHER1", "recOTHER2"


def plain(text: str) -> str:
    return re.sub(r"</?(b|code)>", "", text)


def project(**kw):
    base = {P.NAME: "MedallClinic SPB", P.PPR_REF: "AW-26-0220", P.PPR_TRACK: "Full", P.NOTIFIED: [D]}
    base.update(kw)
    return base


def test_header_and_full_track_missing_lines():
    p = project(**{P.PPR_NEXT_MISSING: "Contact Level (needs proof); Dossier Files; Expected Order Date;",
                   P.CONTACT_LEVEL: "Declared"})
    text = plain(cards.accept_card(p, D, "STATIO", {}, first_time=True))
    lines = text.splitlines()
    assert lines[:6] == [
        "✅ Заявка принята",
        "🔖 Референс: AW-26-0220",
        "🏗 Название проекта: MedallClinic SPB",
        "👤 STATIO · статус уведомление",
        "🔎 Других статусов на объекте нет",
        "━━━━━━━━━━━━━━━",
    ]
    assert "➡️ Для перехода на предрегистрацию не хватает:" in lines
    assert ("🤝 Подтверждение контакта с клиентом или бюро — сейчас «заявляю, без подтверждения». "
            "Нужен документ от бюро, переписка или контакт.") in lines
    assert "📎 Файлы досье — визуализации, планировки, список серий." in lines
    assert "📅 Месяц размещения заказа." in lines
    assert "👉 Всё это можно добавить через «Обновить проект» → «Дополнить материалы»." in lines
    assert lines[-2:] == ["✉️ Указывай название проекта и референс в теме писем по этому проекту:",
                          "MedallClinic SPB AW-26-0220"]
    assert "<code>MedallClinic SPB AW-26-0220</code>" in cards.accept_card(p, D, "STATIO", {}, first_time=True)


def test_all_data_present_and_no_tail_on_repeat():
    text = plain(cards.accept_card(project(), D, "STATIO", {}, first_time=False))
    assert "✔️ Все данные для следующего этапа есть. Нажми «Обновить проект» → «Запросить следующий этап»." in text
    assert "✉️" not in text


def test_simplified_body():
    p = project(**{P.PPR_TRACK: "Simplified (<30k)", P.EXW_POTENTIAL: 9000})
    text = plain(cards.accept_card(p, D, "STATIO", {"PL Volume": 20000}, first_time=False))
    assert "💶 Объём по прайсу: 20 000 € — ниже порога 30 000 €" in text
    assert "Квотацию запрашивай напрямую в бэк-офисе, Егор в копии." in text
    assert "📈 Если объём вырастет выше 30 000 € — обнови его через «Обновить проект»" in text
    assert "➡️" not in text


def test_others_without_names_and_queue_position():
    p = project(**{P.NOTIFIED: [], P.PRE_REGISTERED: [O1, D]})
    text = plain(cards.accept_card(p, D, "STATIO", {}, first_time=False))
    assert "👤 STATIO · статус предрегистрация" in text
    assert "🔎 На объекте есть действующая предрегистрация другого партнёра" in text
    assert "🔢 Твой номер в предрегистрации — второй из двух" in text
    assert "recOTHER1" not in text
    p2 = project(**{P.REGISTERED: [O2]})
    text2 = plain(cards.accept_card(p2, D, "STATIO", {}, first_time=False))
    assert "🔎 На объекте есть действующая регистрация другого партнёра" in text2
    assert "Регистрация на объекте уже у другого партнёра" in text2


def test_admin_copy_and_update_card():
    card = cards.update_card(project(), D, "STATIO", "📝 Информацию внесли в проект.", note="Спасибо")
    assert plain(card).endswith("📝 Информацию внесли в проект.\n\n💬 Комментарий: Спасибо")
    admin = plain(cards.admin_copy(card, True, ["🆕 Проект создан"]))
    assert admin.startswith("🛠 Взято в работу\n🔖 Референс: AW-26-0220")
    assert admin.endswith("🆕 Проект создан\n📨 Дилеру отправлено")


def test_list_label_with_ref_and_next_stage_rules():
    from bot.airtable_repo import DealerProject
    from bot.main import _button_label, _next_request_type
    p = DealerProject("recP", "MedallClinic SPB", "notified", None, "Full", {P.PPR_REF: "AW-26-0220"})
    assert _button_label(p) == "AW-26-0220 · MedallClinic SPB · уведомление"
    assert _next_request_type(p) == "Upgrade — Pre-registration"
    p.fields[P.PPR_NEXT_MISSING] = "Dossier Files;"
    assert _next_request_type(p) is None
    pre = DealerProject("recP", "X", "pre_registered", None, "Full", {})
    assert _next_request_type(pre) == "Upgrade — Registration"
    assert _next_request_type(DealerProject("recP", "X", "pre_registered", None, "Full", {P.REGISTERED: ["r"]})) is None
    assert _next_request_type(DealerProject("recP", "X", "notified", None, "Simplified (<30k)", {})) is None
    assert _next_request_type(DealerProject("recP", "X", "registered", None, "Full", {})) is None


def test_admin_request_card_russian_values_warning_and_changes():
    import json
    from bot.admin import request_card
    from bot.airtable_ids import Registrations as R
    reg = {R.DEALER_PROJECT_NAME: "Медалл", R.REQUEST_TYPE: "New — Pre-registration",
           R.PAYLOAD: json.dumps({"Project Stage": "Design", "Contact Level": "Declared",
                                  "AW Alternatives": "Yes — tender requirement"}, ensure_ascii=False)}
    text = plain(request_card(reg, "recR", "Lola", None, "STATIO"))
    assert "Стадия: проектирование" in text and "Уровень контакта: Заявляю (без подтверждения)" in text
    assert "Альтернативы AW: Да, по требованию тендера" in text
    assert "⚠️ Уровень контакта — заявляю, без подтверждения. Для предрегистрации рекомендуется запросить документы" in text
    upd = {R.DEALER_PROJECT_NAME: "Медалл", R.REQUEST_TYPE: "Update — fields",
           R.PAYLOAD: json.dumps({"Object Address": "ул. Лесная, 9", "Contact Level": "Joint meeting with AW",
                                  "Dealer Project Name": "Медалл"}, ensure_ascii=False)}
    t2 = plain(request_card(upd, "recU"))
    assert "📝 Изменения:" in t2 and "• Адрес: ул. Лесная, 9" in t2
    assert "• Уровень контакта: Готов к совместной встрече" in t2 and "Dealer Project Name" not in t2


def test_decision_markup_merge_buttons():
    from types import SimpleNamespace
    from bot.admin import decision_markup
    ctx = SimpleNamespace(bot_data={"base_id": "appX"})
    rows = decision_markup(ctx, "recR", [("recA", "Petrovitch"), ("recB", "Technodom")]).inline_keyboard
    labels = [b.text for row in rows for b in row]
    assert labels[:3] == ["🔗 Слить с: Petrovitch", "🔗 Слить с: Technodom", "🆕 Принять как новый"]
    assert rows[0][0].callback_data == "ad:pick:recR:recA"
    assert [b.text for b in decision_markup(ctx, "recR").inline_keyboard[0]] == ["Принять"]


def test_declined_upgrade_card():
    text = plain(cards.declined_upgrade_card(project(), D, "STATIO", {}, "предрегистрация", "Нет документов", True))
    lines = text.splitlines()
    assert lines[0] == "❌ Предрегистрация не подтверждена"
    assert "👤 STATIO · статус уведомление" in lines
    assert "📌 Плейсмент зафиксирован как уведомление." in lines and "💬 Причина: Нет документов" in lines
    assert plain(cards.admin_copy(cards.declined_upgrade_card(project(), D, "STATIO", {}, "предрегистрация", "", False),
                                  True)).startswith("🛠 ❌ Предрегистрация не подтверждена")


def test_architect_selection_bureau_line():
    p = project(**{P.PPR_NEXT_MISSING: "Specifier;", P.PROJECT_STAGE: "Architect selection"})
    assert "🏛 Архитектурное бюро — как только клиент его выберет." in plain(cards.accept_card(p, D, "S", {}, False))


def test_admin_card_shows_aw_origin():
    import json
    from bot.admin import request_card
    from bot.airtable_ids import Registrations as R
    reg = {R.DEALER_PROJECT_NAME: "Медалл", R.REQUEST_TYPE: "New — Notification",
           R.PAYLOAD: json.dumps({"Specifier": "UNK Project", "AW Origin": "Нет — AW предлагает дилер взамен продукта бюро"},
                                 ensure_ascii=False)}
    assert "AW в проекте бюро: Нет — AW предлагает дилер взамен продукта бюро" in plain(request_card(reg, "recR"))
