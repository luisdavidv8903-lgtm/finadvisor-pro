"""
ConversationRouter (Phase 2C section 1/2) -- translates WhatsApp user intent
into calls against EXISTING Chambeando services/routers. This file must never
grow its own copy of membership, authorization, order-state, settlement, or
dispute logic -- every privileged decision below is a direct call into the
already-audited function that the HTTP API itself uses (deps.get_current_membership,
services.invites.redeem_invite_for_user, services.authorization.mask_wallet,
routers.orders.attach_order_metadata). Calling a FastAPI route function
directly (not through HTTP) is the same pattern already established by
bootstrap_admin.py's core function and Phase 2C's own E2E tests -- `Depends(...)`
default values are simply unused when real arguments are supplied.

Money-moving actions (claim, mark-paid, release, raise-dispute) are NOT
performed here -- they are on-chain wallet actions. This router's job for
those is exactly what section 4 asks: hand off a short-lived deep link to
the (mocked, in this phase) dApp/web page where the wallet actually signs.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import timeutils
from ..config import settings
from ..deps import get_current_membership
from ..models import ConversationSessionDB, ConversationState, MembershipStatus, OrderStatus, P2POrderDB
from ..schemas import OrderMetadataCreate
from ..services.authorization import mask_wallet
from ..services.invites import InviteRedemptionError, redeem_invite_for_user
from . import identity
from .adapter import MessagingAdapter, OutboundMessage

SYNTHETIC_PAYMENT_METHOD = "TEST_CUP_TRANSFER"

HELP_TEXT = (
    "Opciones:\n"
    "BUY - ver ofertas disponibles\n"
    "SELL - crear una oferta\n"
    "MY TRADES - ver tus trades activos\n"
    "HELP - este mensaje"
)


def _get_or_create_session(db: Session, whatsapp_id: str) -> ConversationSessionDB:
    session = db.query(ConversationSessionDB).filter(ConversationSessionDB.whatsapp_id == whatsapp_id).first()
    if session is None:
        session = ConversationSessionDB(whatsapp_id=whatsapp_id, state=ConversationState.IDLE)
        db.add(session)
        db.commit()
        db.refresh(session)
    return session


def _get_context(session: ConversationSessionDB) -> dict:
    if not session.context:
        return {}
    try:
        return json.loads(session.context)
    except (json.JSONDecodeError, TypeError):
        return {}


class ConversationRouter:
    def __init__(self, adapter: MessagingAdapter, dapp_base_url: str = "https://chambeando.local/app") -> None:
        self._adapter = adapter
        self._dapp_base_url = dapp_base_url

    # --- transport-agnostic entrypoint -----------------------------------

    def handle_inbound(self, db: Session, whatsapp_id: str, raw_text: str) -> None:
        text = (raw_text or "").strip()
        upper = text.upper()
        session = _get_or_create_session(db, whatsapp_id)

        dispatch = {
            "START": self._handle_start,
            "JOIN": self._handle_join,
            "MENU": self._handle_menu,
            "HELP": lambda db, wid, s: self._send(wid, HELP_TEXT),
            "BUY": self._handle_buy,
            "SELL": self._handle_sell_start,
            "MY TRADES": self._handle_my_trades,
            "MYTRADES": self._handle_my_trades,
            "DISPUTE": self._handle_dispute_command,
        }
        if upper in dispatch:
            dispatch[upper](db, whatsapp_id, session)
            return

        state_dispatch = {
            ConversationState.AWAITING_INVITE_CODE: self._handle_invite_code_input,
            ConversationState.BUY_SELECT_OFFER: self._handle_buy_selection,
            ConversationState.SELL_AWAITING_AMOUNT: self._handle_sell_amount,
            ConversationState.SELL_AWAITING_RATE: self._handle_sell_rate,
            ConversationState.SELL_AWAITING_CONFIRM: self._handle_sell_confirm,
            ConversationState.DISPUTE_SELECT_TRADE: self._handle_dispute_trade_selection,
        }
        handler = state_dispatch.get(session.state)
        if handler is not None:
            handler(db, whatsapp_id, session, text)
            return

        self._send(whatsapp_id, "No entendi ese mensaje. Escribi MENU para ver las opciones.")

    # --- helpers ------------------------------------------------------------

    def _send(self, whatsapp_id: str, text: str, action_url: str | None = None) -> None:
        self._adapter.send(OutboundMessage(to=whatsapp_id, text=text, action_url=action_url))

    def _set_state(self, db: Session, session: ConversationSessionDB, state: ConversationState, context: dict | None = None) -> None:
        session.state = state
        if context is not None:
            session.context = json.dumps(context)
        session.updated_at = timeutils.utcnow()
        db.commit()

    def _require_linked_member(self, db: Session, whatsapp_id: str):
        """Direct reuse of deps.get_current_membership -- the SAME fail-closed
        rule the HTTP API enforces (no membership -> 403, suspended -> 403),
        just invoked as a plain function with real arguments instead of
        FastAPI dependency injection. Returns (user, membership) or None
        (having already sent the appropriate safe denial message)."""
        user = identity.get_linked_user(db, whatsapp_id)
        if user is None:
            self._send(whatsapp_id, "Todavia no vinculaste tu wallet. Escribi JOIN para empezar.")
            return None
        try:
            membership = get_current_membership(current_user=user, db=db)
        except HTTPException as exc:
            detail = (exc.detail or "").lower()
            if "suspend" in detail:
                self._send(whatsapp_id, "Tu membership esta suspendida. Contacta a un admin.")
            else:
                self._send(whatsapp_id, "Necesitas ser member para esto. Escribi JOIN para redimir un invite.")
            return None
        return user, membership

    # --- START / JOIN ---------------------------------------------------

    def _handle_start(self, db: Session, whatsapp_id: str, session: ConversationSessionDB) -> None:
        self._set_state(db, session, ConversationState.IDLE, context={})
        self._send(
            whatsapp_id,
            "Bienvenido a Chambeando. Este es un marketplace P2P SOLO por invitacion.\n"
            "Si ya tenes un codigo de invitacion, escribi JOIN.",
        )

    def _handle_join(self, db: Session, whatsapp_id: str, session: ConversationSessionDB) -> None:
        user = identity.get_linked_user(db, whatsapp_id)
        if user is None:
            token = identity.issue_link_token(db, whatsapp_id, settings.WHATSAPP_LINK_TOKEN_EXPIRE_SECONDS)
            link_url = f"{self._dapp_base_url}/link?token={token}"
            self._set_state(db, session, ConversationState.AWAITING_LINK, context={})
            self._send(
                whatsapp_id,
                "Primero necesitamos verificar tu wallet (nunca compartas tu clave privada). Abri este link para firmar:",
                action_url=link_url,
            )
            return

        self._send_post_link_status(db, whatsapp_id, session, user)

    def _send_post_link_status(self, db: Session, whatsapp_id: str, session: ConversationSessionDB, user) -> None:
        from ..models import MembershipDB

        membership = db.query(MembershipDB).filter(MembershipDB.user_id == user.id).first()
        if membership is not None and membership.status == MembershipStatus.ACTIVE:
            self._set_state(db, session, ConversationState.MENU, context={})
            self._send(whatsapp_id, "Ya sos member. Escribi MENU para ver las opciones.")
            return

        self._set_state(db, session, ConversationState.AWAITING_INVITE_CODE, context={})
        self._send(whatsapp_id, "Wallet verificada. Envia tu codigo de invitacion.")

    def notify_link_complete(self, db: Session, whatsapp_id: str) -> None:
        """Called by POST /whatsapp/link right after the wallet-signing
        handoff succeeds (section 4: "...backend verifies -> WhatsApp
        receives status update") -- proactively sends the same status the
        user would get by typing JOIN again, so they never have to guess
        that linking worked."""
        user = identity.get_linked_user(db, whatsapp_id)
        if user is None:
            return
        session = _get_or_create_session(db, whatsapp_id)
        self._send_post_link_status(db, whatsapp_id, session, user)

    def _handle_invite_code_input(self, db: Session, whatsapp_id: str, session: ConversationSessionDB, text: str) -> None:
        user = identity.get_linked_user(db, whatsapp_id)
        if user is None:
            self._set_state(db, session, ConversationState.IDLE, context={})
            self._send(whatsapp_id, "Se perdio la vinculacion de wallet. Escribi JOIN de nuevo.")
            return
        try:
            redeem_invite_for_user(db, text, user)
        except InviteRedemptionError:
            self._send(whatsapp_id, "Codigo invalido, expirado o agotado. Intenta de nuevo o pedi otro invite.")
            return
        self._set_state(db, session, ConversationState.MENU, context={})
        self._send(whatsapp_id, "Listo, ya sos member. Escribi MENU para ver las opciones.")

    def _handle_menu(self, db: Session, whatsapp_id: str, session: ConversationSessionDB) -> None:
        if self._require_linked_member(db, whatsapp_id) is None:
            return
        self._set_state(db, session, ConversationState.MENU, context={})
        self._send(whatsapp_id, HELP_TEXT)

    # --- BUY --------------------------------------------------------------

    def _handle_buy(self, db: Session, whatsapp_id: str, session: ConversationSessionDB) -> None:
        result = self._require_linked_member(db, whatsapp_id)
        if result is None:
            return
        user, _membership = result

        offers = (
            db.query(P2POrderDB)
            .filter(P2POrderDB.onchain_status == OrderStatus.OPEN, P2POrderDB.seller_wallet != user.wallet_address)
            .order_by(P2POrderDB.id)
            .limit(5)
            .all()
        )
        if not offers:
            self._send(whatsapp_id, "No hay ofertas disponibles ahora mismo. Escribi MENU para volver.")
            return

        offer_map: dict[str, int] = {}
        lines = ["Ofertas disponibles:"]
        for i, o in enumerate(offers, start=1):
            offer_map[str(i)] = o.id
            fiat = f" -- {o.fiat_amount} {o.fiat_currency}" if o.fiat_amount else ""
            lines.append(f"{i}. {o.crypto_amount} cripto -- vendedor {mask_wallet(o.seller_wallet)}{fiat}")
        lines.append("Responde con el numero de la oferta que te interesa.")

        self._set_state(db, session, ConversationState.BUY_SELECT_OFFER, context={"offer_map": offer_map})
        self._send(whatsapp_id, "\n".join(lines))

    def _handle_buy_selection(self, db: Session, whatsapp_id: str, session: ConversationSessionDB, text: str) -> None:
        offer_map = _get_context(session).get("offer_map", {})
        order_id = offer_map.get(text.strip())
        if order_id is None:
            self._send(whatsapp_id, "Numero invalido. Escribi BUY de nuevo para ver las ofertas.")
            return
        order = db.query(P2POrderDB).filter(P2POrderDB.id == order_id).first()
        if order is None or order.onchain_status != OrderStatus.OPEN:
            self._set_state(db, session, ConversationState.MENU, context={})
            self._send(whatsapp_id, "Esa oferta ya no esta disponible. Escribi BUY para ver otras.")
            return

        fiat = f"Fiat: {order.fiat_amount} {order.fiat_currency}\n" if order.fiat_amount else ""
        summary = (
            f"Oferta seleccionada (ref #{order.id}):\n"
            f"Monto: {order.crypto_amount}\n"
            f"Vendedor: {mask_wallet(order.seller_wallet)}\n"
            f"{fiat}"
            "Para tomar esta oferta necesitas firmar con tu wallet. Abri este link:"
        )
        link_url = f"{self._dapp_base_url}/claim?order_id={order.id}"
        self._set_state(db, session, ConversationState.MENU, context={})
        self._send(whatsapp_id, summary, action_url=link_url)

    # --- SELL ---------------------------------------------------------------

    def _handle_sell_start(self, db: Session, whatsapp_id: str, session: ConversationSessionDB) -> None:
        if self._require_linked_member(db, whatsapp_id) is None:
            return
        self._set_state(db, session, ConversationState.SELL_AWAITING_AMOUNT, context={})
        self._send(whatsapp_id, "Cuanto cripto queres vender? Envia solo el numero (ej: 100).")

    def _handle_sell_amount(self, db: Session, whatsapp_id: str, session: ConversationSessionDB, text: str) -> None:
        amount = self._parse_positive_decimal(text)
        if amount is None:
            self._send(whatsapp_id, "Monto invalido. Envia solo un numero, ej: 100")
            return
        context = _get_context(session)
        context["amount"] = str(amount)
        self._set_state(db, session, ConversationState.SELL_AWAITING_RATE, context=context)
        self._send(whatsapp_id, f"Cuanto fiat (CUP) por esos {amount}? Envia solo el numero.")

    def _handle_sell_rate(self, db: Session, whatsapp_id: str, session: ConversationSessionDB, text: str) -> None:
        fiat_amount = self._parse_positive_decimal(text)
        if fiat_amount is None:
            self._send(whatsapp_id, "Monto invalido. Envia solo un numero, ej: 25000")
            return
        context = _get_context(session)
        context["fiat_amount"] = str(fiat_amount)
        self._set_state(db, session, ConversationState.SELL_AWAITING_CONFIRM, context=context)
        self._send(
            whatsapp_id,
            f"Confirmar oferta: {context['amount']} cripto por {fiat_amount} CUP ({SYNTHETIC_PAYMENT_METHOD}).\n"
            "Responde SI para continuar o NO para cancelar.",
        )

    def _handle_sell_confirm(self, db: Session, whatsapp_id: str, session: ConversationSessionDB, text: str) -> None:
        if text.strip().upper() != "SI":
            self._set_state(db, session, ConversationState.MENU, context={})
            self._send(whatsapp_id, "Oferta cancelada. Escribi MENU para volver.")
            return
        context = _get_context(session)
        link_url = f"{self._dapp_base_url}/create-order?amount={context.get('amount')}&fiat={context.get('fiat_amount')}"
        self._set_state(db, session, ConversationState.MENU, context={})
        self._send(
            whatsapp_id,
            "Para publicar tu oferta necesitas firmar la transaccion de escrow con tu wallet. Abri este link:",
            action_url=link_url,
        )

    @staticmethod
    def _parse_positive_decimal(text: str) -> Decimal | None:
        try:
            value = Decimal(text.strip())
        except (InvalidOperation, ValueError):
            return None
        return value if value > 0 else None

    # --- MY TRADES / DISPUTE -------------------------------------------------

    def _handle_my_trades(self, db: Session, whatsapp_id: str, session: ConversationSessionDB) -> None:
        result = self._require_linked_member(db, whatsapp_id)
        if result is None:
            return
        user, _membership = result

        orders = (
            db.query(P2POrderDB)
            .filter(or_(P2POrderDB.seller_wallet == user.wallet_address, P2POrderDB.buyer_wallet == user.wallet_address))
            .order_by(P2POrderDB.id.desc())
            .limit(10)
            .all()
        )
        if not orders:
            self._send(whatsapp_id, "No tenes trades todavia. Escribi BUY o SELL para empezar.")
            return

        trade_map: dict[str, int] = {}
        lines = ["Tus trades:"]
        for i, o in enumerate(orders, start=1):
            trade_map[str(i)] = o.id
            role = "vendedor" if o.seller_wallet == user.wallet_address else "comprador"
            lines.append(f"{i}. {o.onchain_status.value.upper()} -- {role} -- {o.crypto_amount} cripto")
        lines.append("Escribi DISPUTE para abrir una disputa sobre uno de estos trades.")

        self._set_state(db, session, ConversationState.MENU, context={"trade_map": trade_map})
        self._send(whatsapp_id, "\n".join(lines))

    def _handle_dispute_command(self, db: Session, whatsapp_id: str, session: ConversationSessionDB) -> None:
        if self._require_linked_member(db, whatsapp_id) is None:
            return
        context = _get_context(session)
        if not context.get("trade_map"):
            self._send(whatsapp_id, "Primero escribi MY TRADES para elegir un trade.")
            return
        self._set_state(db, session, ConversationState.DISPUTE_SELECT_TRADE, context=context)
        self._send(whatsapp_id, "Que trade queres disputar? Responde con el numero.")

    def _handle_dispute_trade_selection(self, db: Session, whatsapp_id: str, session: ConversationSessionDB, text: str) -> None:
        trade_map = _get_context(session).get("trade_map", {})
        order_id = trade_map.get(text.strip())
        if order_id is None:
            self._set_state(db, session, ConversationState.MENU, context={})
            self._send(whatsapp_id, "Numero invalido. Escribi MY TRADES de nuevo.")
            return
        order = db.query(P2POrderDB).filter(P2POrderDB.id == order_id).first()
        if order is None or order.onchain_status not in (OrderStatus.PAID, OrderStatus.DISPUTED):
            self._set_state(db, session, ConversationState.MENU, context={})
            self._send(whatsapp_id, "Ese trade no se puede disputar en su estado actual.")
            return

        link_url = f"{self._dapp_base_url}/dispute?order_id={order.id}"
        self._set_state(db, session, ConversationState.MENU, context={})
        self._send(
            whatsapp_id,
            "Para abrir la disputa y enviar evidencia usa la pagina segura -- nunca compartas evidencia por este chat.",
            action_url=link_url,
        )


def create_order_metadata_via_dapp(db: Session, membership, user, payload: OrderMetadataCreate):
    """Thin passthrough used by the (mocked) dApp handoff after an on-chain
    createOrder confirms -- calls the EXISTING attach_order_metadata route
    function directly (same reuse pattern as get_current_membership above),
    never a WhatsApp-specific reimplementation of settlement-snapshot logic."""
    from ..routers.orders import attach_order_metadata

    return attach_order_metadata(payload, db=db, membership=membership, current_user=user)
