"""«Сообщить новость»: один файл по умолчанию — без отдельного шага."""
from bot import engine

FILE = {"kind": "file", "file_id": "F1", "name": "plan.pdf", "mime": "application/pdf"}


def _start():
    return engine.start("project_news", {"_target": {"id": "recP", "name": "TESTBANK Office FL3"}})


def test_file_then_text_goes_together():
    r = _start()
    r = engine.handle_file(r.state, FILE)
    assert "приложил" in r.screens[0].text and r.state.awaiting == "answer"
    r = engine.handle_file(r.state, dict(FILE, name="plan2.pdf"))
    assert "заменил" in r.screens[0].text
    r = engine.handle_text(r.state, "Клиент утвердил планировку")
    assert r.submit
    p = engine.build_payload(r.state)
    assert p["News File"][0]["name"] == "plan2.pdf"
    assert [k for k, _ in engine.collected_files(r.state)] == ["News File"]


def test_text_only_still_works():
    r = engine.handle_text(_start().state, "Тендер перенесли на март")
    assert r.submit and "News File" not in engine.build_payload(r.state)
