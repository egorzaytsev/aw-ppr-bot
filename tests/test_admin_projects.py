"""«Проекты моих дилеров»: Лола — свои (Owner) и Astros / Aluframe; Егор — все."""
import asyncio

from bot import admin_projects as ap, preview, routing
from bot.airtable_ids import Projects as P

ALUFRAME, ASTROS = "rec2DJSCpnhZORiyB", "recYQinVKoMACGVc1"
AKFA_LIB = {"id": "recCpCNhhySrLmh3D", "name": "AKFA University — Library building (3 floors)", "ref": "AW260236",
            "status": "Active", "owner": "Lola Sapaeva", "dealer_ids": [], "partner_ids": []}
TEG = {"id": "recTEG", "name": "AKFA TEG Alfraganus", "ref": "AW260264", "status": "Prospecting",
       "owner": "Egor Zaitcev (me)", "dealer_ids": [], "partner_ids": [ALUFRAME]}
STATIO = {"id": "recST", "name": "Petrovich BC Space", "ref": "AW260240", "status": "Active",
          "owner": "Egor Zaitcev (me)", "dealer_ids": ["recSTATIO"], "partner_ids": []}


def test_lola_sees_owned_and_partner_projects():
    ids = [p["id"] for p in ap.scoped([AKFA_LIB, TEG, STATIO], routing.LOLA)]
    assert ids == ["recCpCNhhySrLmh3D", "recTEG"]          # AKFA Library — по Owner, хоть без дилера и менеджера


def test_egor_sees_all():
    assert len(ap.scoped([AKFA_LIB, TEG, STATIO], routing.EGOR)) == 3


def test_group_key():
    assert ap.group_key(STATIO) == "recSTATIO"
    assert ap.group_key(TEG) == ALUFRAME
    assert ap.group_key(AKFA_LIB) == ap.NO_DEALER


def test_menu_flow_in_sandbox():
    async def go():
        repo, bot, ctx, app, update, dealer = preview._world()
        repo.projects[preview.PROJECT_ID][P.OWNER] = "Egor Zaitcev (me)"
        up = update(preview.ADMIN)
        await ap.on_menu(up, ctx, "dp")
        rows = bot.sent[-1][2].inline_keyboard
        assert "Проекты моих дилеров" in bot.sent[-1][1]
        group = rows[0][0].callback_data
        await ap.on_menu(up, ctx, group[len("am:"):])
        btn = bot.sent[-1][2].inline_keyboard[0][0]
        assert btn.text.startswith("AW269999 · TESTBANK Office FL3")
        await ap.on_menu(up, ctx, btn.callback_data[len("am:"):])
        assert "TESTBANK Office FL3 AW269999" in bot.sent[-1][1]
        await ap.find(up, ctx, "269999")
        assert bot.sent[-1][2].inline_keyboard[0][0].text.startswith("AW269999")
    asyncio.run(go())


def test_write_to_manager_with_file():
    from bot import admin
    from bot.airtable_ids import Projects as PP

    async def go():
        repo, bot, ctx, app, update, dealer = preview._world()
        up = update(preview.ADMIN)
        repo.projects[preview.PROJECT_ID][PP.NOTIFIED] = [preview.DEALER_ID]
        repo.projects[preview.PROJECT_ID][PP.PRE_REGISTERED] = []
        repo.projects[preview.PROJECT_ID][PP.REGISTERED] = []
        await ap.on_menu(up, ctx, f"dpw:{preview.PROJECT_ID}")
        assert "Сообщение для" in bot.sent[-1][1]
        await admin.on_admin_text(up, ctx, "Прикладываю обновлённую квотацию.", resume=False)
        assert await ap.on_file(up, ctx, {"kind": "file", "file_id": "F1", "name": "quote.pdf", "mime": "application/pdf"})
        await ap.on_menu(up, ctx, "dpd")
        assert "Так увидит" in bot.sent[-1][1] and "quote.pdf" in bot.sent[-1][1]
        await ap.on_menu(up, ctx, "dps")
        to_dealer = [t for to, t, m in bot.sent if str(to) == str(preview.DEALER)]
        assert any("Прикладываю обновлённую квотацию" in t for t in to_dealer)
        assert "Прикладываю обновлённую квотацию" in repo.projects[preview.PROJECT_ID][PP.PROJECT_LOG]
        assert "Отправлено" in bot.sent[-1][1]
        assert not await ap.on_file(up, ctx, {"kind": "file", "file_id": "F2", "name": "x.pdf"})   # черновика нет
    asyncio.run(go())
