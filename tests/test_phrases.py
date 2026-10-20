from bot import engine, phrases
from bot.scenarios import NEW_PROJECT


def test_pick_never_repeats_twice_in_a_row():
    phrases.seed(1)
    seen = [phrases.pick(phrases.ACKS, "t") for _ in range(50)]
    assert all(a != b for a, b in zip(seen, seen[1:]))
    assert len(set(seen)) > 3


def test_first_name():
    assert phrases.first_name("Egor Zaitcev (me)") == "Egor"
    assert phrases.first_name("Lola Sapaeva") == "Lola" and phrases.first_name("") == ""


def test_ack_prefix_and_last_steps_hint():
    phrases.seed(3)
    r = engine.start(NEW_PROJECT.code)
    texts = []
    for answer in ("ПАО Сбербанк", "Офис", "Лобби"):
        r = engine.handle_text(r.state, answer)
        texts.append(r.screens[0].text)
    # Реакция бывает не всегда, но встречается и стоит в начале
    assert any(t.split(" ")[0] in phrases.ACKS or t.startswith(tuple(phrases.ACKS)) for t in texts)
    st = r.state
    st.step = 13  # последний вопрос фазы 1 — альтернативы
    assert "Последний вопрос — и готово." in engine.question_screen(st).text
