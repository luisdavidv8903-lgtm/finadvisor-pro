"""
Phase 2B.1 #5 — invite redemption is ONE database transaction (atomic capacity
consumption + membership creation). These tests use two INDEPENDENT SQLAlchemy
engines against the SAME sqlite FILE (not the shared `db_session`/`client`
fixtures, which bind a single session) so we can control real interleaving
between two concurrent transactions — HTTP-level testing can't do that.
"""

import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models import InviteDB, MembershipDB, UserDB
from backend.services.invites import InviteRedemptionError, hash_invite_code, redeem_invite_for_user


def _engine(db_path):
    # timeout=30: busy_timeout en segundos — sin esto, sqlite3 lanza
    # "database is locked" de inmediato ante la segunda escritura concurrente,
    # en vez de esperar a que la primera transaccion termine (que es el
    # comportamiento que queremos probar: serializacion correcta, no un error
    # de infraestructura de test).
    return create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30})


def _setup(db_path, *, max_uses, n_users):
    engine = _engine(db_path)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()

    admin = UserDB(wallet_address="TFakeConcAdmin00000000000000001")
    session.add(admin)
    session.commit()

    code = "synthetic-concurrency-invite-code"
    invite = InviteDB(
        code_hash=hash_invite_code(code),
        created_by_user_id=admin.id,
        max_uses=max_uses,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    session.add(invite)
    session.commit()

    user_ids = []
    for i in range(n_users):
        u = UserDB(wallet_address=f"TFakeConcUser{i:02d}0000000000001")
        session.add(u)
        session.commit()
        user_ids.append(u.id)

    session.close()
    return code, user_ids


def _attempt_in_own_session(db_path, code, user_id, results, key, barrier=None):
    engine = _engine(db_path)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        user = session.query(UserDB).filter(UserDB.id == user_id).first()
        if barrier is not None:
            barrier.wait()
        membership = redeem_invite_for_user(session, code, user)
        results[key] = ("success", membership.id)
    except InviteRedemptionError as exc:
        results[key] = ("denied", exc.reason)
    finally:
        session.close()
        engine.dispose()


def test_two_simultaneous_attempts_for_final_invite_slot(tmp_path):
    db_path = tmp_path / "final_slot.db"
    code, user_ids = _setup(db_path, max_uses=1, n_users=2)

    results = {}
    barrier = threading.Barrier(2)
    threads = [
        threading.Thread(target=_attempt_in_own_session, args=(db_path, code, user_ids[0], results, "a", barrier)),
        threading.Thread(target=_attempt_in_own_session, args=(db_path, code, user_ids[1], results, "b", barrier)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    outcomes = [v[0] for v in results.values()]
    assert outcomes.count("success") == 1
    assert outcomes.count("denied") == 1

    verify_engine = _engine(db_path)
    VerifySession = sessionmaker(bind=verify_engine, autocommit=False, autoflush=False)
    vs = VerifySession()
    invite = vs.query(InviteDB).filter(InviteDB.code_hash == hash_invite_code(code)).first()
    assert invite.used_count == 1  # nunca 2 — el cupo nunca se excede
    assert vs.query(MembershipDB).count() == 1


def test_exhausted_invite_cannot_race_above_max_uses(tmp_path):
    db_path = tmp_path / "exhaustion_race.db"
    max_uses = 3
    code, user_ids = _setup(db_path, max_uses=max_uses, n_users=8)

    results = {}
    barrier = threading.Barrier(len(user_ids))
    threads = [
        threading.Thread(target=_attempt_in_own_session, args=(db_path, code, uid, results, f"u{i}", barrier))
        for i, uid in enumerate(user_ids)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [v for v in results.values() if v[0] == "success"]
    assert len(successes) == max_uses  # exactamente el cupo, nunca mas

    verify_engine = _engine(db_path)
    VerifySession = sessionmaker(bind=verify_engine, autocommit=False, autoflush=False)
    vs = VerifySession()
    invite = vs.query(InviteDB).filter(InviteDB.code_hash == hash_invite_code(code)).first()
    assert invite.used_count == max_uses
    assert vs.query(MembershipDB).count() == max_uses


def test_same_wallet_concurrent_redemption_does_not_double_consume_invite(tmp_path):
    """Dos hilos, LA MISMA wallet, mismo invite con cupo de sobra (max_uses=5):
    ambos pueden pasar el chequeo temprano de 'ya es member' antes de que
    ninguno haga commit, asi que la carrera real la resuelve el
    UniqueConstraint(user_id) de memberships al momento del commit. El
    perdedor debe revertir TAMBIEN su incremento de used_count — el invite
    termina con used_count == 1, no 2, aunque dos intentos "tocaron" el invite."""
    db_path = tmp_path / "same_wallet_race.db"
    code, user_ids = _setup(db_path, max_uses=5, n_users=1)
    same_user_id = user_ids[0]

    results = {}
    barrier = threading.Barrier(2)
    threads = [
        threading.Thread(target=_attempt_in_own_session, args=(db_path, code, same_user_id, results, "x", barrier)),
        threading.Thread(target=_attempt_in_own_session, args=(db_path, code, same_user_id, results, "y", barrier)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    outcomes = [v[0] for v in results.values()]
    assert outcomes.count("success") == 1
    assert outcomes.count("denied") == 1

    verify_engine = _engine(db_path)
    VerifySession = sessionmaker(bind=verify_engine, autocommit=False, autoflush=False)
    vs = VerifySession()
    invite = vs.query(InviteDB).filter(InviteDB.code_hash == hash_invite_code(code)).first()
    assert invite.used_count == 1  # el perdedor NO dejo su incremento consumido
    assert vs.query(MembershipDB).filter(MembershipDB.user_id == same_user_id).count() == 1


def test_membership_creation_failure_does_not_consume_invite_sequential(tmp_path):
    """Version determinista (sin threads) del mismo principio: si una wallet ya
    es member, el intento de redencion ni siquiera toca el invite — used_count
    queda exactamente donde estaba."""
    db_path = tmp_path / "sequential_already_member.db"
    code, user_ids = _setup(db_path, max_uses=5, n_users=1)
    user_id = user_ids[0]

    engine = _engine(db_path)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    user = session.query(UserDB).filter(UserDB.id == user_id).first()

    first = redeem_invite_for_user(session, code, user)
    assert first is not None
    invite_after_first = session.query(InviteDB).filter(InviteDB.code_hash == hash_invite_code(code)).first()
    assert invite_after_first.used_count == 1

    try:
        redeem_invite_for_user(session, code, user)
        assert False, "expected InviteRedemptionError"
    except InviteRedemptionError as exc:
        assert exc.reason == "wallet already has a membership"

    invite_after_second = session.query(InviteDB).filter(InviteDB.code_hash == hash_invite_code(code)).first()
    assert invite_after_second.used_count == 1  # sin cambios — el segundo intento nunca llego al invite
