"""В боте — только дилеры и партнёры (и Andreu World, общий профиль AW); подрядчики, бюро, клиенты — нет."""
from bot.airtable_ids import Partners
from bot.airtable_repo import AW_COMPANY, AirtableRepo

TYPES = {"recDealer": "Dealer", "recPartner": "Partner", "recContr": "Contractor", "recStudio": "Studio"}


def _repo():
    repo = AirtableRepo.__new__(AirtableRepo)
    repo.company_types = lambda ids: [TYPES.get(c) for c in ids]
    return repo


def test_dealers_and_partners_allowed():
    repo = _repo()
    assert repo.bot_allowed("recP", ["recDealer"])
    assert repo.bot_allowed("recP", ["recPartner"])
    assert repo.bot_allowed("recP", ["recStudio", "recPartner"])


def test_contractors_studios_and_no_company_not_allowed():
    repo = _repo()
    assert not repo.bot_allowed("recP", ["recContr"])
    assert not repo.bot_allowed("recP", ["recStudio"])
    assert not repo.bot_allowed("recP", [])


def test_aw_and_shared_profile_allowed():
    repo = _repo()
    assert repo.bot_allowed("recP", [AW_COMPANY])
    assert repo.bot_allowed(next(iter(Partners.SENDERS)), [])
