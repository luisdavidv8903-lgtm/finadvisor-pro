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

from sqlalchemy.orm import Session

from .. import timeutils
from ..models import UserDB, WhatsAppLinkDB


class LinkTokenError(Exception):
    def __init__(self, reason: str, status_code: int = 400) -> None:
        self.reason = reason
        self.status_code = status_code
        super().__init__(reason)


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


def issue_link_token(db: Session, whatsapp_id: str, ttl_seconds: int) -> str:
    """Short-lived, single-use, HASHED (never persisted in plaintext -- same
    discipline as InviteDB.code_hash) token for the deep-link handoff to the
    wallet-signing page. The plaintext token is returned ONCE, to be embedded
    in the deep link the user is sent -- never logged, never re-derivable
    from the DB."""
    link = get_or_create_link(db, whatsapp_id)
    token = secrets.token_urlsafe(24)
    link.link_token_hash = _hash_token(token)
    link.link_token_expires_at = timeutils.utcnow() + timedelta(seconds=ttl_seconds)
    db.commit()
    return token


def consume_link_token(db: Session, token: str, user: UserDB) -> WhatsAppLinkDB:
    """Called ONLY after the caller already authenticated as `user` through
    the normal /auth/nonce + /auth/verify wallet-signature flow (see
    routers/whatsapp.py's POST /whatsapp/link) -- this function itself proves
    nothing about wallet ownership; it just binds an already-proven identity
    to the WhatsApp id that issued the token."""
    link = db.query(WhatsAppLinkDB).filter(WhatsAppLinkDB.link_token_hash == _hash_token(token)).first()
    if link is None:
        raise LinkTokenError("invalid link token")
    if link.link_token_expires_at is None or timeutils.ensure_utc(link.link_token_expires_at) < timeutils.utcnow():
        raise LinkTokenError("link token expired")
    if link.user_id is not None and link.user_id != user.id:
        raise LinkTokenError("this WhatsApp identity is already linked to a different wallet")

    link.user_id = user.id
    link.linked_at = timeutils.utcnow()
    link.link_token_hash = None  # single-use
    link.link_token_expires_at = None
    db.commit()
    db.refresh(link)
    return link


def get_linked_user(db: Session, whatsapp_id: str) -> UserDB | None:
    """None if this WhatsApp identity has never linked a wallet -- callers
    must treat that as "no wallet", never fall back to any other signal."""
    link = db.query(WhatsAppLinkDB).filter(WhatsAppLinkDB.whatsapp_id == whatsapp_id).first()
    if link is None or link.user_id is None:
        return None
    return db.query(UserDB).filter(UserDB.id == link.user_id).first()
