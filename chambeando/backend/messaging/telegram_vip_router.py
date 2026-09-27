"""
TelegramVipRouter -- Telegram-as-source-of-truth pilot for the VIP offer
board (2026-09-27). Telegram is where an invited member creates and
confirms an offer (private wizard); an admin then copies the generated
text + deep link and pastes it manually into the WhatsApp VIP group --
this module NEVER calls the Meta Cloud API and never posts to WhatsApp
itself. A person who taps that link in WhatsApp opens Telegram (via
t.me/<bot>?start=offer_<id>) and expresses interest there.

Deliberately reuses, rather than duplicates, the VIP domain this codebase
already built and audited for WhatsApp:

  - services/whatsapp_vip_offers.py (create_offer/list_active_offers/
    get_my_offers/get_offer_by_id/close_offer/register_interest) is used
    UNCHANGED -- it was already generic over `identity: ChannelIdentityDB`,
    never WhatsApp-specific itself.
  - WhatsAppVipOfferDB/WhatsAppVipMembershipDB/WhatsAppVipInviteDB/
    WhatsAppVipSessionDB are reused AS-IS (no migration): every one of them
    is keyed off channel_identity_id, which was already channel-agnostic.
    See messaging/whatsapp_vip_identity.py's revision note for the one
    change that WAS needed (the identity-lookup layer no longer hardcodes
    Channel.WHATSAPP).
  - "No reutilices P2POfferDB de una sola moneda" -- correct, and this
    module never imports P2POfferDB, OfferSide, or telegram_offers.py at
    all. Nor does it import telegram_identity.py/TelegramMembershipDB: that
    module grants access to ANY Telegram user who says /start (by design,
    for the OPEN marketplace) -- the VIP pilot's access rule is the
    opposite ("Acceso solo a miembros invitados"), so it gates through
    WhatsAppVipMembershipDB/whatsapp_vip_identity.require_active_vip, the
    SAME invite-only gate WhatsApp's VIP pilot uses, exactly like
    TelegramConversationRouter and WhatsAppVipRouter already stay mutually
    isolated on the WhatsApp side.

Commit discipline mirrors whatsapp_vip_router.py's audited pattern:
handle_inbound() never commits and never calls the Telegram Bot API --
every reply is STAGED (via services/telegram_outbox.stage_pending, a
db.flush()-only durable PENDING row) in the SAME transaction the caller
(routers/telegram.py's _handle_telegram_vip_message_atomically) commits
together with the wizard/session-state advance and the webhook idempotency
marker; only AFTER that commit succeeds does the caller attempt to send
each row (services/telegram_outbox.attempt_send_one).

Revision 2026-09-27 (reliability audit, "revision final del piloto"): an
earlier version of this module staged replies as plain in-memory
(chat_id, text) tuples and reasoned that a durable outbox was unnecessary
because Telegram's Bot API has none of Meta's asynchronous
delivery-status-webhook ambiguity. That reasoning was correct about
*delivery status* but wrong about *durability*: TelegramAdapter.send()
still swallows TelegramApiError (same best-effort contract as
WhatsAppAdapter.send, so the inbound webhook's ack never depends on
outbound success), which means a transient API failure OR a process crash
between the atomic commit and the send loop silently dropped the reply
forever -- nothing was ever persisted to recover it from. See
tests/test_telegram_vip_outbox_reliability.py, which reproduces both
failure modes against the pre-fix code and proves this outbox closes them.
Constructed from the raw TelegramClient (not a TelegramAdapter), exactly
like WhatsAppVipRouter takes a raw MetaClient -- the outbox needs direct
access to a client whose send_message() actually RAISES TelegramApiError
on failure (TelegramAdapter.send() would swallow it, defeating the whole
point).

Cross-channel isolation (same audit): every offer lookup that can lead to
an action (deep-link view, ME INTERESA, CERRAR) passes
owner_channel=Channel.TELEGRAM to get_offer_by_id -- WhatsAppVipOfferDB is
the SAME table whatsapp_vip_router.py writes to, so without this a
Telegram identity could reference a WhatsApp-owned offer's id and this
router would try to notify its owner through the Telegram Bot API using a
WhatsApp phone-derived id. See services/whatsapp_vip_offers.py's
get_offer_by_id docstring and _channel_user_id_of below.
"""
from __future__ import annotations

import json
import re
import threading
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from .. import timeutils
from ..config import settings
from ..models import (
    Channel,
    ChannelIdentityDB,
    TelegramOutboundMessageDB,
    VipCurrency,
    VipOfferIntent,
    VipOfferStatus,
    VipSettlementMethod,
    VipWizardState,
    WhatsAppVipOfferDB,
    WhatsAppVipSessionDB,
)
from ..services import telegram_outbox
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
from .telegram_adapter import TelegramClient
from .whatsapp_vip_router import (
    MAX_LOCATION_LENGTH,
    MAX_OTHER_METHOD_DETAIL_LENGTH,
    _currency_menu,
    _format_offer_summary,
    _INTENT_LABELS,
    _method_menu,
    _METHOD_LABELS,
)

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

# Telegram's own deep-link convention: t.me/<bot>?start=<payload> makes
# Telegram send the bot the literal text "/start <payload>" the moment the
# link is tapped. `offer_<id>` is OUR payload shape (chosen so it can never
# collide with a plain "/start", which telegram_router.py's OWN /START
# handler -- a different router, the open marketplace -- still owns).
_OFFER_DEEPLINK_RE = re.compile(r"^/start\s+offer_(\d+)\s*$", re.IGNORECASE)
_BARE_START_RE = re.compile(r"^/start(\s+\S.*)?$", re.IGNORECASE)


def validate_telegram_bot_username(username: str | None) -> str | None:
    """Telegram's own username rules (5-32 chars, letters/digits/
    underscore, must end in "bot", case-insensitive) -- returns the
    username unchanged if valid, else None. Never guesses or fabricates a
    username: a None return means "do not build a deep link", the caller's
    job, never this function's, to decide what to show the operator
    instead (see _handle_wiz_confirm below)."""
    if not username:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return None
    if not username.lower().endswith("bot"):
        return None
    return username


def build_offer_deep_link(offer_id: int) -> str | None:
    bot_username = validate_telegram_bot_username(settings.TELEGRAM_VIP_BOT_USERNAME)
    if bot_username is None:
        return None
    return f"https://t.me/{bot_username}?start=offer_{offer_id}"


def _render_whatsapp_share_text(offer: WhatsAppVipOfferDB, deep_link: str | None) -> str:
    """The short, copy-paste-ready announcement an admin pastes manually
    into the WhatsApp VIP group (requirement 3) -- never sent
    automatically, never posted via any API, only returned as text in the
    Telegram confirmation reply for a human to copy themselves."""
    other = offer.receive_currency if offer.rate_currency == offer.give_currency else offer.give_currency
    lines = [
        f"[VIP] {_INTENT_LABELS[offer.intent].upper()} -- {offer.amount} {offer.give_currency.value} <-> {offer.receive_currency.value}",
        f"Tasa: {offer.rate_value} {offer.rate_currency.value} por 1 {other.value}",
        f"Vence: {offer.expires_at.isoformat()}",
    ]
    if deep_link:
        lines.append(f"Interesados: {deep_link}")
    else:
        lines.append("(enlace no disponible -- configura TELEGRAM_VIP_BOT_USERNAME)")
    return "\n".join(lines)


def _get_or_create_session(db: Session, identity: ChannelIdentityDB) -> WhatsAppVipSessionDB:
    """Reuses WhatsAppVipSessionDB as-is (see module docstring) -- never
    commits, matching whatsapp_vip_router.py's own _get_or_create_session."""
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


class TelegramVipRouter:
    def __init__(self, telegram_client: TelegramClient) -> None:
        """Takes the RAW TelegramClient (not a TelegramAdapter) -- same
        reason WhatsAppVipRouter takes a raw MetaClient: the outbox
        (services/telegram_outbox.py) needs a client whose send_message()
        actually RAISES TelegramApiError on failure, which TelegramAdapter's
        own send() deliberately swallows (see module docstring's revision
        note)."""
        self._client = telegram_client
        self._local = threading.local()

    @property
    def telegram_client(self) -> TelegramClient:
        """Exposed so routers/telegram.py's outbox sweep and main.py's
        startup/periodic recovery sweep can call the SAME transport this
        router would use -- same reason WhatsAppVipRouter.meta_client is
        exposed to routers/whatsapp.py."""
        return self._client

    # --- transport-agnostic entrypoint -------------------------------------

    def handle_inbound(self, db: Session, telegram_user_id: str, raw_text: str, *, message_id: str) -> list[TelegramOutboundMessageDB]:
        """Same shape as WhatsAppVipRouter.handle_inbound: stages every
        reply as a durable PENDING outbox row (never commits, never calls
        the Telegram Bot API). Returns every row this call staged so the
        caller (routers/telegram.py) can attempt each one AFTER its own
        atomic commit succeeds."""
        self._local.pending_sends = []
        try:
            self._dispatch(db, telegram_user_id, raw_text, message_id=message_id)
        finally:
            pending = self._local.pending_sends
            del self._local.pending_sends
        return pending

    def _dispatch(self, db: Session, telegram_user_id: str, raw_text: str, *, message_id: str) -> None:
        text = (raw_text or "").strip()
        upper = text.upper()
        identity = identity_svc.stage_identity(db, telegram_user_id, channel=Channel.TELEGRAM)
        session = _get_or_create_session(db, identity)

        deep_link_match = _OFFER_DEEPLINK_RE.match(text)
        if deep_link_match:
            self._handle_offer_deep_link(db, identity, int(deep_link_match.group(1)))
            return
        if _BARE_START_RE.match(text):
            # A /start with no payload, or a payload that isn't our
            # offer_<id> shape (tampered/garbled link) -- never crash, never
            # silently fall through to a wizard step; just explain.
            self._send(db, identity.channel_user_id, "Enlace invalido o vencido. Si tenes un codigo de invitacion, envia: VIP <codigo>")
            return

        if upper.startswith("VIP "):
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

        self._send(db, identity.channel_user_id, UNKNOWN_COMMAND_TEXT)

    # --- helpers -------------------------------------------------------------

    def _send(self, db: Session, chat_id: str, text: str) -> None:
        """Stages a durable PENDING row via services/telegram_outbox.py
        (db.flush() only, no commit -- see module docstring) instead of
        appending to a plain in-memory list. source_message_id is left None
        throughout this router (unlike WhatsAppVipRouter's _send): it is
        observability-only there too (never an idempotency key -- the real
        idempotency guarantee is routers/telegram.py's ProcessedWebhookEventDB
        marker), and threading it through every one of this method's ~55
        call sites was judged not worth the size for a pilot."""
        row = telegram_outbox.stage_pending(db, chat_id, text)
        self._local.pending_sends.append(row)

    def _set_state(self, session: WhatsAppVipSessionDB, state: VipWizardState, context: dict | None = None) -> None:
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
            self._send(db, identity.channel_user_id, text)
            return False

    # --- deep link: t.me/<bot>?start=offer_<id> ------------------------------

    def _handle_offer_deep_link(self, db: Session, identity: ChannelIdentityDB, offer_id: int) -> None:
        """Requirement 4: consults the offer's CURRENT (lazily-corrected)
        status regardless of the viewer's own VIP membership -- the same
        information an admin already pasted publicly into the WhatsApp
        group. Only ME INTERESA (below) is gated behind require_active_vip;
        merely viewing an offer's status through the link is not. An
        offer that is CLOSED/EXPIRED is shown as such and interest is
        never possible on it (register_interest's own effective-status
        check enforces that even if this view text is skipped)."""
        # owner_channel=TELEGRAM (2026-09-27 audit): never show or act on a
        # WhatsApp-owned offer through this link, even if its id is guessed
        # or shared verbatim -- see get_offer_by_id's docstring.
        offer = get_offer_by_id(db, offer_id, owner_channel=Channel.TELEGRAM)
        if offer is None:
            self._send(db, identity.channel_user_id, f"No existe la oferta #{offer_id}. El enlace puede estar alterado o vencido.")
            return
        if offer.status != VipOfferStatus.ACTIVE:
            self._send(
                db,
                identity.channel_user_id,
                f"La oferta #{offer.id} ya no esta disponible (estado: {offer.status.value.upper()}).",
            )
            return
        self._send(
            db,
            identity.channel_user_id,
            f"{_format_offer_summary(offer)}\n\nPara mostrar interes: ME INTERESA {offer.id}",
        )

    # --- VIP <code> redemption ----------------------------------------------

    def _handle_redeem_invite(self, db: Session, identity: ChannelIdentityDB, code: str) -> None:
        if not code:
            self._send(db, identity.channel_user_id, "Uso: VIP <codigo>")
            return
        try:
            identity_svc.redeem_vip_invite(db, identity.channel_user_id, code, channel=Channel.TELEGRAM)
        except identity_svc.InviteRedemptionError as exc:
            text = "Ya estas autorizado." if exc.reason == "already authorized" else INVALID_CODE_TEXT
            self._send(db, identity.channel_user_id, text)
            return
        self._send(
            db,
            identity.channel_user_id,
            "Listo, ya estas autorizado para el piloto VIP.\n\nOFERTA - crear una oferta\nOFERTAS - ver ofertas disponibles",
        )

    # --- OFERTA (creation wizard) --------------------------------------------

    def _handle_offer_start(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB) -> None:
        if not self._require_vip(db, identity):
            return
        menu, _ = self._intent_menu()
        self._set_state(session, VipWizardState.WIZ_INTENT, context={})
        self._send(db, identity.channel_user_id, f"Que queres hacer?\n{menu}")

    @staticmethod
    def _intent_menu() -> tuple[str, dict[str, VipOfferIntent]]:
        order = [VipOfferIntent.COMPRO, VipOfferIntent.VENDO, VipOfferIntent.CAMBIO]
        lines = [f"{i}. {_INTENT_LABELS[v]}" for i, v in enumerate(order, start=1)]
        mapping = {str(i): v for i, v in enumerate(order, start=1)}
        return "\n".join(lines), mapping

    def _handle_wiz_intent(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        _, mapping = self._intent_menu()
        intent = mapping.get(text.strip())
        if intent is None:
            menu, _ = self._intent_menu()
            self._send(db, identity.channel_user_id, f"Opcion invalida.\n{menu}")
            return
        context = {"intent": intent.value}
        menu, _ = _currency_menu()
        self._set_state(session, VipWizardState.WIZ_GIVE_CURRENCY, context=context)
        self._send(db, identity.channel_user_id, f"Que entregas? (moneda)\n{menu}")

    def _handle_wiz_give_currency(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        _, mapping = _currency_menu()
        currency = mapping.get(text.strip())
        if currency is None:
            menu, _ = _currency_menu()
            self._send(db, identity.channel_user_id, f"Opcion invalida.\n{menu}")
            return
        context = _get_context(session)
        context["give_currency"] = currency.value
        menu, _ = _currency_menu(exclude=currency)
        self._set_state(session, VipWizardState.WIZ_RECEIVE_CURRENCY, context=context)
        self._send(db, identity.channel_user_id, f"Que recibis? (debe ser distinta a {currency.value})\n{menu}")

    def _handle_wiz_receive_currency(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        context = _get_context(session)
        give_currency = VipCurrency(context["give_currency"])
        menu, mapping = _currency_menu(exclude=give_currency)
        currency = mapping.get(text.strip())
        if currency is None:
            self._send(db, identity.channel_user_id, f"Opcion invalida.\n{menu}")
            return
        context["receive_currency"] = currency.value
        self._set_state(session, VipWizardState.WIZ_AMOUNT, context=context)
        self._send(db, identity.channel_user_id, f"Cuanto {give_currency.value} tenes disponible? Envia un numero (ej: 4000) o un rango (ej: 1000-4000).")

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
            self._send(db, identity.channel_user_id, "Monto invalido. Envia un numero (ej: 4000) o un rango (ej: 1000-4000).")
            return

        context["amount"] = str(amount)
        context["min_amount"] = str(min_amount) if min_amount is not None else None
        give_currency = context["give_currency"]
        receive_currency = context["receive_currency"]
        self._set_state(session, VipWizardState.WIZ_RATE_UNIT, context=context)
        self._send(db, identity.channel_user_id, f"En que moneda esta la tasa?\n1. {give_currency}\n2. {receive_currency}")

    def _handle_wiz_rate_unit(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        context = _get_context(session)
        choice = text.strip()
        if choice == "1":
            rate_currency = context["give_currency"]
        elif choice == "2":
            rate_currency = context["receive_currency"]
        else:
            self._send(db, identity.channel_user_id, f"Opcion invalida.\n1. {context['give_currency']}\n2. {context['receive_currency']}")
            return
        other = context["receive_currency"] if rate_currency == context["give_currency"] else context["give_currency"]
        context["rate_currency"] = rate_currency
        self._set_state(session, VipWizardState.WIZ_RATE_VALUE, context=context)
        self._send(db, identity.channel_user_id, f"Cuantos {rate_currency} por 1 {other}? Envia solo el numero (ej: 445).")

    def _handle_wiz_rate_value(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        rate = self._parse_positive_decimal(text)
        if rate is None:
            self._send(db, identity.channel_user_id, "Tasa invalida. Envia solo un numero, ej: 445")
            return
        context = _get_context(session)
        context["rate_value"] = str(rate)
        menu, _ = _method_menu()
        self._set_state(session, VipWizardState.WIZ_GIVE_METHOD, context=context)
        self._send(db, identity.channel_user_id, f"Como entregas? (medio de tu lado)\n{menu}")

    def _handle_wiz_give_method(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        menu, mapping = _method_menu()
        method = mapping.get(text.strip())
        if method is None:
            self._send(db, identity.channel_user_id, f"Opcion invalida.\n{menu}")
            return
        context = _get_context(session)
        context["give_method"] = method.value
        if method == VipSettlementMethod.OTHER:
            self._set_state(session, VipWizardState.WIZ_GIVE_METHOD_OTHER, context=context)
            self._send(db, identity.channel_user_id, "Describi brevemente el medio (ej: tarjeta BPA):")
            return
        self._set_state(session, VipWizardState.WIZ_GIVE_LOCATION, context=context)
        self._send(db, identity.channel_user_id, "Ubicacion para la entrega? (ej: La Habana)")

    def _handle_wiz_give_method_other(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        detail = text.strip()[:MAX_OTHER_METHOD_DETAIL_LENGTH]
        if not detail:
            self._send(db, identity.channel_user_id, "Descripcion invalida. Intenta de nuevo, ej: tarjeta BPA")
            return
        context = _get_context(session)
        context["give_method_detail"] = detail
        self._set_state(session, VipWizardState.WIZ_GIVE_LOCATION, context=context)
        self._send(db, identity.channel_user_id, "Ubicacion para la entrega? (ej: La Habana)")

    def _handle_wiz_give_location(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        location = text.strip()[:MAX_LOCATION_LENGTH]
        if not location:
            self._send(db, identity.channel_user_id, "Ubicacion invalida. Intenta de nuevo.")
            return
        context = _get_context(session)
        context["give_location"] = location
        menu, _ = _method_menu()
        self._set_state(session, VipWizardState.WIZ_RECEIVE_METHOD, context=context)
        self._send(db, identity.channel_user_id, f"Como recibis? (medio del otro lado)\n{menu}")

    def _handle_wiz_receive_method(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        menu, mapping = _method_menu()
        method = mapping.get(text.strip())
        if method is None:
            self._send(db, identity.channel_user_id, f"Opcion invalida.\n{menu}")
            return
        context = _get_context(session)
        context["receive_method"] = method.value
        if method == VipSettlementMethod.OTHER:
            self._set_state(session, VipWizardState.WIZ_RECEIVE_METHOD_OTHER, context=context)
            self._send(db, identity.channel_user_id, "Describi brevemente el medio (ej: tarjeta BPA):")
            return
        self._set_state(session, VipWizardState.WIZ_RECEIVE_LOCATION, context=context)
        self._send(db, identity.channel_user_id, "Ubicacion para recibir? (ej: Miami)")

    def _handle_wiz_receive_method_other(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        detail = text.strip()[:MAX_OTHER_METHOD_DETAIL_LENGTH]
        if not detail:
            self._send(db, identity.channel_user_id, "Descripcion invalida. Intenta de nuevo.")
            return
        context = _get_context(session)
        context["receive_method_detail"] = detail
        self._set_state(session, VipWizardState.WIZ_RECEIVE_LOCATION, context=context)
        self._send(db, identity.channel_user_id, "Ubicacion para recibir? (ej: Miami)")

    def _handle_wiz_receive_location(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        location = text.strip()[:MAX_LOCATION_LENGTH]
        if not location:
            self._send(db, identity.channel_user_id, "Ubicacion invalida. Intenta de nuevo.")
            return
        context = _get_context(session)
        context["receive_location"] = location
        self._set_state(session, VipWizardState.WIZ_DURATION, context=context)
        self._send(db, identity.channel_user_id, "Vigencia?\n1. 24 horas (por defecto)\n2. 3 dias")

    def _handle_wiz_duration(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB, text: str) -> None:
        choice = text.strip()
        duration_map = {"1": ALLOWED_DURATION_HOURS[0], "2": ALLOWED_DURATION_HOURS[1]}
        duration_hours = duration_map.get(choice)
        if duration_hours is None:
            self._send(db, identity.channel_user_id, "Opcion invalida.\n1. 24 horas (por defecto)\n2. 3 dias")
            return
        context = _get_context(session)
        context["duration_hours"] = duration_hours

        summary = self._render_pending_summary(context)
        self._set_state(session, VipWizardState.WIZ_CONFIRM, context=context)
        self._send(db, identity.channel_user_id, f"{summary}\n\nResponde CONFIRMAR para publicar o CANCELAR para descartar.")

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
            self._set_state(session, VipWizardState.IDLE, context={})
            self._send(db, identity.channel_user_id, "Oferta descartada.")
            return
        if choice != "CONFIRMAR":
            self._send(db, identity.channel_user_id, "Respondi CONFIRMAR para publicar o CANCELAR para descartar.")
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
            self._set_state(session, VipWizardState.IDLE, context={})
            self._send(db, identity.channel_user_id, SUSPENDED_TEXT)
            return
        except OfferError as exc:
            self._set_state(session, VipWizardState.IDLE, context={})
            self._send(db, identity.channel_user_id, f"No se pudo publicar la oferta: {exc.reason}")
            return

        self._set_state(session, VipWizardState.IDLE, context={})
        deep_link = build_offer_deep_link(offer.id)
        share_text = _render_whatsapp_share_text(offer, deep_link)
        self._send(
            db,
            identity.channel_user_id,
            f"Oferta #{offer.id} publicada (ACTIVA) hasta {offer.expires_at.isoformat()}.\n\n"
            f"Copia esto al grupo VIP de WhatsApp:\n\n{share_text}",
        )

    # --- OFERTAS (browse, read-only) -----------------------------------------

    def _handle_offers_browse(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB) -> None:
        if not self._require_vip(db, identity):
            return
        # owner_channel=TELEGRAM (2026-09-27 audit, second pass): OFERTAS on
        # Telegram must never list a WhatsApp-owned offer -- see
        # list_active_offers' own docstring.
        offers = list_active_offers(db, owner_channel=Channel.TELEGRAM, exclude_identity=identity)
        self._set_state(session, VipWizardState.IDLE, context={})
        if not offers:
            self._send(db, identity.channel_user_id, "No hay ofertas disponibles ahora mismo.")
            return
        lines = ["OFERTAS DISPONIBLES", ""]
        for o in offers:
            lines.append(_format_offer_summary(o))
            lines.append("")
        lines.append("ME INTERESA <id> para mostrar interes.")
        self._send(db, identity.channel_user_id, "\n".join(lines))

    # --- MIS OFERTAS ----------------------------------------------------------

    def _handle_my_offers(self, db: Session, identity: ChannelIdentityDB, session: WhatsAppVipSessionDB) -> None:
        if not self._require_vip(db, identity):
            return
        offers = get_my_offers(db, identity)
        self._set_state(session, VipWizardState.IDLE, context={})
        if not offers:
            self._send(db, identity.channel_user_id, "No tenes ofertas todavia. Escribi OFERTA para crear una.")
            return
        lines = ["TUS OFERTAS", ""]
        for o in offers:
            lines.append(f"{_format_offer_summary(o)}\nEstado: {o.status.value.upper()}")
            lines.append("")
        lines.append("CERRAR <id> para cerrar una oferta activa.")
        self._send(db, identity.channel_user_id, "\n".join(lines))

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
            self._send(db, identity.channel_user_id, f"No se pudo registrar el interes: {exc.reason}")
            return

        if created:
            self._send(db, identity.channel_user_id, f"Interes registrado en la oferta #{offer.id}.")
            # Never reveal the interested party's telegram id to the owner --
            # only that SOMEONE is interested, via the bot.
            owner_channel_user_id = self._channel_user_id_of(db, offer.channel_identity_id)
            if owner_channel_user_id:
                self._send(
                    db,
                    owner_channel_user_id,
                    f"Alguien mostro interes en tu oferta #{offer.id}. Escribi MIS OFERTAS para verla.",
                )
        else:
            self._send(db, identity.channel_user_id, f"Ya habias mostrado interes en la oferta #{offer.id}.")

    @staticmethod
    def _channel_user_id_of(db: Session, channel_identity_id: int) -> str:
        """Cross-channel isolation (2026-09-27 audit): returns "" (no-op)
        unless the owner is actually a TELEGRAM identity -- defense in
        depth alongside get_offer_by_id's own owner_channel filter in
        _parse_offer_id/_handle_offer_deep_link, so this can never hand a
        WhatsApp phone-derived id to the Telegram Bot API even if some
        future call site reaches here with an unfiltered offer."""
        owner_identity = db.query(ChannelIdentityDB).filter(ChannelIdentityDB.id == channel_identity_id).first()
        if owner_identity is None or owner_identity.channel != Channel.TELEGRAM:
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
            self._send(db, identity.channel_user_id, f"No se pudo cerrar: {exc.reason}")
            return

        text = f"La oferta #{updated.id} ya estaba cerrada." if was_already_closed else f"Oferta #{updated.id} cerrada."
        self._send(db, identity.channel_user_id, text)

    def _parse_offer_id(self, db: Session, identity: ChannelIdentityDB, id_text: str) -> WhatsAppVipOfferDB | None:
        if not id_text.strip().lstrip("#").isdigit():
            self._send(db, identity.channel_user_id, "Uso: <comando> <id>, ej: ME INTERESA 7")
            return None
        offer_id = int(id_text.strip().lstrip("#"))
        # owner_channel=TELEGRAM (2026-09-27 audit): a WhatsApp-owned offer
        # is "no existe" here, never actionable via ME INTERESA/CERRAR --
        # see get_offer_by_id's docstring.
        offer = get_offer_by_id(db, offer_id, owner_channel=Channel.TELEGRAM)
        if offer is None:
            self._send(db, identity.channel_user_id, f"No existe la oferta #{offer_id}.")
            return None
        return offer

    @staticmethod
    def _parse_positive_decimal(text: str) -> Decimal | None:
        try:
            value = Decimal(text.strip())
        except (InvalidOperation, ValueError):
            return None
        return value if value > 0 else None
