"""
Phase 2B.1 #2 — MODERATOR/ADMIN role alone must NEVER grant dispute-evidence
access; only an active, order-specific DisputeAssignmentDB does.
"""

from backend.models import MemberRole, P2POrderDB, SecurityEventDB, SecurityEventType

from .conftest import auth_headers, login, seed_membership, sync_indexer

SELLER = "TFakeDaSeller000000000000000001"
BUYER = "TFakeDaBuyer0000000000000000001"
ARBITER = "TFakeDaArbiter00000000000000001"
ADMIN_WALLET = "TFakeDaAdmin0000000000000000001"
MODERATOR_WALLET = "TFakeDaModerator000000000000001"
OTHER_MODERATOR_WALLET = "TFakeDaOtherModerator0000000001"


def _member(client, db_session, wallet, role=MemberRole.MEMBER):
    seed_membership(db_session, wallet, role=role)
    return login(client, wallet)


def _seed_disputed_order(db_session, fake_chain, onchain_id=1, seller=SELLER, buyer=BUYER, arbiter=ARBITER):
    fake_chain.seed_order_created(onchain_id, seller, "TOKEN", 100_000000)
    fake_chain.seed_order_claimed(onchain_id, buyer, arbiter)
    fake_chain.seed_paid(onchain_id)
    fake_chain.seed_disputed(onchain_id)
    sync_indexer(db_session, fake_chain)
    return db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == onchain_id).first()


def _submit_evidence(client, order, seller_token):
    return client.post(
        "/disputes/evidence", json={"order_id": order.id, "evidence_type": "note", "note": "synthetic evidence note"}, headers=auth_headers(seller_token)
    )


def test_unrelated_moderator_denied_by_default(client, db_session, fake_chain):
    order = _seed_disputed_order(db_session, fake_chain)
    seller_token = _member(client, db_session, SELLER)
    _submit_evidence(client, order, seller_token)

    moderator_token = _member(client, db_session, MODERATOR_WALLET, role=MemberRole.MODERATOR)
    r = client.get(f"/disputes/{order.id}/evidence", headers=auth_headers(moderator_token))
    assert r.status_code == 403


def test_unrelated_admin_denied_by_default(client, db_session, fake_chain):
    order = _seed_disputed_order(db_session, fake_chain)
    seller_token = _member(client, db_session, SELLER)
    _submit_evidence(client, order, seller_token)

    admin_token = _member(client, db_session, ADMIN_WALLET, role=MemberRole.ADMIN)
    r = client.get(f"/disputes/{order.id}/evidence", headers=auth_headers(admin_token))
    assert r.status_code == 403  # ADMIN role alone = NO evidence access


def test_admin_can_assign_a_moderator_and_the_assignment_grants_access(client, db_session, fake_chain):
    order = _seed_disputed_order(db_session, fake_chain)
    seller_token = _member(client, db_session, SELLER)
    _submit_evidence(client, order, seller_token)

    admin_user = seed_membership(db_session, ADMIN_WALLET, role=MemberRole.ADMIN)
    admin_token = login(client, ADMIN_WALLET)
    moderator_user = seed_membership(db_session, MODERATOR_WALLET, role=MemberRole.MODERATOR)
    moderator_token = login(client, MODERATOR_WALLET)

    assign = client.post(
        f"/admin/disputes/{order.id}/assignments",
        json={"assigned_user_id": moderator_user.id, "reason": "synthetic escalation review", "expires_in_hours": 24},
        headers=auth_headers(admin_token),
    )
    assert assign.status_code == 201, assign.text

    r = client.get(f"/disputes/{order.id}/evidence", headers=auth_headers(moderator_token))
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_assignment_to_one_dispute_gives_zero_access_to_another(client, db_session, fake_chain):
    order1 = _seed_disputed_order(db_session, fake_chain, onchain_id=1)
    seller_token = _member(client, db_session, SELLER)
    _submit_evidence(client, order1, seller_token)

    order2 = _seed_disputed_order(db_session, fake_chain, onchain_id=2, buyer="TFakeDaBuyer20000000000000001")
    _submit_evidence(client, order2, seller_token)

    admin_token = _member(client, db_session, ADMIN_WALLET, role=MemberRole.ADMIN)
    moderator_user = seed_membership(db_session, MODERATOR_WALLET, role=MemberRole.MODERATOR)
    moderator_token = login(client, MODERATOR_WALLET)

    client.post(
        f"/admin/disputes/{order1.id}/assignments",
        json={"assigned_user_id": moderator_user.id, "reason": "synthetic review", "expires_in_hours": 24},
        headers=auth_headers(admin_token),
    )

    allowed = client.get(f"/disputes/{order1.id}/evidence", headers=auth_headers(moderator_token))
    assert allowed.status_code == 200

    denied = client.get(f"/disputes/{order2.id}/evidence", headers=auth_headers(moderator_token))
    assert denied.status_code == 403


def test_reviewer_denied_after_expiration(client, db_session, fake_chain):
    from datetime import datetime, timedelta, timezone

    from backend.models import DisputeAssignmentDB

    order = _seed_disputed_order(db_session, fake_chain)
    seller_token = _member(client, db_session, SELLER)
    _submit_evidence(client, order, seller_token)

    admin_token = _member(client, db_session, ADMIN_WALLET, role=MemberRole.ADMIN)
    moderator_user = seed_membership(db_session, MODERATOR_WALLET, role=MemberRole.MODERATOR)
    moderator_token = login(client, MODERATOR_WALLET)

    assign = client.post(
        f"/admin/disputes/{order.id}/assignments",
        json={"assigned_user_id": moderator_user.id, "reason": "synthetic review", "expires_in_hours": 1},
        headers=auth_headers(admin_token),
    ).json()

    before_expiry = client.get(f"/disputes/{order.id}/evidence", headers=auth_headers(moderator_token))
    assert before_expiry.status_code == 200

    assignment = db_session.query(DisputeAssignmentDB).filter(DisputeAssignmentDB.id == assign["id"]).first()
    assignment.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()

    after_expiry = client.get(f"/disputes/{order.id}/evidence", headers=auth_headers(moderator_token))
    assert after_expiry.status_code == 403


def test_reviewer_denied_after_revocation(client, db_session, fake_chain):
    order = _seed_disputed_order(db_session, fake_chain)
    seller_token = _member(client, db_session, SELLER)
    _submit_evidence(client, order, seller_token)

    admin_token = _member(client, db_session, ADMIN_WALLET, role=MemberRole.ADMIN)
    moderator_user = seed_membership(db_session, MODERATOR_WALLET, role=MemberRole.MODERATOR)
    moderator_token = login(client, MODERATOR_WALLET)

    assign = client.post(
        f"/admin/disputes/{order.id}/assignments",
        json={"assigned_user_id": moderator_user.id, "reason": "synthetic review", "expires_in_hours": 24},
        headers=auth_headers(admin_token),
    ).json()

    before_revoke = client.get(f"/disputes/{order.id}/evidence", headers=auth_headers(moderator_token))
    assert before_revoke.status_code == 200

    revoke = client.post(f"/admin/disputes/assignments/{assign['id']}/revoke", headers=auth_headers(admin_token))
    assert revoke.status_code == 200

    after_revoke = client.get(f"/disputes/{order.id}/evidence", headers=auth_headers(moderator_token))
    assert after_revoke.status_code == 403


def test_assignment_creation_requires_admin_not_moderator(client, db_session, fake_chain):
    order = _seed_disputed_order(db_session, fake_chain)
    moderator_token = _member(client, db_session, MODERATOR_WALLET, role=MemberRole.MODERATOR)
    other_moderator_user = seed_membership(db_session, OTHER_MODERATOR_WALLET, role=MemberRole.MODERATOR)

    r = client.post(
        f"/admin/disputes/{order.id}/assignments",
        json={"assigned_user_id": other_moderator_user.id, "reason": "self-assign attempt", "expires_in_hours": 24},
        headers=auth_headers(moderator_token),
    )
    assert r.status_code == 403


def test_assignment_rejected_for_non_disputed_order(client, db_session, fake_chain):
    fake_chain.seed_order_created(1, SELLER, "TOKEN", 100_000000)
    fake_chain.seed_order_claimed(1, BUYER, ARBITER)
    fake_chain.seed_paid(1)  # PAID, not DISPUTED
    sync_indexer(db_session, fake_chain)
    order = db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == 1).first()

    admin_token = _member(client, db_session, ADMIN_WALLET, role=MemberRole.ADMIN)
    moderator_user = seed_membership(db_session, MODERATOR_WALLET, role=MemberRole.MODERATOR)

    r = client.post(
        f"/admin/disputes/{order.id}/assignments",
        json={"assigned_user_id": moderator_user.id, "reason": "premature assignment", "expires_in_hours": 24},
        headers=auth_headers(admin_token),
    )
    assert r.status_code == 400


def test_assignment_rejected_for_non_staff_target(client, db_session, fake_chain):
    order = _seed_disputed_order(db_session, fake_chain)
    admin_token = _member(client, db_session, ADMIN_WALLET, role=MemberRole.ADMIN)
    plain_member = seed_membership(db_session, "TFakeDaPlainMember0000000000001", role=MemberRole.MEMBER)

    r = client.post(
        f"/admin/disputes/{order.id}/assignments",
        json={"assigned_user_id": plain_member.id, "reason": "wrong target", "expires_in_hours": 24},
        headers=auth_headers(admin_token),
    )
    assert r.status_code == 400


def test_assignment_and_revocation_and_evidence_view_are_all_audited(client, db_session, fake_chain):
    order = _seed_disputed_order(db_session, fake_chain)
    seller_token = _member(client, db_session, SELLER)
    _submit_evidence(client, order, seller_token)

    admin_token = _member(client, db_session, ADMIN_WALLET, role=MemberRole.ADMIN)
    moderator_user = seed_membership(db_session, MODERATOR_WALLET, role=MemberRole.MODERATOR)
    moderator_token = login(client, MODERATOR_WALLET)

    assign = client.post(
        f"/admin/disputes/{order.id}/assignments",
        json={"assigned_user_id": moderator_user.id, "reason": "synthetic review", "expires_in_hours": 24},
        headers=auth_headers(admin_token),
    ).json()
    client.get(f"/disputes/{order.id}/evidence", headers=auth_headers(moderator_token))
    client.post(f"/admin/disputes/assignments/{assign['id']}/revoke", headers=auth_headers(admin_token))

    created = db_session.query(SecurityEventDB).filter(SecurityEventDB.action == SecurityEventType.DISPUTE_ASSIGNMENT_CREATED).all()
    viewed = db_session.query(SecurityEventDB).filter(SecurityEventDB.action == SecurityEventType.DISPUTE_EVIDENCE_VIEWED).all()
    revoked = db_session.query(SecurityEventDB).filter(SecurityEventDB.action == SecurityEventType.DISPUTE_ASSIGNMENT_REVOKED).all()
    assert len(created) == 1
    assert len(viewed) == 1
    assert len(revoked) == 1
