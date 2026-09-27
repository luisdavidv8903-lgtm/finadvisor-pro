"""
WhatsApp VIP offer-board service -- covers idempotent creation (the actual
correctness property routers/whatsapp.py's transaction-ordering fix depends
on), never-show-expired listing, and idempotent close/interest.
"""
from datetime import timedelta
from decimal import Decimal

import pytest

from backend import timeutils
from backend.messaging.whatsapp_vip_identity import authorize_whatsapp_id, get_or_create_identity
from backend.models import Channel, VipCurrency, VipOfferIntent, VipOfferStatus, VipSettlementMethod
from backend.services import whatsapp_vip_offers as svc


def _identity(db, wa_id: str):
    authorize_whatsapp_id(db, wa_id)
    return get_or_create_identity(db, wa_id)


def _make_offer(db, identity, *, source_message_id: str = "wamid.offer1", duration_hours: int = 24, give="CUP", receive="USD"):
    return svc.create_offer(
        db,
        identity,
        source_message_id=source_message_id,
        intent=VipOfferIntent.COMPRO,
        give_currency=VipCurrency(give),
        receive_currency=VipCurrency(receive),
        amount=Decimal("4000"),
        min_amount=None,
        rate_value=Decimal("445"),
        rate_currency=VipCurrency(give),
        give_method=VipSettlementMethod.CASH,
        give_method_detail=None,
        give_location="La Habana",
        receive_method=VipSettlementMethod.ZELLE,
        receive_method_detail=None,
        receive_location="Miami",
        duration_hours=duration_hours,
    )


def test_create_offer_requires_active_vip(db_session):
    identity = get_or_create_identity(db_session, "wa-no-vip")
    with pytest.raises(Exception):
        _make_offer(db_session, identity)


def test_create_offer_persists_all_fields(db_session):
    identity = _identity(db_session, "wa-create-1")
    offer = _make_offer(db_session, identity)
    assert offer.status == VipOfferStatus.ACTIVE
    assert offer.give_currency == VipCurrency.CUP
    assert offer.receive_currency == VipCurrency.USD
    assert offer.amount == Decimal("4000")
    assert offer.expires_at is not None


def test_create_offer_rejects_same_give_and_receive_currency(db_session):
    identity = _identity(db_session, "wa-create-2")
    with pytest.raises(svc.OfferError):
        _make_offer(db_session, identity, give="USD", receive="USD")


def test_create_offer_rejects_rate_currency_not_in_pair(db_session):
    identity = _identity(db_session, "wa-create-3")
    with pytest.raises(svc.OfferError):
        svc.create_offer(
            db_session,
            identity,
            source_message_id="wamid.badrate",
            intent=VipOfferIntent.COMPRO,
            give_currency=VipCurrency.CUP,
            receive_currency=VipCurrency.USD,
            amount=Decimal("100"),
            min_amount=None,
            rate_value=Decimal("1"),
            rate_currency=VipCurrency.EUR,  # neither give nor receive
            give_method=VipSettlementMethod.CASH,
            give_method_detail=None,
            give_location="X",
            receive_method=VipSettlementMethod.CASH,
            receive_method_detail=None,
            receive_location="Y",
            duration_hours=24,
        )


def test_create_offer_other_method_requires_detail(db_session):
    identity = _identity(db_session, "wa-create-4")
    with pytest.raises(svc.OfferError):
        svc.create_offer(
            db_session,
            identity,
            source_message_id="wamid.otherfail",
            intent=VipOfferIntent.VENDO,
            give_currency=VipCurrency.USD,
            receive_currency=VipCurrency.CUP,
            amount=Decimal("100"),
            min_amount=None,
            rate_value=Decimal("445"),
            rate_currency=VipCurrency.CUP,
            give_method=VipSettlementMethod.OTHER,
            give_method_detail="   ",  # blank after strip
            give_location="X",
            receive_method=VipSettlementMethod.CASH,
            receive_method_detail=None,
            receive_location="Y",
            duration_hours=24,
        )


def test_create_offer_is_idempotent_by_source_message_id(db_session):
    """THE key correctness property for webhook-redelivery safety: calling
    create_offer twice with the SAME source_message_id must return the SAME
    row, never create a second one -- this is what makes routers/whatsapp.py's
    marker-committed-last ordering safe under at-least-once handler
    execution."""
    identity = _identity(db_session, "wa-idempotent-1")
    first = _make_offer(db_session, identity, source_message_id="wamid.dup1")
    second = _make_offer(db_session, identity, source_message_id="wamid.dup1")
    assert first.id == second.id

    from backend.models import WhatsAppVipOfferDB

    assert db_session.query(WhatsAppVipOfferDB).filter(WhatsAppVipOfferDB.source_message_id == "wamid.dup1").count() == 1


def test_list_active_offers_excludes_expired_even_if_status_column_stale(db_session):
    """expires_at is checked directly, never trusting a status column that no
    sweep has flipped yet."""
    owner = _identity(db_session, "wa-list-owner")
    offer = _make_offer(db_session, owner, source_message_id="wamid.expiring1", duration_hours=24)
    # Force it into the past WITHOUT touching status -- simulates "no sweep
    # has run yet" exactly.
    offer.expires_at = timeutils.utcnow() - timedelta(hours=1)
    db_session.commit()
    assert offer.status == VipOfferStatus.ACTIVE  # column still says ACTIVE

    viewer = _identity(db_session, "wa-list-viewer")
    active = svc.list_active_offers(db_session, owner_channel=Channel.WHATSAPP, exclude_identity=viewer)
    assert offer.id not in [o.id for o in active]


def test_list_active_offers_excludes_own_offers_when_requested(db_session):
    owner = _identity(db_session, "wa-list-owner2")
    offer = _make_offer(db_session, owner, source_message_id="wamid.own1")
    active = svc.list_active_offers(db_session, owner_channel=Channel.WHATSAPP, exclude_identity=owner)
    assert offer.id not in [o.id for o in active]


def test_list_active_offers_requires_owner_channel_and_never_leaks_across_channels(db_session):
    """2026-09-27 audit, second pass: list_active_offers had NO channel
    filter at all until this fix -- OFERTAS on Telegram could show a
    WhatsApp-owned offer and vice versa. owner_channel is now a REQUIRED
    keyword (no default) specifically so this can never silently regress."""
    from backend.messaging.whatsapp_vip_identity import get_or_create_identity as get_or_create_any_identity
    from backend.models import VipAuthorizationMethod, VipOfferStatus, WhatsAppVipMembershipDB

    whatsapp_owner = _identity(db_session, "wa-crosscheck-owner")
    _make_offer(db_session, whatsapp_owner, source_message_id="wamid.crosscheck1")

    telegram_owner = get_or_create_any_identity(db_session, "tg-crosscheck-owner", channel=Channel.TELEGRAM)
    # No Telegram equivalent of authorize_whatsapp_id exists by design (VIP
    # access there is invite-only via redeem_vip_invite, see
    # whatsapp_vip_identity.py) -- grant membership directly here since this
    # test is exercising list_active_offers at the service layer, not the
    # chat flow.
    db_session.add(WhatsAppVipMembershipDB(channel_identity_id=telegram_owner.id, authorized_via=VipAuthorizationMethod.DIRECT))
    db_session.commit()
    svc.create_offer(
        db_session,
        telegram_owner,
        source_message_id="tgmid.crosscheck1",
        intent=VipOfferIntent.COMPRO,
        give_currency=VipCurrency("CUP"),
        receive_currency=VipCurrency("USD"),
        amount=Decimal("4000"),
        min_amount=None,
        rate_value=Decimal("445"),
        rate_currency=VipCurrency("CUP"),
        give_method=VipSettlementMethod.CASH,
        give_method_detail=None,
        give_location="La Habana",
        receive_method=VipSettlementMethod.ZELLE,
        receive_method_detail=None,
        receive_location="Miami",
        duration_hours=24,
    )

    with pytest.raises(TypeError):
        svc.list_active_offers(db_session)  # owner_channel omitted -- must fail loudly, never silently list everything

    whatsapp_active = svc.list_active_offers(db_session, owner_channel=Channel.WHATSAPP)
    assert all(o.status == VipOfferStatus.ACTIVE for o in whatsapp_active)
    assert "wamid.crosscheck1" in [o.source_message_id for o in whatsapp_active]
    assert "tgmid.crosscheck1" not in [o.source_message_id for o in whatsapp_active]

    telegram_active = svc.list_active_offers(db_session, owner_channel=Channel.TELEGRAM)
    assert "tgmid.crosscheck1" in [o.source_message_id for o in telegram_active]
    assert "wamid.crosscheck1" not in [o.source_message_id for o in telegram_active]


def test_my_offers_lazily_corrects_stored_status_to_expired(db_session):
    owner = _identity(db_session, "wa-my-owner")
    offer = _make_offer(db_session, owner, source_message_id="wamid.expiring2")
    offer.expires_at = timeutils.utcnow() - timedelta(hours=1)
    db_session.commit()

    mine = svc.get_my_offers(db_session, owner)
    assert mine[0].status == VipOfferStatus.EXPIRED  # now persisted, not just computed


def test_close_offer_is_idempotent(db_session):
    owner = _identity(db_session, "wa-close-1")
    offer = _make_offer(db_session, owner, source_message_id="wamid.close1")
    first = svc.close_offer(db_session, owner, offer)
    assert first.status == VipOfferStatus.CLOSED
    closed_at_first = first.closed_at

    second = svc.close_offer(db_session, owner, offer)
    assert second.status == VipOfferStatus.CLOSED
    assert second.closed_at == closed_at_first  # never re-stamped


def test_close_offer_only_owner_can_close(db_session):
    owner = _identity(db_session, "wa-close-2")
    stranger = _identity(db_session, "wa-close-3")
    offer = _make_offer(db_session, owner, source_message_id="wamid.close2")
    with pytest.raises(svc.OfferError):
        svc.close_offer(db_session, stranger, offer)


def test_register_interest_is_idempotent(db_session):
    owner = _identity(db_session, "wa-interest-owner")
    interested = _identity(db_session, "wa-interest-1")
    offer = _make_offer(db_session, owner, source_message_id="wamid.interest1")

    first, created_first = svc.register_interest(db_session, interested, offer)
    second, created_second = svc.register_interest(db_session, interested, offer)
    assert created_first is True
    assert created_second is False
    assert first.id == second.id

    from backend.models import WhatsAppVipOfferInterestDB

    assert db_session.query(WhatsAppVipOfferInterestDB).filter(WhatsAppVipOfferInterestDB.offer_id == offer.id).count() == 1


def test_cannot_express_interest_in_own_offer(db_session):
    owner = _identity(db_session, "wa-interest-owner2")
    offer = _make_offer(db_session, owner, source_message_id="wamid.interest2")
    with pytest.raises(svc.OfferError):
        svc.register_interest(db_session, owner, offer)


def test_cannot_express_interest_in_expired_offer(db_session):
    owner = _identity(db_session, "wa-interest-owner3")
    interested = _identity(db_session, "wa-interest-2")
    offer = _make_offer(db_session, owner, source_message_id="wamid.interest3")
    offer.expires_at = timeutils.utcnow() - timedelta(hours=1)
    db_session.commit()

    with pytest.raises(svc.OfferError):
        svc.register_interest(db_session, interested, offer)


def test_duration_hours_must_be_allowed_value(db_session):
    identity = _identity(db_session, "wa-duration-1")
    with pytest.raises(svc.OfferError):
        _make_offer(db_session, identity, source_message_id="wamid.baddur", duration_hours=48)
