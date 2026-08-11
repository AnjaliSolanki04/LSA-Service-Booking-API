"""Booking domain logic.

Views stay thin: they parse and serialise. Every rule about *when a booking is
allowed to exist* lives here, so the same guarantee holds whether a booking is
created by the API, a management command, or a future admin action.

Author: Vansh Mehta <mehtavansh6626@gmail.com>
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction

from apps.bookings.models import (
    Booking,
    BookingStatus,
    LSAProfile,
    Parent,
    Payment,
    PaymentStatus,
)
from apps.bookings.services.payment_gateway import PaymentGatewayClient
from apps.common.exceptions import BookingConflictError

logger = logging.getLogger(__name__)


def quote_session_price(lsa: LSAProfile, start, end) -> Decimal:
    """Price a session from the LSA's hourly rate, rounded to two decimals."""
    hours = Decimal((end - start).total_seconds()) / Decimal(3600)
    total = (lsa.hourly_rate * hours).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return total


@transaction.atomic
def create_booking(
    *,
    parent: Parent,
    lsa: LSAProfile,
    scheduled_start,
    scheduled_end,
    session_mode: str,
    notes: str = "",
    initiate_payment: bool = False,
    gateway_client: PaymentGatewayClient | None = None,
) -> Booking:
    """Create a booking, guaranteeing no overlapping session for the same LSA.

    Concurrency
    -----------
    A plain "check then insert" is a race: two requests can both read an empty
    calendar and both insert. Three layers close that gap, in order of cost:

    1. ``select_for_update`` on the LSA row serialises concurrent booking
       attempts *for that one LSA*. Requests for different LSAs never block each
       other, so throughput is unaffected.
    2. The overlap query then runs inside the same transaction, so it observes a
       consistent snapshot.
    3. A database ``UniqueConstraint`` is the final backstop. Even if the lock
       were bypassed (a different code path, a replica, a future refactor) the
       database itself refuses the duplicate, and the ``IntegrityError`` is
       translated back into the same 409 the caller would otherwise have got.

    Layer 3 is the one that actually makes the rule unbreakable; layers 1 and 2
    make the common case return a clean error instead of an exception trace.
    """
    # (1) Serialise on the LSA row for the lifetime of this transaction.
    locked_lsa = LSAProfile.objects.select_for_update().get(pk=lsa.pk)

    # (2) Look for a colliding session under that lock.
    clash = (
        Booking.objects.overlapping(locked_lsa.pk, scheduled_start, scheduled_end)
        .only("id", "reference", "scheduled_start", "scheduled_end", "status")
        .first()
    )
    if clash is not None:
        logger.info(
            "Rejected booking for LSA %s: overlaps %s (%s to %s)",
            locked_lsa.pk,
            clash.reference,
            clash.scheduled_start,
            clash.scheduled_end,
        )
        raise BookingConflictError(
            "This Learning Support Assistant already has a session booked in the "
            "requested time window.",
            details={
                "conflicting_booking_reference": clash.reference,
                "conflicting_start": clash.scheduled_start.isoformat(),
                "conflicting_end": clash.scheduled_end.isoformat(),
            },
        )

    total_amount = quote_session_price(locked_lsa, scheduled_start, scheduled_end)

    booking = Booking(
        parent=parent,
        lsa=locked_lsa,
        scheduled_start=scheduled_start,
        scheduled_end=scheduled_end,
        session_mode=session_mode,
        notes=notes,
        status=BookingStatus.PENDING_PAYMENT,
        total_amount=total_amount,
    )

    # (3) Database constraint is the real guarantee.
    try:
        with transaction.atomic():
            booking.save()
    except IntegrityError as exc:
        logger.warning(
            "Database rejected booking for LSA %s as a duplicate slot: %s",
            locked_lsa.pk,
            exc,
        )
        raise BookingConflictError(
            "This Learning Support Assistant already has a session booked in the "
            "requested time window."
        ) from exc

    logger.info(
        "Created booking %s for parent %s with LSA %s (%s)",
        booking.reference,
        parent.email,
        locked_lsa.full_name,
        booking.status,
    )

    if initiate_payment:
        _initiate_payment(booking, gateway_client=gateway_client)

    return booking


def _initiate_payment(
    booking: Booking, *, gateway_client: PaymentGatewayClient | None = None
) -> Payment | None:
    """Open a charge with the gateway and persist a local Payment record.

    A gateway outage must not lose the booking, so the failure is logged and the
    booking survives in PENDING_PAYMENT for a retry. The caller decides whether
    that is acceptable; the API surfaces it as a warning rather than a 5xx.
    """
    from apps.common.exceptions import PaymentGatewayError

    client = gateway_client or PaymentGatewayClient()
    try:
        intent = client.create_payment_intent(
            booking_reference=booking.reference,
            amount=booking.total_amount,
            currency=booking.currency,
            customer_email=booking.parent.email,
            idempotency_key=str(booking.id),
        )
    except PaymentGatewayError:
        logger.exception(
            "Could not open a payment intent for booking %s; it stays PENDING_PAYMENT.",
            booking.reference,
        )
        return None

    payment = Payment.objects.create(
        booking=booking,
        gateway_reference=intent.reference,
        amount=intent.amount,
        currency=intent.currency,
        status=PaymentStatus.INITIATED,
        raw_payload=intent.raw or {},
    )
    logger.info("Opened payment intent %s for booking %s", intent.reference, booking.reference)
    return payment
