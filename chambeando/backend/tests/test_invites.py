"""Invites: la unica via de entrada a membership."""

from backend.models import InviteDB, MemberRole, MembershipDB

from .conftest import auth_headers, login, seed_membership

ADMIN_WALLET = "TFakeAdmin00000000000000000001"


def _create_invite(client, db_session, *, max_uses=1, expires_in_hours=72):
    seed_membership(db_session, ADMIN_WALLET, role=MemberRole.ADMIN)
    token = login(client, ADMIN_WALLET)
    r = client.post(
        "/admin/invites",
        json={"max_uses": max_uses, "expires_in_hours": expires_in_hours},
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_valid_invite_redemption_creates_active_membership(client, db_session):
    invite = _create_invite(client, db_session)
    wallet = "TFakeMember00000000000000000001"
    token = login(client, wallet)
    r = client.post("/invites/redeem", json={"code": invite["code"]}, headers=auth_headers(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "member"
    assert body["status"] == "active"


def test_random_wallet_without_invite_denied_marketplace_access(client, db_session):
    wallet = "TFakeNoInvite0000000000000001"
    token = login(client, wallet)
    r = client.get("/orders/", headers=auth_headers(token))
    assert r.status_code == 403


def test_expired_invite_denied(client, db_session):
    invite = _create_invite(client, db_session, expires_in_hours=1)
    inv = db_session.query(InviteDB).filter(InviteDB.id == invite["id"]).first()
    from datetime import datetime, timedelta, timezone

    inv.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.commit()

    wallet = "TFakeExpiredInvite000000000001"
    token = login(client, wallet)
    r = client.post("/invites/redeem", json={"code": invite["code"]}, headers=auth_headers(token))
    assert r.status_code == 400


def test_revoked_invite_denied(client, db_session):
    invite = _create_invite(client, db_session)
    admin_token = login(client, ADMIN_WALLET)
    revoke = client.post(f"/admin/invites/{invite['id']}/revoke", headers=auth_headers(admin_token))
    assert revoke.status_code == 204

    wallet = "TFakeRevokedInvite00000000001"
    token = login(client, wallet)
    r = client.post("/invites/redeem", json={"code": invite["code"]}, headers=auth_headers(token))
    assert r.status_code == 400


def test_max_use_exhausted_denied(client, db_session):
    invite = _create_invite(client, db_session, max_uses=1)
    first_wallet = "TFakeFirstRedeemer0000000001"
    second_wallet = "TFakeSecondRedeemer000000001"

    first_token = login(client, first_wallet)
    first = client.post("/invites/redeem", json={"code": invite["code"]}, headers=auth_headers(first_token))
    assert first.status_code == 200

    second_token = login(client, second_wallet)
    second = client.post("/invites/redeem", json={"code": invite["code"]}, headers=auth_headers(second_token))
    assert second.status_code == 400


def test_replayed_invite_by_same_wallet_denied(client, db_session):
    """Misma wallet intenta redimir el MISMO invite dos veces — el
    UniqueConstraint(user_id) en memberships lo bloquea aunque el invite
    tuviera cupo de sobra (max_uses alto)."""
    invite = _create_invite(client, db_session, max_uses=10)
    wallet = "TFakeReplayer00000000000000001"
    token = login(client, wallet)

    first = client.post("/invites/redeem", json={"code": invite["code"]}, headers=auth_headers(token))
    assert first.status_code == 200

    second = client.post("/invites/redeem", json={"code": invite["code"]}, headers=auth_headers(token))
    assert second.status_code == 400


def test_nonexistent_invite_code_denied(client, db_session):
    wallet = "TFakeBadCode00000000000000001"
    token = login(client, wallet)
    r = client.post("/invites/redeem", json={"code": "this-code-was-never-issued-by-anyone"}, headers=auth_headers(token))
    assert r.status_code == 404


def test_plaintext_invite_code_never_persisted(client, db_session):
    invite = _create_invite(client, db_session)
    stored = db_session.query(InviteDB).filter(InviteDB.id == invite["id"]).first()
    assert invite["code"] not in (stored.code_hash or "")
    assert stored.code_hash != invite["code"]
