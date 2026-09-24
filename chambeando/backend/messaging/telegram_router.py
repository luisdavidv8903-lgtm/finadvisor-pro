"""
TelegramConversationRouter -- the Telegram counterpart of
messaging/router.py's ConversationRouter. Translates Telegram text commands
into calls against services/telegram_offers.py and telegram_identity.py --
never a second copy of matching/moderation/audit logic (same rule
router.py's own docstring states for the WhatsApp flow).

No wallet-signing handoff exists here at all (V1 has no custody/escrow), so
there is no equivalent of _send_handoff/dapp_base_url.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from .. import timeutils
from ..models import ChannelIdentityDB, MemberRole, OfferSide, OfferStatus, P2POfferDB, TelegramConversationState, TelegramMembershipDB, TelegramSessionDB
from ..services.telegram_offers import OfferError, allowed_transitions, create_offer, list_all_open_offers, list_open_offers, match_offer, transition_offer_status
from . import telegram_identity
from .adapter import MessagingAdapter, OutboundMessage

MENU_TEXT = (
    "MENU PRINCIPAL\n\n"
    "1. Comprar USDT\n"
    "2. Vender USDT\n"
    "3. Ver ofertas disponibles\n"
    "4. Mis ofertas\n"
    "5. Buscar match\n"
    "6. Ayuda\n"
    "0. Volver\n\n"
    "Responde con el numero de la opcion."
)

HELP_TEXT = (
    "Ayuda:\n"
    "1/2. Comprar o vender -- publica una oferta (monto, moneda, tasa, forma de pago, ubicacion).\n"
    "3. Ver ofertas disponibles -- lista las ofertas abiertas del mercado.\n"
    "4. Mis ofertas -- revisa y actualiza el estado de tus ofertas.\n"
    "5. Buscar match -- busca una oferta contraria a la tuya.\n\n"
    "En cualquier menu, 0 vuelve al menu principal.\n"
    "Tambien funcionan los comandos de texto: COMPRO, VENDO, MATCH, MIS OFERTAS, MENU."
)

# V1 self-service transitions only -- DISPUTED stays admin-only (no chat UX defined for it yet).
_ACTION_LABELS = {
    OfferStatus.PAYMENT_PENDING: "Marcar como pago pendiente",
    OfferStatus.COMPLETED: "Marcar como completada",
}


def _get_or_create_session(db: Session, identity: ChannelIdentityDB) -> TelegramSessionDB:
    session = db.query(TelegramSessionDB).filter(TelegramSessionDB.channel_identity_id == identity.id).first()
    if session is None:
        session = TelegramSessionDB(channel_identity_id=identity.id, state=TelegramConversationState.IDLE)
        db.add(session)
        db.commit()
        db.refresh(session)
    return session


def _get_context(session: TelegramSessionDB) -> dict:
    if not session.context:
        return {}
    try:
        return json.loads(session.context)
    except (json.JSONDecodeError, TypeError):
        return {}


class TelegramConversationRouter:
    def __init__(self, adapter: MessagingAdapter) -> None:
        self._adapter = adapter

    # --- transport-agnostic entrypoint -----------------------------------

    def handle_inbound(self, db: Session, channel_user_id: str, raw_text: str) -> None:
        text = (raw_text or "").strip()
        upper = text.upper()
        identity = telegram_identity.get_or_create_identity(db, channel_user_id)
        session = _get_or_create_session(db, identity)

        dispatch = {
            "/START": self._handle_start,
            "START": self._handle_start,
            "MENU": self._handle_menu,
            "HELP": lambda db, i, s: self._send(i, HELP_TEXT),
            "COMPRO": lambda db, i, s: self._handle_offer_start(db, i, s, OfferSide.COMPRO),
            "VENDO": lambda db, i, s: self._handle_offer_start(db, i, s, OfferSide.VENDO),
            "MATCH": self._handle_match_menu,
            "MIS OFERTAS": self._handle_my_offers,
            "MISOFERTAS": self._handle_my_offers,
        }
        if upper in dispatch:
            dispatch[upper](db, identity, session)
            return

        if upper.startswith("SUSPENDER ") or upper.startswith("REACTIVAR "):
            self._handle_admin_command(db, identity, upper)
            return

        state_dispatch = {
            TelegramConversationState.OFFER_AMOUNT: self._handle_offer_amount,
            TelegramConversationState.OFFER_CURRENCY: self._handle_offer_currency,
            TelegramConversationState.OFFER_RATE: self._handle_offer_rate,
            TelegramConversationState.OFFER_SETTLEMENT_METHOD: self._handle_offer_settlement_method,
            TelegramConversationState.OFFER_SETTLEMENT_LOCATION: self._handle_offer_settlement_location,
            TelegramConversationState.OFFER_CONFIRM: self._handle_offer_confirm,
            TelegramConversationState.MATCH_SELECT: self._handle_match_selection,
            TelegramConversationState.MENU: self._handle_menu_selection,
            TelegramConversationState.MY_OFFERS_SELECT: self._handle_my_offers_selection,
            TelegramConversationState.OFFER_ACTION_SELECT: self._handle_offer_action_selection,
            TelegramConversationState.CANCEL_CONFIRM: self._handle_cancel_confirm,
        }
        handler = state_dispatch.get(session.state)
        if handler is not None:
            handler(db, identity, session, text)
            return

        self._send(identity, "No entendi ese mensaje. Escribi MENU para ver las opciones.")

    # --- helpers ------------------------------------------------------------

    def _send(self, identity: ChannelIdentityDB, text: str) -> None:
        self._adapter.send(OutboundMessage(to=identity.channel_user_id, text=text))

    def _set_state(self, db: Session, session: TelegramSessionDB, state: TelegramConversationState, context: dict | None = None) -> None:
        session.state = state
        if context is not None:
            session.context = json.dumps(context)
        session.updated_at = timeutils.utcnow()
        db.commit()

    def _require_active(self, db: Session, identity: ChannelIdentityDB) -> TelegramMembershipDB | None:
        try:
            return telegram_identity.require_active_membership(db, identity)
        except telegram_identity.MembershipRejected as exc:
            if exc.reason == "suspended":
                self._send(identity, "Tu cuenta esta suspendida. Contacta a un admin.")
            else:
                self._send(identity, "Todavia no estas registrado. Escribi /START para empezar.")
            return None

    # --- onboarding -------------------------------------------------------

    def _handle_start(self, db: Session, identity: ChannelIdentityDB, session: TelegramSessionDB) -> None:
        telegram_identity.register_or_get_membership(db, identity)
        self._send(identity, "Bienvenido a Chambeando.")
        self._show_menu(db, identity, session)

    def _handle_menu(self, db: Session, identity: ChannelIdentityDB, session: TelegramSessionDB) -> None:
        if self._require_active(db, identity) is None:
            return
        self._show_menu(db, identity, session)

    def _show_menu(self, db: Session, identity: ChannelIdentityDB, session: TelegramSessionDB) -> None:
        self._set_state(db, session, TelegramConversationState.MENU, context={})
        self._send(identity, MENU_TEXT)

    def _handle_menu_selection(self, db: Session, identity: ChannelIdentityDB, session: TelegramSessionDB, text: str) -> None:
        actions = {
            "1": lambda: self._handle_offer_start(db, identity, session, OfferSide.COMPRO),
            "2": lambda: self._handle_offer_start(db, identity, session, OfferSide.VENDO),
            "3": lambda: self._handle_offers_browse(db, identity, session),
            "4": lambda: self._handle_my_offers(db, identity, session),
            "5": lambda: self._handle_match_menu(db, identity, session),
            "6": lambda: self._send(identity, HELP_TEXT),
            "0": lambda: self._show_menu(db, identity, session),
        }
        action = actions.get(text.strip())
        if action is None:
            self._send(identity, "Opcion invalida. Elegi un numero del 0 al 6.")
            return
        action()

    # --- offer creation (COMPRO / VENDO) -----------------------------------

    def _handle_offer_start(self, db: Session, identity: ChannelIdentityDB, session: TelegramSessionDB, side: OfferSide) -> None:
        if self._require_active(db, identity) is None:
            return
        self._set_state(db, session, TelegramConversationState.OFFER_AMOUNT, context={"side": side.value})
        self._send(identity, f"Monto a {side.value}? Envia solo el numero (ej: 100).")

    def _handle_offer_amount(self, db: Session, identity: ChannelIdentityDB, session: TelegramSessionDB, text: str) -> None:
        amount = self._parse_positive_decimal(text)
        if amount is None:
            self._send(identity, "Monto invalido. Envia solo un numero, ej: 100")
            return
        context = _get_context(session)
        context["amount"] = str(amount)
        self._set_state(db, session, TelegramConversationState.OFFER_CURRENCY, context=context)
        self._send(identity, "Moneda? (ej: USD, USDT, CUP)")

    def _handle_offer_currency(self, db: Session, identity: ChannelIdentityDB, session: TelegramSessionDB, text: str) -> None:
        currency = text.strip()
        if not currency:
            self._send(identity, "Moneda invalida. Intenta de nuevo, ej: USD")
            return
        context = _get_context(session)
        context["currency"] = currency
        self._set_state(db, session, TelegramConversationState.OFFER_RATE, context=context)
        self._send(identity, "Tasa? Envia solo el numero (ej: 320.5).")

    def _handle_offer_rate(self, db: Session, identity: ChannelIdentityDB, session: TelegramSessionDB, text: str) -> None:
        rate = self._parse_positive_decimal(text)
        if rate is None:
            self._send(identity, "Tasa invalida. Envia solo un numero, ej: 320.5")
            return
        context = _get_context(session)
        context["rate"] = str(rate)
        self._set_state(db, session, TelegramConversationState.OFFER_SETTLEMENT_METHOD, context=context)
        self._send(identity, "Forma de liquidacion? (ej: efectivo, zelle, transferencia)")

    def _handle_offer_settlement_method(self, db: Session, identity: ChannelIdentityDB, session: TelegramSessionDB, text: str) -> None:
        method = text.strip()
        if not method:
            self._send(identity, "Forma de liquidacion invalida. Intenta de nuevo.")
            return
        context = _get_context(session)
        context["settlement_method"] = method
        self._set_state(db, session, TelegramConversationState.OFFER_SETTLEMENT_LOCATION, context=context)
        self._send(identity, "Ubicacion? (ej: La Habana)")

    def _handle_offer_settlement_location(self, db: Session, identity: ChannelIdentityDB, session: TelegramSessionDB, text: str) -> None:
        location = text.strip()
        if not location:
            self._send(identity, "Ubicacion invalida. Intenta de nuevo.")
            return
        context = _get_context(session)
        context["settlement_location"] = location
        self._set_state(db, session, TelegramConversationState.OFFER_CONFIRM, context=context)
        self._send(
            identity,
            f"Confirmar oferta {context['side'].upper()}: {context['amount']} {context['currency']} a tasa {context['rate']}, "
            f"{context['settlement_method']} en {location}.\nResponde SI para publicar o NO para cancelar.",
        )

    def _handle_offer_confirm(self, db: Session, identity: ChannelIdentityDB, session: TelegramSessionDB, text: str) -> None:
        context = _get_context(session)
        if text.strip().upper() != "SI":
            self._set_state(db, session, TelegramConversationState.MENU, context={})
            self._send(identity, "Oferta cancelada. Escribi MENU para volver.")
            return
        try:
            offer = create_offer(
                db,
                identity,
                side=OfferSide(context["side"]),
                amount=Decimal(context["amount"]),
                currency=context["currency"],
                rate=Decimal(context["rate"]),
                settlement_method=context["settlement_method"],
                settlement_location=context["settlement_location"],
            )
        except telegram_identity.MembershipRejected:
            self._set_state(db, session, TelegramConversationState.MENU, context={})
            self._send(identity, "Tu cuenta esta suspendida. Contacta a un admin.")
            return
        self._set_state(db, session, TelegramConversationState.MENU, context={})
        self._send(identity, f"Oferta #{offer.id} publicada (OPEN). Escribi MATCH para ver ofertas contrarias.")

    # --- browse (read-only) ------------------------------------------------

    def _handle_offers_browse(self, db: Session, identity: ChannelIdentityDB, session: TelegramSessionDB) -> None:
        if self._require_active(db, identity) is None:
            return
        offers = list_all_open_offers(db)
        self._set_state(db, session, TelegramConversationState.MENU, context={})
        if not offers:
            self._send(identity, "No hay ofertas abiertas en este momento.\n\nEscribi MENU para volver.")
            return
        lines = ["OFERTAS DISPONIBLES", ""]
        for o in offers:
            lines.append(f"#{o.id} -- {o.side.value.upper()} -- {o.amount} {o.currency} @ {o.rate} -- {o.settlement_method} en {o.settlement_location}")
        lines.append("")
        lines.append("Escribi MENU para volver.")
        self._send(identity, "\n".join(lines))

    # --- MATCH ------------------------------------------------------------

    def _handle_match_menu(self, db: Session, identity: ChannelIdentityDB, session: TelegramSessionDB) -> None:
        if self._require_active(db, identity) is None:
            return

        own_open = (
            db.query(P2POfferDB)
            .filter(P2POfferDB.channel_identity_id == identity.id, P2POfferDB.status == OfferStatus.OPEN)
            .order_by(P2POfferDB.id.desc())
            .first()
        )
        if own_open is None:
            self._send(identity, "No tenes una oferta abierta para matchear. Escribi COMPRO o VENDO primero.")
            return

        opposite_side = OfferSide.VENDO if own_open.side == OfferSide.COMPRO else OfferSide.COMPRO
        candidates = list_open_offers(db, opposite_of=opposite_side, exclude_identity=identity)
        if not candidates:
            self._send(identity, "No hay ofertas contrarias disponibles ahora mismo.")
            return

        offer_map: dict[str, int] = {}
        lines = [f"Ofertas disponibles para tu oferta #{own_open.id} ({own_open.side.value}):"]
        for i, o in enumerate(candidates, start=1):
            offer_map[str(i)] = o.id
            lines.append(f"{i}. #{o.id} -- {o.amount} {o.currency} @ {o.rate} -- {o.settlement_method} en {o.settlement_location}")
        lines.append("Responde con el numero para matchear, o MENU para volver.")

        self._set_state(
            db, session, TelegramConversationState.MATCH_SELECT, context={"match_own_offer_id": own_open.id, "match_offer_map": offer_map}
        )
        self._send(identity, "\n".join(lines))

    def _handle_match_selection(self, db: Session, identity: ChannelIdentityDB, session: TelegramSessionDB, text: str) -> None:
        if text.strip() == "0":
            self._show_menu(db, identity, session)
            return
        context = _get_context(session)
        offer_map = context.get("match_offer_map", {})
        counterparty_id = offer_map.get(text.strip())
        if counterparty_id is None:
            self._set_state(db, session, TelegramConversationState.MENU, context={})
            self._send(identity, "Numero invalido. Escribi MATCH de nuevo.")
            return

        own_offer = db.query(P2POfferDB).filter(P2POfferDB.id == context.get("match_own_offer_id")).first()
        counterparty_offer = db.query(P2POfferDB).filter(P2POfferDB.id == counterparty_id).first()
        self._set_state(db, session, TelegramConversationState.MENU, context={})
        if own_offer is None or counterparty_offer is None:
            self._send(identity, "Esa oferta ya no esta disponible. Escribi MATCH de nuevo.")
            return

        try:
            match_offer(db, identity, own_offer, counterparty_offer)
        except OfferError as exc:
            self._send(identity, f"No se pudo matchear: {exc.reason}")
            return
        except telegram_identity.MembershipRejected:
            self._send(identity, "Tu cuenta esta suspendida. Contacta a un admin.")
            return

        self._send(identity, f"Match confirmado entre tu oferta #{own_offer.id} y #{counterparty_offer.id}. Estado: MATCHED.")

    # --- MIS OFERTAS --------------------------------------------------------

    def _handle_my_offers(self, db: Session, identity: ChannelIdentityDB, session: TelegramSessionDB) -> None:
        if self._require_active(db, identity) is None:
            return

        offers = (
            db.query(P2POfferDB)
            .filter(P2POfferDB.channel_identity_id == identity.id)
            .order_by(P2POfferDB.id.desc())
            .limit(10)
            .all()
        )
        if not offers:
            self._set_state(db, session, TelegramConversationState.MENU, context={})
            self._send(identity, "No tenes ofertas todavia. Escribi COMPRO o VENDO para empezar.")
            return

        offer_map: dict[str, int] = {}
        lines = ["TUS OFERTAS", ""]
        for i, o in enumerate(offers, start=1):
            offer_map[str(i)] = o.id
            lines.append(f"{i}. {o.side.value.upper()} {o.amount} {o.currency} @ {o.rate} -- {o.status.value.upper()}")
        lines.append("")
        lines.append("Responde con el numero para ver acciones, o 0 para volver.")

        self._set_state(db, session, TelegramConversationState.MY_OFFERS_SELECT, context={"my_offers_map": offer_map})
        self._send(identity, "\n".join(lines))

    def _handle_my_offers_selection(self, db: Session, identity: ChannelIdentityDB, session: TelegramSessionDB, text: str) -> None:
        choice = text.strip()
        if choice == "0":
            self._show_menu(db, identity, session)
            return

        context = _get_context(session)
        offer_id = context.get("my_offers_map", {}).get(choice)
        if offer_id is None:
            self._set_state(db, session, TelegramConversationState.MENU, context={})
            self._send(identity, "Numero invalido. Escribi MIS OFERTAS de nuevo.")
            return

        # Stale-list protection: re-fetch fresh, never trust the number blindly.
        offer = db.query(P2POfferDB).filter(P2POfferDB.id == offer_id, P2POfferDB.channel_identity_id == identity.id).first()
        if offer is None:
            self._set_state(db, session, TelegramConversationState.MENU, context={})
            self._send(identity, "Esa oferta ya no esta disponible. Escribi MIS OFERTAS de nuevo.")
            return

        self._send_offer_action_menu(db, identity, session, offer)

    def _send_offer_action_menu(self, db: Session, identity: ChannelIdentityDB, session: TelegramSessionDB, offer: P2POfferDB) -> None:
        allowed = allowed_transitions(offer.status)
        offered_statuses = [s for s in (OfferStatus.PAYMENT_PENDING, OfferStatus.COMPLETED) if s in allowed]

        action_map: dict[str, str] = {}
        lines = [f"OFERTA #{offer.id} -- {offer.side.value.upper()} {offer.amount} {offer.currency} -- {offer.status.value.upper()}", ""]
        n = 1
        for status in offered_statuses:
            action_map[str(n)] = status.value
            lines.append(f"{n}. {_ACTION_LABELS[status]}")
            n += 1
        if OfferStatus.CANCELLED in allowed:
            action_map[str(n)] = "cancel"
            lines.append(f"{n}. Cancelar oferta")
            n += 1
        if not action_map:
            lines.append("No hay acciones disponibles para esta oferta.")
        lines.append("0. Volver")

        self._set_state(
            db, session, TelegramConversationState.OFFER_ACTION_SELECT,
            context={"offer_action_offer_id": offer.id, "offer_action_map": action_map},
        )
        self._send(identity, "\n".join(lines))

    def _handle_offer_action_selection(self, db: Session, identity: ChannelIdentityDB, session: TelegramSessionDB, text: str) -> None:
        choice = text.strip()
        if choice == "0":
            self._show_menu(db, identity, session)
            return

        context = _get_context(session)
        action = context.get("offer_action_map", {}).get(choice)
        offer_id = context.get("offer_action_offer_id")
        if action is None or offer_id is None:
            self._set_state(db, session, TelegramConversationState.MENU, context={})
            self._send(identity, "Opcion invalida. Escribi MENU para volver.")
            return

        offer = db.query(P2POfferDB).filter(P2POfferDB.id == offer_id).first()
        if offer is None:
            self._set_state(db, session, TelegramConversationState.MENU, context={})
            self._send(identity, "Esa oferta ya no esta disponible. Escribi MENU para volver.")
            return

        if action == "cancel":
            self._set_state(db, session, TelegramConversationState.CANCEL_CONFIRM, context={"cancel_offer_id": offer.id})
            self._send(identity, f"Cancelar la oferta #{offer.id}?\n\n1. Si, cancelar\n2. No, volver")
            return

        try:
            updated = transition_offer_status(db, identity, offer, OfferStatus(action))
        except OfferError as exc:
            self._set_state(db, session, TelegramConversationState.MENU, context={})
            self._send(identity, f"No se pudo actualizar la oferta: {exc.reason}")
            return
        except telegram_identity.MembershipRejected:
            self._set_state(db, session, TelegramConversationState.MENU, context={})
            self._send(identity, "Tu cuenta esta suspendida. Contacta a un admin.")
            return

        self._set_state(db, session, TelegramConversationState.MENU, context={})
        self._send(identity, f"Oferta #{updated.id} actualizada a {updated.status.value.upper()}.")

    def _handle_cancel_confirm(self, db: Session, identity: ChannelIdentityDB, session: TelegramSessionDB, text: str) -> None:
        choice = text.strip().upper()
        if choice in ("2", "NO"):
            self._show_menu(db, identity, session)
            return
        if choice not in ("1", "SI"):
            self._send(identity, "Opcion invalida.\n\n1. Si, cancelar\n2. No, volver")
            return

        context = _get_context(session)
        offer_id = context.get("cancel_offer_id")
        offer = db.query(P2POfferDB).filter(P2POfferDB.id == offer_id).first() if offer_id else None
        if offer is None:
            self._set_state(db, session, TelegramConversationState.MENU, context={})
            self._send(identity, "Esa oferta ya no esta disponible. Escribi MENU para volver.")
            return

        try:
            updated = transition_offer_status(db, identity, offer, OfferStatus.CANCELLED)
        except OfferError as exc:
            self._set_state(db, session, TelegramConversationState.MENU, context={})
            self._send(identity, f"No se pudo cancelar: {exc.reason}")
            return
        except telegram_identity.MembershipRejected:
            self._set_state(db, session, TelegramConversationState.MENU, context={})
            self._send(identity, "Tu cuenta esta suspendida. Contacta a un admin.")
            return

        self._set_state(db, session, TelegramConversationState.MENU, context={})
        self._send(identity, f"Oferta #{updated.id} cancelada.")

    # --- admin moderation ---------------------------------------------------

    def _handle_admin_command(self, db: Session, identity: ChannelIdentityDB, upper: str) -> None:
        membership = telegram_identity.get_membership(db, identity)
        if membership is None or membership.role != MemberRole.ADMIN:
            self._send(identity, "Comando solo para admins.")
            return

        parts = upper.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip().isdigit():
            self._send(identity, "Uso: SUSPENDER <telegram_membership_id> o REACTIVAR <telegram_membership_id>")
            return

        target_id = int(parts[1].strip())
        target = db.query(TelegramMembershipDB).filter(TelegramMembershipDB.id == target_id).first()
        if target is None:
            self._send(identity, "Membership no encontrada.")
            return

        if parts[0] == "SUSPENDER":
            telegram_identity.suspend_membership(db, target, by_identity=identity, reason="suspended via admin command")
            self._send(identity, f"Membership #{target_id} suspendida.")
        else:
            telegram_identity.reactivate_membership(db, target, by_identity=identity)
            self._send(identity, f"Membership #{target_id} reactivada.")

    @staticmethod
    def _parse_positive_decimal(text: str) -> Decimal | None:
        try:
            value = Decimal(text.strip())
        except (InvalidOperation, ValueError):
            return None
        return value if value > 0 else None
