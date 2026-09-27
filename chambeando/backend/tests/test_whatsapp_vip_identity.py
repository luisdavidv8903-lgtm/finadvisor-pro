"""
WhatsApp VIP identity/membership -- the hard requirement under test
throughout this file is requirement 1 from the pilot spec: a webhook message
NEVER proves group membership. No function reachable from chat/HTTP may
create an ACTIVE WhatsAppVipMembershipDB row on its own.
"""
import pytest

from backend.messaging import whatsapp_vip_identity as svc
from backend.models import MembershipStatus, VipAuthorizationMethod


def test_talking_to_the_bot_never_grants_access(db_session):
    """get_or_create_identity is what every inbound message triggers -- it
    must NEVER also create a membership."""
    identity = svc.get_or_create_identity(db_session, "wa-never-authorized")
    assert svc.get_membership(db_session, identity) is None
    with pytest.raises(svc.MembershipRejected) as excinfo:
        svc.require_active_vip(db_session, identity)
    assert excinfo.value.reason == "not authorized"


def test_authorize_whatsapp_id_grants_active_access(db_session):
    membership = svc.authorize_whatsapp_id(db_session, "wa-direct-1", note="known VIP member")
    assert membership.status == MembershipStatus.ACTIVE
    assert membership.authorized_via == VipAuthorizationMethod.DIRECT

    identity = svc.get_or_create_identity(db_session, "wa-direct-1")
    assert svc.require_active_vip(db_session, identity).id == membership.id


def test_authorize_whatsapp_id_is_idempotent(db_session):
    first = svc.authorize_whatsapp_id(db_session, "wa-direct-2")
    second = svc.authorize_whatsapp_id(db_session, "wa-direct-2")
    assert first.id == second.id


def test_authorize_never_resets_a_revoked_membership(db_session):
    membership = svc.authorize_whatsapp_id(db_session, "wa-direct-3")
    svc.revoke_membership(db_session, membership, reason="testing revoke")

    again = svc.authorize_whatsapp_id(db_session, "wa-direct-3")
    assert again.status == MembershipStatus.SUSPENDED


def test_revoked_membership_is_rejected(db_session):
    membership = svc.authorize_whatsapp_id(db_session, "wa-direct-4")
    identity = svc.get_or_create_identity(db_session, "wa-direct-4")
    svc.revoke_membership(db_session, membership, reason="testing")

    with pytest.raises(svc.MembershipRejected) as excinfo:
        svc.require_active_vip(db_session, identity)
    assert excinfo.value.reason == "suspended"


def test_invite_code_is_plaintext_once_never_persisted(db_session):
    code = svc.create_vip_invite(db_session, max_uses=1, ttl_days=7, note="for Maria")
    assert len(code) > 10

    from backend.models import WhatsAppVipInviteDB

    stored = db_session.query(WhatsAppVipInviteDB).first()
    assert stored is not None
    assert stored.code_hash != code
    assert code not in (stored.code_hash or "")


def test_redeem_valid_invite_grants_access(db_session):
    code = svc.create_vip_invite(db_session, max_uses=1, ttl_days=7)
    membership = svc.redeem_vip_invite(db_session, "wa-invite-1", code)
    assert membership.status == MembershipStatus.ACTIVE
    assert membership.authorized_via == VipAuthorizationMethod.INVITE


def test_invite_is_single_use_by_default(db_session):
    code = svc.create_vip_invite(db_session, max_uses=1, ttl_days=7)
    svc.redeem_vip_invite(db_session, "wa-invite-2", code)

    with pytest.raises(svc.InviteRedemptionError) as excinfo:
        svc.redeem_vip_invite(db_session, "wa-invite-3", code)
    assert excinfo.value.reason == "exhausted"


def test_invite_with_multiple_uses_allows_that_many_redemptions(db_session):
    code = svc.create_vip_invite(db_session, max_uses=2, ttl_days=7)
    svc.redeem_vip_invite(db_session, "wa-invite-multi-1", code)
    svc.redeem_vip_invite(db_session, "wa-invite-multi-2", code)
    with pytest.raises(svc.InviteRedemptionError):
        svc.redeem_vip_invite(db_session, "wa-invite-multi-3", code)


def test_invalid_invite_code_rejected(db_session):
    with pytest.raises(svc.InviteRedemptionError) as excinfo:
        svc.redeem_vip_invite(db_session, "wa-invite-4", "totally-made-up-code")
    assert excinfo.value.reason == "invalid code"


def test_expired_invite_rejected(db_session, monkeypatch):
    from backend import timeutils

    code = svc.create_vip_invite(db_session, max_uses=1, ttl_days=7)

    future = timeutils.utcnow() + __import__("datetime").timedelta(days=8)
    monkeypatch.setattr(timeutils, "utcnow", lambda: future)
    with pytest.raises(svc.InviteRedemptionError) as excinfo:
        svc.redeem_vip_invite(db_session, "wa-invite-5", code)
    assert excinfo.value.reason == "expired"


def test_revoked_invite_rejected(db_session):
    code = svc.create_vip_invite(db_session, max_uses=1, ttl_days=7)
    from backend.models import WhatsAppVipInviteDB

    invite = db_session.query(WhatsAppVipInviteDB).first()
    svc.revoke_vip_invite(db_session, invite)

    with pytest.raises(svc.InviteRedemptionError) as excinfo:
        svc.redeem_vip_invite(db_session, "wa-invite-6", code)
    assert excinfo.value.reason == "revoked"


def test_already_authorized_identity_cannot_redeem_another_invite(db_session):
    svc.authorize_whatsapp_id(db_session, "wa-already-1")
    code = svc.create_vip_invite(db_session, max_uses=1, ttl_days=7)
    with pytest.raises(svc.InviteRedemptionError) as excinfo:
        svc.redeem_vip_invite(db_session, "wa-already-1", code)
    assert excinfo.value.reason == "already authorized"
