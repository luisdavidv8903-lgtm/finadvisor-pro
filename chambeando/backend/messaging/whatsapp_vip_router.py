"""
WhatsAppVipRouter -- guided-question offer-board flow for the WhatsApp VIP
pilot. Completely separate from messaging/router.py's ConversationRouter
(wallet/escrow flow): different session table (WhatsAppVipSessionDB),
different state enum (VipWizardState), different identity table
(ChannelIdentityDB with channel=WHATSAPP, via whatsapp_vip_identity.py),
different offer table (WhatsAppVipOfferDB, via services/whatsapp_vip_offers.py).

No wallet, no escrow, no P2POrderDB, no WALLET_HANDOFF_ENABLED reference
anywhere in this file -- see whatsapp_vip_offers.py's module docstring for
the same rule applied to the data layer.

Every outbound reply goes through the outbox (services/whatsapp_outbox.py),
never adapter.send() directly -- see _send() below.

Only OFERTA creation is a multi-step wizard. OFERTAS / MIS OFERTAS /
ME INTERESA <id> / CERRAR <id> / VIP <code> are single-shot commands.

Revision 2026-09-27 (audit finding): handle_inbound() no longer commits
ANYTHING itself, and never calls Meta. _set_state() only mutates the
session object in memory; _send() only stages a PENDING outbox row via
services.whatsapp_outbox.stage_pending() (also no commit). Both are staged
into the SAME SQLAlchemy transaction the caller (routers/whatsapp.py) is
building, which also stages the webhook idempotency marker -- ONE commit
covers the wizard/session state advance, every outbox row this call
staged, and that marker, together. If the process dies anywhere inside
handle_inbound, NONE of those three land, so a redelivery of the exact same
message replays this call from scratch against the UNCHANGED prior state --
never a wizard step applied twice, and never an intermediate numeric answer
(no unique key of its own, unlike the final OFERTA confirmation's
source_message_id) silently consumed as the answer to the NEXT question
just because the state commit had already gone through separately. Meta is
only ever called (attempt_send_one, per row) by the caller AFTER that
commit succeeds -- see routers/whatsapp.py's _handle_vip_message_atomically.

handle_inbound() returns the list of newly-staged OutboundMessageDB rows so
the caller can do that. The list lives in threading.local() (self._local),
never a plain instance attribute -- this router is a process-wide singleton
(get_whatsapp_vip_router()), and a bare list on self would race if two
inbound deliveries ever ran on different OS threads at once (this codebase
has no `await` inside handle_inbound, so it can never interleave with
itself on a single thread/event loop either way -- threading.local is
specifically for genuine OS-thread concurrency, e.g. two webhook requests
served by different worker threads).

Revision 2026-09-27, second pass (full commit-graph audit): every db.commit()
reachable from _dispatch has been removed, not just handle_inbound's own.
identity_svc.stage_identity() (identity), _get_or_create_session() (session),
create_offer/close_offer/register_interest/_lazily_expire_if_needed
(services/whatsapp_vip_offers.py), and redeem_vip_invite()
(whatsapp_vip_identity.py) all now use db.flush() only -- every one of them
is reachable ONLY from this file's _dispatch, never from vip_admin.py or any
other standalone entry point, so NOTHING they touch has its own separate
transaction anymore. The ENTIRE effect of one inbound VIP message --
identity/session creation, the wizard's state advance, an offer's creation/
close, an interest registration, an invite redemption, a lazy EXPIRED flip
noticed along the way, every outbox reply it staged, and the webhook
idempotency marker -- lands in exactly ONE commit
(_handle_vip_message_atomically in routers/whatsapp.py). A crash or an
injected failure ANYWHERE in that call graph, no matter how deep, now rolls
back the WHOLE thing; a redelivery of the same message_id replays from the
unchanged prior state, and reaches the exact same idempotency guard
(source_message_id's unique constraint for create_offer, the
(offer_id, interested_channel_identity_id) unique constraint for
register_interest, the CLOSED-is-a-no-op check for close_offer, the
already-authorized check for redeem_vip_invite) that previously only
protected against a redelivery arriving AFTER a real commit -- now it also
covers a redelivery after a rollback of an attempt that got partway through
before failing.

authorize_whatsapp_id()/create_vip_invite()/revoke_vip_invite()/
revoke_membership() (whatsapp_vip_identity.py) are DIFFERENT: they are
vip_admin.py's own standalone units of work (one operator-run CLI
invocation, one session) and correctly keep their OWN commit -- see that
module's docstring for the explicit split (stage_identity as the shared
non-committing core; those four functions build on it and commit once,
themselves, never through this router).
"""
from __future__ import annotations

import json
import re
import threading
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from .. import timeutils
from ..messaging.whatsapp_adapter import MetaClient
from ..models import (
    Channel,
    ChannelIdentityDB,
    OutboundMessageDB,
    VipCurrency,
    VipOfferIntent,
    VipOfferStatus,
    VipSettlementMethod,
    VipWizardState,
    WhatsAppVipOfferDB,
    WhatsAppVipSessionDB,
)
from ..services import whatsapp_outbox
from ..services.whatsapp_vip_offers import (
    ALLOWED_DURATION_HOURS,
    OfferError,
    close_offer,
    create_offer,
    get_my_offers,
    get_offer_by_id,
    list_active_offers,
    register_interest,
)
from . import whatsapp_vip_identity as identity_svc

MAX_OTHER_METHOD_DETAIL_LENGTH = 80
MAX_LOCATION_LENGTH = 80

_CURRENCY_ORDER = [VipCurrency.USD, VipCurrency.CUP, VipCurrency.EUR, VipCurrency.MLC, VipCurrency.MXN]
_METHOD_ORDER = [VipSettlementMethod.CASH, VipSettlementMethod.TRANSFER, VipSettlementMethod.ZELLE, VipSettlementMethod.MLC_BALANCE, VipSettlementMethod.OTHER]
_METHOD_LABELS = {
    VipSettlementMethod.CASH: "Efectivo",
    VipSettlementMethod.TRANSFER: "Transferencia",
    VipSettlementMethod.ZELLE: "Zelle",
    VipSettlementMethod.MLC_BALANCE: "Saldo MLC",
    VipSettlementMethod.OTHER: "Otro",
}
_INTENT_ORDER = [VipOfferIntent.COMPRO, VipOfferIntent.VENDO, VipOfferIntent.CAMBIO]
_INTENT_LABELS = {VipOfferIntent.COMPRO: "Comprar", VipOfferIntent.VENDO: "Vender", VipOfferIntent.CAMBIO: "Cambiar"}

NOT_AUTHORIZED_TEXT = (
    "No estas autorizado para este piloto VIP. Si tenes un codigo de invitacion, "
    "envia: VIP <codigo>"
)
SUSPENDED_TEXT = "Tu acceso al piloto VIP esta suspendido."
INVALID_CODE_TEXT = "Codigo invalido o vencido."
UNKNOWN_COMMAND_TEXT = (
    "No entendi ese mensaje.\n\nOFERTA - crear una oferta\nOFERTAS - ver ofertas disponibles\n"
    "MIS OFERTAS - ver tus ofertas\nME INTERESA <id> - mostrar interes en una oferta\nCERRAR <id> - cerrar tu oferta"
)


def _get_or_create_session(db: Session, identity: ChannelIdentityDB) -> WhatsAppVipSessionDB:
    """Never commits -- only ever called from _dispatch (the VIP message
    path); a NEW session row (db.flush() only) lands in the caller's single
    atomic commit, same as everything else _dispatch touches."""
    session = db.query(WhatsAppVipSessionDB).filter(WhatsAppVipSessionDB.channel_identity_id == identity.id).first()
    if session is None:
        session = WhatsAppVipSessionDB(channel_identity_id=identity.id, state=VipWizardState.IDLE)
        db.add(session)
        db.flush()
        db.refresh(session)
    return session


def _get_context(session: WhatsAppVipSessionDB) -> dict:
    if not session.context:
        return {}
    try:
        return json.loads(session.context)
    except (json.JSONDecodeError, TypeError):
        return {}


def _currency_menu(exclude: VipCurrency | None = None) -> tuple[str, dict[str, VipCurrency]]:
    options = [c for c in _CURRENCY_ORDER if c != exclude]
    lines = [f"{i}. {c.value}" for i, c in enumerate(options, start=1)]
    mapping = {str(i): c for i, c in enumerate(options, start=1)}
    return "\n".join(lines), mapping


def _method_menu() -> tuple[str, dict[str, VipSettlementMethod]]:
    lines = [f"{i}. {_METHOD_LABELS[m]}" for i, m in enumerate(_METHOD_ORDER, start=1)]
    mapping = {str(i): m for i, m in enumerate(_METHOD_ORDER, start=1)}
    return "\n".join(lines), mapping


def _format_offer_summary(offer: WhatsAppVipOfferDB) -> str:
    give_detail = f" ({offer.give_method_detail})" if offer.give_method_detail else ""
    receive_detail = f" ({offer.receive_method_detail})" if offer.receive_method_detail else ""
    min_part = f" (minimo {offer.min_amount})" if offer.min_amount else ""
    return (
        f"#{offer.id} -- {_INTENT_LABELS[offer.intent].upper()}\n"
        f"Entrega: {offer.amount} {offer.give_currency.value}{min_part}\n"
        f"Recibe: {offer.receive_currency.value}\n"
        f"Tasa: {offer.rate_value} {offer.rate_currency.value} por 1 "
        f"{(offer.receive_currency if offer.rate_currency == offer.give_currency else offer.give_currency).value}\n"
        f"Entrega por: {_METHOD_LABELS[offer.give_method]}{give_detail} en {offer.give_location}\n"
        f"Recibe por: {_METHOD_LABELS[offer.receive_method]}{receive_detail} en {offer.receive_location}\n"
        f"Vence: {offer.expires_at.isoformat()}"
    )


class WhatsAppVipRouter:
    def __init__(self, meta_client: MetaClient) -> None:
        self._client = meta_client
        self._local = threading.local()

    @property
    def meta_client(self) -> MetaClient:
        """Exposed so main.py's outbox sweep (startup recovery + periodic
        loop) can call services.whatsapp_outbox.flush_pending_outbound()
        against the SAME transport this router sends through."""
        return self._client

    # --- transport-agnostic entrypoint -------------------------------------

    def handle_inbound(self, db: Session, whatsapp_id: str, raw_text: str, *, message_id: str) -> list[OutboundMessageDB]:
        """Runs the full dispatch for one inbound message and returns every
        outbox row it staged (via _send/stage_pending), WITHOUT committing
        anything and WITHOUT ever calling Meta -- see the module docstring
        for why, and routers/whatsapp.py for the caller that commits the
        atomic unit and only then attempts each returned row."""
        self._local.pending_sends = []
        try:
            self._dispatch(db, whatsapp_id, raw_text, message_id=message_id)
        finally:
            pending = self._local.pending_sends
            del self._local.pending_sends
        return pending

    def _dispatch(self, db: Session, whatsapp_id: str, raw_text: str, *, message_id: str) -> None:
        text = (raw_text or "").strip()
        upper = text.upper()
        identity = identity_svc.stage_identity(db, whatsapp_id)
        session = _get_or_create_session(db, identity)

        if upper.startswith("VIP "):
            # Extract the code from the ORIGINAL-case `text`, never `upper`
            # -- invite codes (secrets.token_urlsafe) are case-sensitive, and
            # redeem_vip_invite hashes exactly what the operator generated.
            self._handle_redeem_invite(db, identity, text[4:].strip())
            return

        top_level = {
            "OFERTA": self._handle_offer_start,
            "OFERTAS": self._handle_offers_browse,
            "MIS OFERTAS": self._handle_my_offers,
            "MISOFERTAS": self._handle_my_offers,
        }
        if upper in top_level:
            top_level[upper](db, identity, session)
            return

        if upper.startswith("ME INTERESA"):
            self._handle_interest(db, identity, upper[len("ME INTERESA"):].strip())
            return
        if upper.startswith("CERRAR"):
            self._handle_close(db, identity, upper[len("CERRAR"):].strip())
            return

        state_dispatch = {
            VipWizardState.WIZ_INTENT: self._handle_wiz_intent,
            VipWizardState.WIZ_GIVE_CURRENCY: self._handle_wiz_give_currency,
            VipWizardState.WIZ_RECEIVE_CURRENCY: self._handle_wiz_receive_currency,
            VipWizardState.WIZ_AMOUNT: self._handle_wiz_amount,
            VipWizardState.WIZ_RATE_UNIT: self._handle_wiz_rate_unit,
            VipWizardState.WIZ_RATE_VALUE: self._handle_wiz_rate_value,
            VipWizardState.WIZ_GIVE_METHOD: self._handle_wiz_give_method,
            VipWizardState.WIZ_GIVE_METHOD_OTHER: self._handle_wiz_give_method_other,
            VipWizardState.WIZ_GIVE_LOCATION: self._handle_wiz_give_location,
            VipWizardState.WIZ_RECEIVE_METHOD: self._handle_wiz_receive_method,
            VipWizardState.WIZ_RECEIVE_METHOD_OTHER: self._handle_wiz_receive_method_other,
            VipWizardState.WIZ_RECEIVE_LOCATION: self._handle_wiz_receive_location,
            VipWizardState.WIZ_DURATION: self._handle_wiz_duration,
            VipWizardState.WIZ_CONFIRM: lambda db, i, s, t: self._handle_wiz_confirm(db, i, s, t, message_id=message_id),
        }
        handler = state_dispatch.get(session.state)
        if handler is not None:
            handler(db, identity, session, text)
            return

        self._send(db, identity.channel_user_id, UNKNOWN_COMMAND_TEXT, source_message_id=message_id)

    # --- helpers -------------------------------------------------------------

    def _send(self, db: Session, whatsapp_id: str, text: str, *, source_message_id: str | None) -> None:
        """Every VIP reply goes through the outbox -- but only STAGES a
        PENDING row here (services.whatsapp_outbox.stage_pending, no
        commit): it lands in the SAME atomic commit as the wizard's
        session-state advance and the webhook idempotency marker (see the
        module docstring and routers/whatsapp.py). Meta is never called from
        here -- the caller attempts each row handle_inbound() returns, AFTER
        that commit succeeds."""
        row = whatsapp_outbox.stage_pending(db, whatsapp_id, text, source_message_id=source_message_id)
        self._local.pending_sends.append(row)

    def _set_state(self, db: Session, session: WhatsAppVipSessionDB, state: VipWizardState, context: dict | None = None) -> None:
        """Mutates the session object in memory ONLY -- no commit (see
        module docstring). `db` is unused now but kept in the signature to
        avoid touching every one of this method's ~15 call sites."""
        session.state = state
        if context is not None:
            session.context = json.dumps(context)
        session.updated_at = timeutils.utcnow()

    def _require_vip(self, db: Session, identity: ChannelIdentityDB) -> bool:
        try:
            identity_svc.require_active_vip(db, identity)
            return True
        except identity_svc.MembershipRejected as exc:
            text = SUSPENDED_TEXT if exc.reason == "suspended" else NOT_AUTHORIZED_TEXT
            self._send(db, identity.channel_user_id, text, source_message_id=None)
            return False

    # --- VIP <code> redemption ----------------------------------------------

    def _handle_redeem_invite(self, db: Session, identity: ChannelIdentityDB, code: str) -> None:
        if not code:
            self._send(db, identity.channel_user_id, "Uso: VIP <codigo>", source_message_id=None)
            return
        try:
            identity_svc.redeem_vip_invite(db, identity.channel_user_id, code)
        except identity_svc.InviteRedemptionError as exc:
            text = "Ya estas autorizado." if exc.reason == "already authorized" else INVALID_CODE_TEXT
            self._send(db, identity.channel_user_id, text, source_message_id=None)
            return
        self._send(
            db,
            identity.channel_user_id,
            "Listo, ya estas autorizado para el piloto VIP.\n\nOFERTA - crear una oferta\nOFERTAS - ver ofertas disponibles",
            source_message_id=None,
        )

    # --- OFERTA (creation wizard) --------------------------------------------

    def _handle_offer_start(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB) -> None:
        if not self._require_vip(db, identity):
            return
        menu, _ = self._intent_menu()
        self._set_state(db, session, VipWizardState.WIZ_INTENT, context={})
        self._send(db, identity.channel_user_id, f"Que queres hacer?\n{menu}", source_message_id=None)

    @staticmethod
    def _intent_menu() -> tuple[str, dict[str, VipOfferIntent]]:
        lines = [f"{i}. {_INTENT_LABELS[v]}" for i, v in enumerate(_INTENT_ORDER, start=1)]
        mapping = {str(i): v for i, v in enumerate(_INTENT_ORDER, start=1)}
        return "\n".join(lines), mapping

    def _handle_wiz_intent(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        _, mapping = self._intent_menu()
        intent = mapping.get(text.strip())
        if intent is None:
            menu, _ = self._intent_menu()
            self._send(db, identity.channel_user_id, f"Opcion invalida.\n{menu}", source_message_id=None)
            return
        context = {"intent": intent.value}
        menu, _ = _currency_menu()
        self._set_state(db, session, VipWizardState.WIZ_GIVE_CURRENCY, context=context)
        self._send(db, identity.channel_user_id, f"Que entregas? (moneda)\n{menu}", source_message_id=None)

    def _handle_wiz_give_currency(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        _, mapping = _currency_menu()
        currency = mapping.get(text.strip())
        if currency is None:
            menu, _ = _currency_menu()
            self._send(db, identity.channel_user_id, f"Opcion invalida.\n{menu}", source_message_id=None)
            return
        context = _get_context(session)
        context["give_currency"] = currency.value
        menu, _ = _currency_menu(exclude=currency)
        self._set_state(db, session, VipWizardState.WIZ_RECEIVE_CURRENCY, context=context)
        self._send(db, identity.channel_user_id, f"Que recibis? (debe ser distinta a {currency.value})\n{menu}", source_message_id=None)

    def _handle_wiz_receive_currency(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        context = _get_context(session)
        give_currency = VipCurrency(context["give_currency"])
        menu, mapping = _currency_menu(exclude=give_currency)
        currency = mapping.get(text.strip())
        if currency is None:
            self._send(db, identity.channel_user_id, f"Opcion invalida.\n{menu}", source_message_id=None)
            return
        context["receive_currency"] = currency.value
        self._set_state(db, session, VipWizardState.WIZ_AMOUNT, context=context)
        self._send(
            db,
            identity.channel_user_id,
            f"Cuanto {give_currency.value} tenes disponible? Envia un numero (ej: 4000) o un rango (ej: 1000-4000).",
            source_message_id=None,
        )

    _AMOUNT_RANGE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*$")

    def _handle_wiz_amount(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        context = _get_context(session)
        stripped = text.strip()
        range_match = self._AMOUNT_RANGE_RE.match(stripped)
        min_amount: Decimal | None = None
        try:
            if range_match:
                min_amount = Decimal(range_match.group(1))
                amount = Decimal(range_match.group(2))
                if not (min_amount > 0 and amount > min_amount):
                    raise InvalidOperation
            else:
                amount = Decimal(stripped)
                if amount <= 0:
                    raise InvalidOperation
        except InvalidOperation:
            self._send(db, identity.channel_user_id, "Monto invalido. Envia un numero (ej: 4000) o un rango (ej: 1000-4000).", source_message_id=None)
            return

        context["amount"] = str(amount)
        context["min_amount"] = str(min_amount) if min_amount is not None else None
        give_currency = context["give_currency"]
        receive_currency = context["receive_currency"]
        self._set_state(db, session, VipWizardState.WIZ_RATE_UNIT, context=context)
        self._send(
            db,
            identity.channel_user_id,
            f"En que moneda esta la tasa?\n1. {give_currency}\n2. {receive_currency}",
            source_message_id=None,
        )

    def _handle_wiz_rate_unit(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        context = _get_context(session)
        choice = text.strip()
        if choice == "1":
            rate_currency = context["give_currency"]
        elif choice == "2":
            rate_currency = context["receive_currency"]
        else:
            self._send(db, identity.channel_user_id, f"Opcion invalida.\n1. {context['give_currency']}\n2. {context['receive_currency']}", source_message_id=None)
            return
        other = context["receive_currency"] if rate_currency == context["give_currency"] else context["give_currency"]
        context["rate_currency"] = rate_currency
        self._set_state(db, session, VipWizardState.WIZ_RATE_VALUE, context=context)
        self._send(db, identity.channel_user_id, f"Cuantos {rate_currency} por 1 {other}? Envia solo el numero (ej: 445).", source_message_id=None)

    def _handle_wiz_rate_value(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        rate = self._parse_positive_decimal(text)
        if rate is None:
            self._send(db, identity.channel_user_id, "Tasa invalida. Envia solo un numero, ej: 445", source_message_id=None)
            return
        context = _get_context(session)
        context["rate_value"] = str(rate)
        menu, _ = _method_menu()
        self._set_state(db, session, VipWizardState.WIZ_GIVE_METHOD, context=context)
        self._send(db, identity.channel_user_id, f"Como entregas? (medio de tu lado)\n{menu}", source_message_id=None)

    def _handle_wiz_give_method(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        menu, mapping = _method_menu()
        method = mapping.get(text.strip())
        if method is None:
            self._send(db, identity.channel_user_id, f"Opcion invalida.\n{menu}", source_message_id=None)
            return
        context = _get_context(session)
        context["give_method"] = method.value
        if method == VipSettlementMethod.OTHER:
            self._set_state(db, session, VipWizardState.WIZ_GIVE_METHOD_OTHER, context=context)
            self._send(db, identity.channel_user_id, "Describi brevemente el medio (ej: tarjeta BPA):", source_message_id=None)
            return
        self._set_state(db, session, VipWizardState.WIZ_GIVE_LOCATION, context=context)
        self._send(db, identity.channel_user_id, "Ubicacion para la entrega? (ej: La Habana)", source_message_id=None)

    def _handle_wiz_give_method_other(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        detail = text.strip()[:MAX_OTHER_METHOD_DETAIL_LENGTH]
        if not detail:
            self._send(db, identity.channel_user_id, "Descripcion invalida. Intenta de nuevo, ej: tarjeta BPA", source_message_id=None)
            return
        context = _get_context(session)
        context["give_method_detail"] = detail
        self._set_state(db, session, VipWizardState.WIZ_GIVE_LOCATION, context=context)
        self._send(db, identity.channel_user_id, "Ubicacion para la entrega? (ej: La Habana)", source_message_id=None)

    def _handle_wiz_give_location(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        location = text.strip()[:MAX_LOCATION_LENGTH]
        if not location:
            self._send(db, identity.channel_user_id, "Ubicacion invalida. Intenta de nuevo.", source_message_id=None)
            return
        context = _get_context(session)
        context["give_location"] = location
        menu, _ = _method_menu()
        self._set_state(db, session, VipWizardState.WIZ_RECEIVE_METHOD, context=context)
        self._send(db, identity.channel_user_id, f"Como recibis? (medio del otro lado)\n{menu}", source_message_id=None)

    def _handle_wiz_receive_method(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        menu, mapping = _method_menu()
        method = mapping.get(text.strip())
        if method is None:
            self._send(db, identity.channel_user_id, f"Opcion invalida.\n{menu}", source_message_id=None)
            return
        context = _get_context(session)
        context["receive_method"] = method.value
        if method == VipSettlementMethod.OTHER:
            self._set_state(db, session, VipWizardState.WIZ_RECEIVE_METHOD_OTHER, context=context)
            self._send(db, identity.channel_user_id, "Describi brevemente el medio (ej: tarjeta BPA):", source_message_id=None)
            return
        self._set_state(db, session, VipWizardState.WIZ_RECEIVE_LOCATION, context=context)
        self._send(db, identity.channel_user_id, "Ubicacion para recibir? (ej: Miami)", source_message_id=None)

    def _handle_wiz_receive_method_other(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        detail = text.strip()[:MAX_OTHER_METHOD_DETAIL_LENGTH]
        if not detail:
            self._send(db, identity.channel_user_id, "Descripcion invalida. Intenta de nuevo.", source_message_id=None)
            return
        context = _get_context(session)
        context["receive_method_detail"] = detail
        self._set_state(db, session, VipWizardState.WIZ_RECEIVE_LOCATION, context=context)
        self._send(db, identity.channel_user_id, "Ubicacion para recibir? (ej: Miami)", source_message_id=None)

    def _handle_wiz_receive_location(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        location = text.strip()[:MAX_LOCATION_LENGTH]
        if not location:
            self._send(db, identity.channel_user_id, "Ubicacion invalida. Intenta de nuevo.", source_message_id=None)
            return
        context = _get_context(session)
        context["receive_location"] = location
        self._set_state(db, session, VipWizardState.WIZ_DURATION, context=context)
        self._send(db, identity.channel_user_id, "Vigencia?\n1. 24 horas (por defecto)\n2. 3 dias", source_message_id=None)

    def _handle_wiz_duration(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        choice = text.strip()
        duration_map = {"1": ALLOWED_DURATION_HOURS[0], "2": ALLOWED_DURATION_HOURS[1]}
        duration_hours = duration_map.get(choice)
        if duration_hours is None:
            self._send(db, identity.channel_user_id, "Opcion invalida.\n1. 24 horas (por defecto)\n2. 3 dias", source_message_id=None)
            return
        context = _get_context(session)
        context["duration_hours"] = duration_hours

        summary = self._render_pending_summary(context)
        self._set_state(db, session, VipWizardState.WIZ_CONFIRM, context=context)
        self._send(db, identity.channel_user_id, f"{summary}\n\nResponde CONFIRMAR para publicar o CANCELAR para descartar.", source_message_id=None)

    @staticmethod
    def _render_pending_summary(context: dict) -> str:
        give_currency = context["give_currency"]
        receive_currency = context["receive_currency"]
        rate_currency = context["rate_currency"]
        other_currency = receive_currency if rate_currency == give_currency else give_currency
        min_part = f" (minimo {context['min_amount']})" if context.get("min_amount") else ""
        give_method_label = _METHOD_LABELS[VipSettlementMethod(context["give_method"])]
        give_detail = f" ({context['give_method_detail']})" if context.get("give_method_detail") else ""
        receive_method_label = _METHOD_LABELS[VipSettlementMethod(context["receive_method"])]
        receive_detail = f" ({context['receive_method_detail']})" if context.get("receive_method_detail") else ""
        duration_label = "24 horas" if context["duration_hours"] == ALLOWED_DURATION_HOURS[0] else "3 dias"
        return (
            f"Confirmar oferta ({_INTENT_LABELS[VipOfferIntent(context['intent'])].upper()}):\n\n"
            f"Entregas: {context['amount']} {give_currency}{min_part}\n"
            f"Recibis: {receive_currency}\n"
            f"Tasa: {context['rate_value']} {rate_currency} por 1 {other_currency}\n"
            f"Entregas por: {give_method_label}{give_detail} en {context['give_location']}\n"
            f"Recibis por: {receive_method_label}{receive_detail} en {context['receive_location']}\n"
            f"Vigencia: {duration_label}"
        )

    def _handle_wiz_confirm(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str, *, message_id: str) -> None:
        choice = text.strip().upper()
        if choice == "CANCELAR":
            self._set_state(db, session, VipWizardState.IDLE, context={})
            self._send(db, identity.channel_user_id, "Oferta descartada.", source_message_id=message_id)
            return
        if choice != "CONFIRMAR":
            self._send(db, identity.channel_user_id, "Respondi CONFIRMAR para publicar o CANCELAR para descartar.", source_message_id=None)
            return

        context = _get_context(session)
        try:
            offer = create_offer(
                db,
                identity,
                source_message_id=message_id,
                intent=VipOfferIntent(context["intent"]),
                give_currency=VipCurrency(context["give_currency"]),
                receive_currency=VipCurrency(context["receive_currency"]),
                amount=Decimal(context["amount"]),
                min_amount=Decimal(context["min_amount"]) if context.get("min_amount") else None,
                rate_value=Decimal(context["rate_value"]),
                rate_currency=VipCurrency(context["rate_currency"]),
                give_method=VipSettlementMethod(context["give_method"]),
                give_method_detail=context.get("give_method_detail"),
                give_location=context["give_location"],
                receive_method=VipSettlementMethod(context["receive_method"]),
                receive_method_detail=context.get("receive_method_detail"),
                receive_location=context["receive_location"],
                duration_hours=context["duration_hours"],
            )
        except identity_svc.MembershipRejected:
            self._set_state(db, session, VipWizardState.IDLE, context={})
            self._send(db, identity.channel_user_id, SUSPENDED_TEXT, source_message_id=message_id)
            return
        except OfferError as exc:
            self._set_state(db, session, VipWizardState.IDLE, context={})
            self._send(db, identity.channel_user_id, f"No se pudo publicar la oferta: {exc.reason}", source_message_id=message_id)
            return

        self._set_state(db, session, VipWizardState.IDLE, context={})
        self._send(
            db,
            identity.channel_user_id,
            f"Oferta #{offer.id} publicada (ACTIVA) hasta {offer.expires_at.isoformat()}.\n\nOFERTAS para ver otras, MIS OFERTAS para ver la tuya.",
            source_message_id=message_id,
        )

    # --- OFERTAS (browse, read-only) -----------------------------------------

    def _handle_offers_browse(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB) -> None:
        if not self._require_vip(db, identity):
            return
        # owner_channel=WHATSAPP (2026-09-27 audit, second pass): OFERTAS on
        # WhatsApp must never list a Telegram-owned offer -- see
        # list_active_offers' own docstring.
        offers = list_active_offers(db, owner_channel=Channel.WHATSAPP, exclude_identity=identity)
        self._set_state(db, session, VipWizardState.IDLE, context={})
        if not offers:
            self._send(db, identity.channel_user_id, "No hay ofertas disponibles ahora mismo.", source_message_id=None)
            return
        lines = ["OFERTAS DISPONIBLES", ""]
        for o in offers:
            lines.append(_format_offer_summary(o))
            lines.append("")
        lines.append("ME INTERESA <id> para mostrar interes.")
        self._send(db, identity.channel_user_id, "\n".join(lines), source_message_id=None)

    # --- MIS OFERTAS ----------------------------------------------------------

    def _handle_my_offers(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB) -> None:
        if not self._require_vip(db, identity):
            return
        offers = get_my_offers(db, identity)
        self._set_state(db, session, VipWizardState.IDLE, context={})
        if not offers:
            self._send(db, identity.channel_user_id, "No tenes ofertas todavia. Escribi OFERTA para crear una.", source_message_id=None)
            return
        lines = ["TUS OFERTAS", ""]
        for o in offers:
            lines.append(f"{_format_offer_summary(o)}\nEstado: {o.status.value.upper()}")
            lines.append("")
        lines.append("CERRAR <id> para cerrar una oferta activa.")
        self._send(db, identity.channel_user_id, "\n".join(lines), source_message_id=None)

    # --- ME INTERESA <id> -------------------------------------------------

    def _handle_interest(self, db: Session, identity: ChannelIdentityDB, id_text: str) -> None:
        if not self._require_vip(db, identity):
            return
        offer = self._parse_offer_id(db, identity, id_text)
        if offer is None:
            return
        try:
            _interest, created = register_interest(db, identity, offer)
        except OfferError as exc:
            self._send(db, identity.channel_user_id, f"No se pudo registrar el interes: {exc.reason}", source_message_id=None)
            return

        if created:
            self._send(db, identity.channel_user_id, f"Interes registrado en la oferta #{offer.id}.", source_message_id=None)
            # Never reveal the interested party's whatsapp_id to the owner --
            # only that SOMEONE is interested, via the bot.
            owner_whatsapp_id = self._whatsapp_id_of(db, offer.channel_identity_id)
            if owner_whatsapp_id:
                self._send(
                    db,
                    owner_whatsapp_id,
                    f"Alguien mostro interes en tu oferta #{offer.id}. Escribi MIS OFERTAS para verla.",
                    source_message_id=None,
                )
        else:
            self._send(db, identity.channel_user_id, f"Ya habias mostrado interes en la oferta #{offer.id}.", source_message_id=None)

    @staticmethod
    def _whatsapp_id_of(db: Session, channel_identity_id: int) -> str:
        """Cross-channel isolation (2026-09-27 audit): returns "" (no-op,
        same as "identity not found") unless the owner is actually a
        WHATSAPP identity -- defense in depth alongside get_offer_by_id's
        own owner_channel filter in _parse_offer_id below, so this can never
        hand a Telegram numeric id to WhatsAppAdapter.send() even if some
        future call site reaches here with an unfiltered offer."""
        owner_identity = db.query(ChannelIdentityDB).filter(ChannelIdentityDB.id == channel_identity_id).first()
        if owner_identity is None or owner_identity.channel != Channel.WHATSAPP:
            return ""
        return owner_identity.channel_user_id

    # --- CERRAR <id> ------------------------------------------------------

    def _handle_close(self, db: Session, identity: ChannelIdentityDB, id_text: str) -> None:
        if not self._require_vip(db, identity):
            return
        offer = self._parse_offer_id(db, identity, id_text)
        if offer is None:
            return
        was_already_closed = offer.status == VipOfferStatus.CLOSED
        try:
            updated = close_offer(db, identity, offer)
        except OfferError as exc:
            self._send(db, identity.channel_user_id, f"No se pudo cerrar: {exc.reason}", source_message_id=None)
            return

        text = f"La oferta #{updated.id} ya estaba cerrada." if was_already_closed else f"Oferta #{updated.id} cerrada."
        self._send(db, identity.channel_user_id, text, source_message_id=None)

    def _parse_offer_id(self, db: Session, identity: ChannelIdentityDB, id_text: str) -> WhatsAppVipOfferDB | None:
        if not id_text.strip().lstrip("#").isdigit():
            self._send(db, identity.channel_user_id, "Uso: <comando> <id>, ej: ME INTERESA 7", source_message_id=None)
            return None
        offer_id = int(id_text.strip().lstrip("#"))
        # owner_channel=WHATSAPP (2026-09-27 audit): WhatsAppVipOfferDB is
        # shared with telegram_vip_router.py -- a Telegram-owned offer must
        # be invisible/unactionable here, exactly "no existe", never a
        # different error that would confirm its existence on the other
        # channel (see services/whatsapp_vip_offers.get_offer_by_id).
        offer = get_offer_by_id(db, offer_id, owner_channel=Channel.WHATSAPP)
        if offer is None:
            self._send(db, identity.channel_user_id, f"No existe la oferta #{offer_id}.", source_message_id=None)
            return None
        return offer

    @staticmethod
    def _parse_positive_decimal(text: str) -> Decimal | None:
        try:
            value = Decimal(text.strip())
        except (InvalidOperation, ValueError):
            return None
        return value if value > 0 else None
