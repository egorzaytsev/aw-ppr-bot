from bot import routing


def test_route_by_company_type():
    assert routing.route(["Dealer"]) == routing.LOLA
    assert routing.route(["Partner"]) == routing.EGOR
    assert routing.route([None]) == routing.EGOR
    assert routing.route([]) == routing.EGOR
    assert routing.route(["Dealer / Partner"]) == routing.EGOR


def test_resolve_falls_back_to_egor_until_lola_known():
    data = {"admin_chat_id": "1"}
    assert routing.resolve(data, routing.LOLA) == (routing.EGOR, "1")
    data["lola_chat_id"] = "2"
    assert routing.resolve(data, routing.LOLA) == (routing.LOLA, "2")
    assert routing.reviewer_chats(data) == {"1", "2"}
    assert routing.who_by_chat(data, 2) == routing.LOLA and routing.who_by_chat(data, 3) is None


def test_summary_lines():
    assert routing.summary_line(routing.LOLA, "Accept", "AW-26-0225", "", "уведомление") == \
        "ℹ️ Лола приняла AW-26-0225 как уведомление"
    assert routing.summary_line(routing.EGOR, "Decline", "", "Медалл") == "ℹ️ Егор отклонил «Медалл»"
    assert routing.summary_line(routing.LOLA, "Need more info", "", "Медалл") == \
        "ℹ️ Лола запросила информацию по «Медалл»"
    assert "плейсмент зафиксирован уведомлением" in routing.summary_line(
        routing.LOLA, "Decline", "AW-26-0225", "X", declined_upgrade=True)
