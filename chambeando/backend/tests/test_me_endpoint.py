"""Phase 2B.1 #1 — GET /me must use an explicit response_model, never a raw dict."""

import inspect

from backend.models import MemberRole
from backend.routers import users as users_router

from .conftest import auth_headers, login, seed_membership

WALLET = "TFakeMeWallet000000000000000001"


def test_me_uses_explicit_response_model():
    route = next(r for r in users_router.router.routes if getattr(r, "path", "") == "/me")
    assert route.response_model is not None
    assert route.response_model.__name__ == "MeOut"


def test_me_route_handler_does_not_return_a_raw_dict():
    source = inspect.getsource(users_router.read_own_profile)
    assert "return {" not in source  # el handler ya no construye/retorna un dict crudo


def test_me_response_has_only_whitelisted_fields(client, db_session):
    seed_membership(db_session, WALLET, role=MemberRole.MODERATOR)
    token = login(client, WALLET)
    r = client.get("/me", headers=auth_headers(token))
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"wallet_address", "alias", "is_member", "role"}
    assert body["wallet_address"] == WALLET
    assert body["role"] == "moderator"
    assert body["is_member"] is True


def test_me_for_non_member_has_no_internal_fields_leaking(client):
    token = login(client, "TFakeMeNoMembership000000000001")
    r = client.get("/me", headers=auth_headers(token))
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"wallet_address", "alias", "is_member", "role"}
    assert body["is_member"] is False
    assert body["role"] is None
    # ni id interno, ni created_at, ni ningun otro campo de UserDB/MembershipDB
    for leaked in ("id", "created_at", "user_id", "suspended_reason", "suspended_at", "joined_at"):
        assert leaked not in body
