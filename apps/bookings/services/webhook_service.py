"""Payment webhook processing.

The gateway is an *at-least-once* delivery system: it will happily send the same
event twice if our first 200 was slow. Everything here is therefore idempotent -
processing an event a second time returns the same answer and changes nothing.

Author: Anjali Solanki <anjalisolanki0104@gmail.com>
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.db import transaction

from apps.bookings.models import Booking, BookingStatus, Payment, PaymentStatus

logger = logging.getLogger(__name__)

EVENT_PAYMENT_SUCCEEDED = "payment.succeeded"
EVENT_PAYMENT_FAILED = "payment.failed"
EVENT_PAYMENT_REFUNDED = "payment.refunded"

SUPPORTED_EVENTS = {
    EVENT_PAYMENT_SUCCEEDED,
    EVENT_PAYMENT_FAILED,
    EVENT_PAYMENT_REFUNDED,
}


@dataclass
class WebhookResult:
    """What the handler did, so the view can pick a status code and message."""

    handled: bool
    duplicate: bool = False
    booking_reference: str | None = None
    booking_status: str | None = None
    payment_status: str | None = None
    message: str = ""


@transaction.atomic
def process_payment_event(event: dict) -> WebhookResult:
    """Apply a gateway event to the local Booking / Payment state.

    Returns a :class:`WebhookResult` instead of raising for business-level
    misses (unknown booking, unsupported event). A webhook endpoint that returns
    5xx makes the gateway retry forever; for anything we will never be able to
    process, we acknowledge and record instead.
    """
    event_id = str(event.get("id") or "").strip()
    event_type = str(event.get("type") or "").strip()
    data = event.get("data") or {}
    booking_reference = str(data.get("booking_reference") or "").strip()

    if event_type not in SUPPORTED_EVENTS:
        logger.info("Ignoring unsupported webhook event type %r (id=%s)", event_type, event_id)
        return WebhookResult(
            handled=False, message=f"Event type '{event_type}' is not handled by this service."
        )

    # Idempotency guard: have we already applied this exact event?
    if event_id and Payment.objects.filter(last_event_id=event_id).exists():
        payment = Payment.objects.select_related("booking").get(last_event_id=event_id)
        logger.info("Webhook event %s already applied - returning cached outcome.", event_id)
        return WebhookResult(
            handled=True,
            duplicate=True,
            booking_reference=payment.booking.reference,
            booking_status=payment.booking.status,
            payment_status=payment.status,
            message="Event already processed.",
        )

    # Lock the booking row so two concurrent deliveries cannot interleave.
    booking = (
        Booking.objects.select_for_update()
        .filter(reference=booking_reference)
        .select_related("parent", "lsa")
        .first()
    )
    if booking is None:
        logger.warning(
            "Webhook event %s references unknown booking %r", event_id, booking_reference
        )
        return WebhookResult(
            handled=False, message=f"No booking found with reference '{booking_reference}'."
        )

    payment = _get_or_create_payment(booking, data)

    if event_type == EVENT_PAYMENT_SUCCEEDED:
        return _handle_success(booking, payment, event_id, event)
    if event_type == EVENT_PAYMENT_FAILED:
        return _handle_failure(booking, payment, event_id, event, data)
    return _handle_refund(booking, payment, event_id, event)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def _handle_success(booking, payment, event_id, event) -> WebhookResult:
    payment.mark_succeeded(event_id=event_id, payload=event)

    if booking.status == BookingStatus.CONFIRMED:
        message = "Booking was already confirmed."
    elif booking.can_transition_to(BookingStatus.CONFIRMED):
        booking.transition_to(BookingStatus.CONFIRMED)
        message = "Payment succeeded; booking confirmed."
    else:
        # e.g. the parent cancelled while the payment was in flight. Do not
        # silently resurrect it - flag it for the operations team to refund.
        logger.warning(
            "Payment succeeded for booking %s but it is %s; manual refund review needed.",
            booking.reference,
            booking.status,
        )
        message = f"Payment succeeded but booking is {booking.status}; flagged for refund review."

    return WebhookResult(
        handled=True,
        booking_reference=booking.reference,
        booking_status=booking.status,
        payment_status=payment.status,
        message=message,
    )


def _handle_failure(booking, payment, event_id, event, data) -> WebhookResult:
    reason = str(data.get("failure_reason") or "Payment declined by gateway.")
    payment.mark_failed(event_id=event_id, payload=event, reason=reason)

    if booking.status == BookingStatus.FAILED:
        message = "Booking was already marked failed."
    elif booking.can_transition_to(BookingStatus.FAILED):
        booking.transition_to(BookingStatus.FAILED, reason=reason)
        message = "Payment failed; booking marked failed and the slot released."
    else:
        logger.warning(
            "Payment failed for booking %s which is %s; leaving status untouched.",
            booking.reference,
            booking.status,
        )
        message = f"Payment failed but booking is {booking.status}; status left unchanged."

    return WebhookResult(
        handled=True,
        booking_reference=booking.reference,
        booking_status=booking.status,
        payment_status=payment.status,
        message=message,
    )


def _handle_refund(booking, payment, event_id, event) -> WebhookResult:
    payment.status = PaymentStatus.REFUNDED
    payment.last_event_id = event_id
    payment.raw_payload = event
    payment.save(update_fields=["status", "last_event_id", "raw_payload", "updated_at"])

    if booking.can_transition_to(BookingStatus.CANCELLED):
        booking.transition_to(BookingStatus.CANCELLED, reason="Payment refunded.")
        message = "Payment refunded; booking cancelled."
    else:
        message = f"Payment refunded; booking already {booking.status}."

    return WebhookResult(
        handled=True,
        booking_reference=booking.reference,
        booking_status=booking.status,
        payment_status=payment.status,
        message=message,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_or_create_payment(booking: Booking, data: dict) -> Payment:
    """Fetch the local Payment row, creating it if the charge started elsewhere."""
    payment = Payment.objects.filter(booking=booking).first()
    if payment is not None:
        return payment

    try:
        amount = Decimal(str(data.get("amount", booking.total_amount)))
    except (InvalidOperation, TypeError):
        amount = booking.total_amount

    gateway_reference = str(data.get("gateway_reference") or f"auto-{booking.reference}")
    logger.info(
        "No local payment for booking %s; creating one from webhook data (%s).",
        booking.reference,
        gateway_reference,
    )
    return Payment.objects.create(
        booking=booking,
        gateway_reference=gateway_reference,
        amount=amount,
        currency=str(data.get("currency") or booking.currency),
        status=PaymentStatus.INITIATED,
        method=str(data.get("method") or ""),
    )
