"""
WhatsApp identity <-> Chambeando identity mapping (Phase 2C section 5).

Three DELIBERATELY separate facts, never collapsed into one:
  1. WhatsAppLinkDB.whatsapp_id exists       -> "this WhatsApp identity has
                                                 talked to the bot" (nothing more)
  2. WhatsAppLinkDB.user_id is set           -> "this WhatsApp identity proved
                                                 ownership of a wallet" (via the
                                                 EXISTING nonce/signature flow in
                                                 auth.py -- reused, not duplicated;
                                                 see consume_link_token below)
  3. a MembershipDB row exists for that user -> "that wallet redeemed an invite"
                                                 (services/invites.py -- reused,
                                                 not duplicated)

WhatsApp possession alone (fact 1) NEVER implies fact 2 or 3. Fact 2 NEVER
implies fact 3. See WHATSAPP_ARCHITECTURE.md.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from .. import timeutils
from ..chain import get_network
from ..chain.auth_networks import is_auth_only_network
from ..chain.networks import NEVER_OPERATIONAL_STATUSES
from ..config import settings
from ..models import UserDB, WhatsAppLinkDB


class LinkTokenError(Exception):
    def __init__(self, reason: str, status_code: int = 400) -> None:
        self.reason = reason
        self.status_code = status_code
        super().__init__(reason)


def _resolve_and_require_linkable_network(network_id: str | None) -> str:
    """Falls back to settings.CHAIN_ADAPTER (today's only real network) when
    the caller doesn't pass one -- backward compatible for existing callers.
    Always fails closed: an unknown network, or one that is neither
    operational NOR auth-only-allowed, never issues/consumes a token --
    network mismatch is the caller's job to check separately (see
    consume_link_token).

    Renamed from `_resolve_and_require_operational_network` (revision
    2026-09-25, multi-network auth): a network can now be valid for a
    wallet-link token for EITHER of two independent reasons --

    1. It's operational (NetworkStatus not in NEVER_OPERATIONAL_STATUSES,
       chain/networks.py) -- same check as before, unchanged.
    2. It's explicitly allowed for AUTH-ONLY use (chain/auth_networks.py) --
       e.g. "evm" (a chain-FAMILY auth identity, not any specific EVM chain
       like bsc -- see that module's docstring for why), which has no entry
       at all in this operational registry (no EvmEscrowAdapter, no RPC, no
       contract) but can still issue/consume a wallet-link token, because
       linking a WhatsApp identity to a wallet is an AUTH fact, not an
       operational/escrow one. Checked FIRST and short-circuits: an
       auth-only network never needs to also satisfy NetworkStatus, by
       design -- the two gates are independent, neither implies the other.

    Uses the SAME NEVER_OPERATIONAL_STATUSES tuple that
    chain/__init__.py's build_adapter_for_network() gates on for path 1 -- a
    prior version of this function had its own copy of the check (just
    `status == DISABLED`) that drifted out of sync when NetworkStatus.HISTORICAL
    was added, letting a HISTORICAL network (e.g. Algorand) issue/consume a
    wallet-link token even though it can never have a working adapter. One
    shared constant means the two gates cannot diverge again."""
    resolved = network_id or settings.CHAIN_ADAPTER
    if is_auth_only_network(resolved):
        return resolved
    try:
        network = get_network(resolved)
    except ValueError:
        raise LinkTokenError(f"network_id desconocido: {resolved!r}") from None
    if network.status in NEVER_OPERATIONAL_STATUSES:
        raise LinkTokenError(f"network {resolved!r} esta {network.status.value}, no puede usarse para wallet-link")
    return resolved


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def get_or_create_link(db: Session, whatsapp_id: str) -> WhatsAppLinkDB:
    link = db.query(WhatsAppLinkDB).filter(WhatsAppLinkDB.whatsapp_id == whatsapp_id).first()
    if link is None:
        link = WhatsAppLinkDB(whatsapp_id=whatsapp_id)
        db.add(link)
        db.commit()
        db.refresh(link)
    return link


def issue_link_token(db: Session, whatsapp_id: str, ttl_seconds: int, network_id: str | None = None) -> str:
    """Short-lived, single-use, HASHED (never persisted in plaintext -- same
    discipline as InviteDB.code_hash) token for the deep-link handoff to the
    wallet-signing page. The plaintext token is returned ONCE, to be embedded
    in the deep link the user is sent -- never logged, never re-derivable
    from the DB.

    `network_id` (multichain refactor): bound into the row alongside the
    hash, so the deep link's `&network=` is a SERVER-issued fact, not a free
    query param -- consume_link_token() rejects any mismatch. Defaults to
    settings.CHAIN_ADAPTER (today's only real network) for callers that don't
    pass one yet. Fails closed (LinkTokenError) for an unknown/DISABLED
    network -- no token is issued for a network that can't operate."""
    resolved_network_id = _resolve_and_require_linkable_network(network_id)
    link = get_or_create_link(db, whatsapp_id)
    token = secrets.token_urlsafe(24)
    # Compare-and-swap: a concurrent consume/reissue must not be overwritten
    # by this session's stale row. A later, deliberate issuance still rotates
    # the outstanding token, but never changes an existing wallet owner.
    result = db.execute(
        update(WhatsAppLinkDB)
        .where(
            WhatsAppLinkDB.id == link.id,
            WhatsAppLinkDB.link_token_hash == link.link_token_hash,
            WhatsAppLinkDB.link_token_expires_at == link.link_token_expires_at,
            WhatsAppLinkDB.link_token_network_id == link.link_token_network_id,
            WhatsAppLinkDB.user_id == link.user_id,
        )
        .values(
            link_token_hash=_hash_token(token),
            link_token_expires_at=timeutils.utcnow() + timedelta(seconds=ttl_seconds),
            link_token_network_id=resolved_network_id,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise LinkTokenError("link changed concurrently; retry token issuance")
    db.commit()
    db.refresh(link)
    return token


def consume_link_token(db: Session, token: str, user: UserDB, network_id: str | None = None) -> WhatsAppLinkDB:
    """Called ONLY after the caller already authenticated as `user` through
    the normal /auth/nonce + /auth/verify wallet-signature flow (see
    routers/whatsapp.py's POST /whatsapp/link) -- this function itself proves
    nothing about wallet ownership; it just binds an already-proven identity
    to the WhatsApp id that issued the token.

    `network_id` (multichain refactor): when passed, must match the network
    the token was ISSUED for (link.link_token_network_id) -- a caller (e.g.
    the wallet-signing page) cannot swap which network a token settles by
    sending a different value than what the deep link originally carried.
    Optional only for backward compatibility with callers that predate
    per-network tokens (network_id defaults to None on both the column and
    this parameter, so an unbound token with no network_id passed still
    works exactly as before)."""
    link = db.query(WhatsAppLinkDB).filter(WhatsAppLinkDB.link_token_hash == _hash_token(token)).first()
    if link is None:
        raise LinkTokenError("invalid link token")
    if link.link_token_expires_at is None or timeutils.ensure_utc(link.link_token_expires_at) < timeutils.utcnow():
        raise LinkTokenError("link token expired")
    if network_id is not None:
        if link.link_token_network_id != network_id:
            raise LinkTokenError("network mismatch: this link was issued for a different network")
        _resolve_and_require_linkable_network(network_id)  # re-check fail-closed: config may have changed since issuance
    if link.user_id is not None and link.user_id != user.id:
        raise LinkTokenError("this WhatsApp identity is already linked to a different wallet")

    # The caller owns commit/rollback, including the security audit event.
    # All authorization predicates are repeated at the write, after any wait
    # for another transaction's lock. Exactly one consumer can clear the hash.
    result = db.execute(
        update(WhatsAppLinkDB)
        .where(
            WhatsAppLinkDB.id == link.id,
            WhatsAppLinkDB.link_token_hash == _hash_token(token),
            WhatsAppLinkDB.link_token_expires_at.is_not(None),
            WhatsAppLinkDB.link_token_expires_at >= timeutils.utcnow(),
            WhatsAppLinkDB.link_token_network_id == link.link_token_network_id,
            or_(WhatsAppLinkDB.user_id.is_(None), WhatsAppLinkDB.user_id == user.id),
        )
        .values(
            user_id=user.id,
            linked_at=timeutils.utcnow(),
            link_token_hash=None,
            link_token_expires_at=None,
            link_token_network_id=None,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise LinkTokenError("link token no longer valid or already consumed")
    db.refresh(link)
    return link


def get_linked_user(db: Session, whatsapp_id: str) -> UserDB | None:
    """None if this WhatsApp identity has never linked a wallet -- callers
    must treat that as "no wallet", never fall back to any other signal."""
    link = db.query(WhatsAppLinkDB).filter(WhatsAppLinkDB.whatsapp_id == whatsapp_id).first()
    if link is None or link.user_id is None:
        return None
    return db.query(UserDB).filter(UserDB.id == link.user_id).first()
