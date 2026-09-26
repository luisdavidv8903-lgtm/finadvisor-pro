"""Real independent connections; deterministic overlap before token writes."""
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.database import Base
from backend.messaging.identity import LinkTokenError, consume_link_token, issue_link_token
from backend.models import SecurityEventDB, SecurityEventType, UserDB, WhatsAppLinkDB
from backend.routers import whatsapp
from backend.schemas import WhatsAppLinkRequest


@pytest.fixture(params=["sqlite", "postgres"])
def link_engine(request, tmp_path):
    if request.param == "sqlite":
        engine = create_engine(f"sqlite:///{tmp_path / 'links.db'}", connect_args={"check_same_thread": False, "timeout": 15})
        Base.metadata.create_all(engine)
        yield engine
        engine.dispose()
        return
    url = os.environ.get("POSTGRES_TEST_DATABASE_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_DATABASE_URL not configured")
    # Isolated disposable schema; never truncate existing application tables.
    schema = "link_audit_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def seed(engine, network="tron"):
    with Session(engine) as db:
        users = [UserDB(wallet_address=f"synthetic-wallet-{i}", network_id=network) for i in range(2)]
        db.add_all(users)
        db.commit()
        ids = [u.id for u in users]
        token = issue_link_token(db, "synthetic-wa", 600, network_id=network)
    return ids, token


@pytest.mark.parametrize("network", ["tron", "evm"])
def test_two_connections_exactly_one_winner(link_engine, network):
    ids, token = seed(link_engine, network)
    barrier = threading.Barrier(2, timeout=10)
    connections = set()

    def after_select(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("SELECT") and "WHERE whatsapp_links.link_token_hash =" in statement:
            connections.add(id(conn.connection.driver_connection))
            barrier.wait()

    event.listen(link_engine, "after_cursor_execute", after_select)
    def consume(user_id):
        with Session(link_engine) as db:
            user = db.get(UserDB, user_id)
            try:
                consume_link_token(db, token, user, network_id=network)
                db.commit()
                return "success", user_id
            except LinkTokenError:
                db.rollback()
                return "rejected", user_id
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(consume, ids))
    finally:
        event.remove(link_engine, "after_cursor_execute", after_select)
    assert len(connections) == 2
    assert sorted(status for status, _ in results) == ["rejected", "success"]
    winner = next(uid for status, uid in results if status == "success")
    with Session(link_engine) as db:
        link = db.query(WhatsAppLinkDB).one()
        assert link.user_id == winner
        assert link.link_token_hash is None
        assert link.link_token_expires_at is None
        assert link.link_token_network_id is None


def test_audit_insert_failure_rolls_back_and_token_can_retry(link_engine, monkeypatch):
    ids, token = seed(link_engine)
    original_logger = whatsapp.log_security_event
    notifications = []
    class Router:
        def notify_link_complete(self, db, whatsapp_id):
            notifications.append(whatsapp_id)
    monkeypatch.setattr(whatsapp, "get_conversation_router", lambda: Router())

    def broken_logger(db, **kwargs):
        # Real NOT NULL violation during INSERT, not a mocked Python error.
        db.execute(SecurityEventDB.__table__.insert().values(action=None))
    monkeypatch.setattr(whatsapp, "log_security_event", broken_logger)
    payload = WhatsAppLinkRequest(link_token=token, network_id="tron")
    with Session(link_engine) as db:
        before = db.query(WhatsAppLinkDB).one()
        original = (before.link_token_hash, before.link_token_expires_at, before.link_token_network_id)
        with pytest.raises(IntegrityError):
            whatsapp.link_whatsapp_identity(payload, db, db.get(UserDB, ids[0]), "tron")
    with Session(link_engine) as db:
        link = db.query(WhatsAppLinkDB).one()
        assert link.user_id is None and link.linked_at is None
        assert (link.link_token_hash, link.link_token_expires_at, link.link_token_network_id) == original
        assert db.query(SecurityEventDB).count() == 0
        assert notifications == []
        monkeypatch.setattr(whatsapp, "log_security_event", original_logger)
        whatsapp.link_whatsapp_identity(payload, db, db.get(UserDB, ids[0]), "tron")
    with Session(link_engine) as db:
        link = db.query(WhatsAppLinkDB).one()
        assert link.user_id == ids[0] and link.link_token_hash is None
        audit = db.query(SecurityEventDB).one()
        assert audit.action == SecurityEventType.WHATSAPP_ACCOUNT_LINKED
        assert audit.actor_user_id == ids[0] and audit.target_id == str(link.id)
    assert notifications == ["synthetic-wa"]


def test_stale_issuer_cannot_resurrect_consumed_token(link_engine, monkeypatch):
    from backend.messaging import identity
    ids, token = seed(link_engine)
    original_get = identity.get_or_create_link
    def stale_read_then_consume(db, whatsapp_id):
        link = original_get(db, whatsapp_id)
        with Session(link_engine) as other:
            consume_link_token(other, token, other.get(UserDB, ids[0]), "tron")
            other.commit()
        return link
    monkeypatch.setattr(identity, "get_or_create_link", stale_read_then_consume)
    with Session(link_engine) as db:
        with pytest.raises(LinkTokenError, match="concurrently"):
            issue_link_token(db, "synthetic-wa", 600, "tron")
        db.rollback()
    with Session(link_engine) as db:
        link = db.query(WhatsAppLinkDB).one()
        assert link.user_id == ids[0] and link.link_token_hash is None


def test_explicit_reissue_rotates_token_but_cannot_replace_owner(link_engine):
    ids, old = seed(link_engine)
    with Session(link_engine) as db:
        new = issue_link_token(db, "synthetic-wa", 600, "tron")
        with pytest.raises(LinkTokenError):
            consume_link_token(db, old, db.get(UserDB, ids[0]), "tron")
        consume_link_token(db, new, db.get(UserDB, ids[0]), "tron")
        db.commit()
        retry = issue_link_token(db, "synthetic-wa", 600, "tron")
        with pytest.raises(LinkTokenError, match="different wallet"):
            consume_link_token(db, retry, db.get(UserDB, ids[1]), "tron")
        assert db.query(WhatsAppLinkDB).one().user_id == ids[0]


@pytest.mark.parametrize("changed", ["hash", "expiry", "network", "owner"])
def test_predicates_rechecked_after_stale_read(link_engine, monkeypatch, changed):
    from datetime import timedelta
    from backend import timeutils
    ids, token = seed(link_engine)
    original_ensure = timeutils.ensure_utc
    values = {
        "hash": {"link_token_hash": "replacement-hash"},
        "expiry": {"link_token_expires_at": timeutils.utcnow() - timedelta(seconds=5)},
        "network": {"link_token_network_id": "evm"},
        "owner": {"user_id": ids[1]},
    }[changed]
    def change_after_read(value):
        # The consumer already read the old row. Commit a competing change
        # on another real connection before the conditional write executes.
        monkeypatch.setattr(timeutils, "ensure_utc", original_ensure)
        with Session(link_engine) as other:
            other.execute(update(WhatsAppLinkDB).values(**values))
            other.commit()
        return original_ensure(value)
    monkeypatch.setattr(timeutils, "ensure_utc", change_after_read)
    with Session(link_engine) as db:
        with pytest.raises(LinkTokenError):
            consume_link_token(db, token, db.get(UserDB, ids[0]), "tron")
        db.rollback()
    with Session(link_engine) as db:
        link = db.query(WhatsAppLinkDB).one()
        assert link.user_id == (ids[1] if changed == "owner" else None)
        assert link.link_token_hash is not None
        assert link.linked_at is None


def test_http_audit_failure_preserves_retry(client, db_session, monkeypatch):
    from .conftest import login, auth_headers
    jwt = login(client, "0xAtomicHttpWallet0000000000000001")
    token = issue_link_token(db_session, "http-atomic-wa", 600, "tron")
    logger = whatsapp.log_security_event
    def broken(db, **kwargs):
        db.execute(SecurityEventDB.__table__.insert().values(action=None))
    monkeypatch.setattr(whatsapp, "log_security_event", broken)
    payload = {"link_token": token, "network_id": "tron"}
    with pytest.raises(IntegrityError):
        client.post("/whatsapp/link", json=payload, headers=auth_headers(jwt))
    with Session(db_session.get_bind()) as observer:
        link = observer.query(WhatsAppLinkDB).filter_by(whatsapp_id="http-atomic-wa").one()
        assert link.user_id is None and link.link_token_hash is not None
        assert observer.query(SecurityEventDB).filter_by(action=SecurityEventType.WHATSAPP_ACCOUNT_LINKED).count() == 0
    monkeypatch.setattr(whatsapp, "log_security_event", logger)
    response = client.post("/whatsapp/link", json=payload, headers=auth_headers(jwt))
    assert response.status_code == 200
