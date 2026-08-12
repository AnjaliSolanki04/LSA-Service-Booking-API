"""Model-level tests: constraints, state machine, derived values.

Author: Anjali Solanki <anjalisolanki0104@gmail.com>
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.bookings.models import Booking, BookingStatus, Payment, PaymentStatus
from apps.common.exceptions import InvalidStateTransitionError

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Success cases
# ---------------------------------------------------------------------------
def test_booking_reference_is_generated_automatically(booking):
    """A booking always gets a human-readable reference without the caller asking."""
    assert booking.reference.startswith("HB-")
    assert len(booking.reference) == 11


def test_duration_minutes_is_derived_from_the_schedule(booking):
    assert booking.duration_minutes == 60


def test_lsa_is_bookable_only_when_all_three_flags_are_set(lsa, unverified_lsa):
    assert lsa.is_bookable is True
    assert unverified_lsa.is_bookable is False


# ---------------------------------------------------------------------------
# Failure cases - database constraints
# ---------------------------------------------------------------------------
def test_database_rejects_a_booking_that_ends_before_it_starts(parent, lsa):
    """The CheckConstraint is the backstop for a bad interval, not just clean()."""
    start = timezone.now() + timedelta(days=3)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Booking.objects.create(
                parent=parent,
                lsa=lsa,
                scheduled_start=start,
                scheduled_end=start - timedelta(hours=1),
                total_amount=Decimal("100.00"),
            )


def test_database_rejects_an_identical_active_slot_for_the_same_lsa(booking, parent):
    """uniq_active_booking_per_lsa_slot makes an exact duplicate impossible."""
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Booking.objects.create(
                parent=parent,
                lsa=booking.lsa,
                scheduled_start=booking.scheduled_start,
                scheduled_end=booking.scheduled_end,
                total_amount=Decimal("1200.00"),
                status=BookingStatus.PENDING_PAYMENT,
            )


def test_cancelling_a_booking_frees_the_exact_slot_for_reuse(booking, parent):
    """A cancelled booking must not keep blocking the calendar."""
    booking.status = BookingStatus.CANCELLED
    booking.save(update_fields=["status"])

    replacement = Booking.objects.create(
        parent=parent,
        lsa=booking.lsa,
        scheduled_start=booking.scheduled_start,
        scheduled_end=booking.scheduled_end,
        total_amount=Decimal("1200.00"),
    )
    assert replacement.pk != booking.pk


def test_rating_outside_zero_to_five_is_rejected(lsa):
    lsa.rating = Decimal("5.50")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            lsa.save(update_fields=["rating"])


# ---------------------------------------------------------------------------
# Edge cases - model validation
# ---------------------------------------------------------------------------
def test_clean_rejects_a_session_in_the_past(parent, lsa):
    past = timezone.now() - timedelta(days=1)
    instance = Booking(
        parent=parent,
        lsa=lsa,
        scheduled_start=past,
        scheduled_end=past + timedelta(hours=1),
        total_amount=Decimal("100.00"),
    )
    with pytest.raises(ValidationError) as exc:
        instance.clean()
    assert "scheduled_start" in exc.value.message_dict


def test_clean_rejects_a_session_shorter_than_the_minimum(parent, lsa):
    start = timezone.now() + timedelta(days=2)
    instance = Booking(
        parent=parent,
        lsa=lsa,
        scheduled_start=start,
        scheduled_end=start + timedelta(minutes=10),
        total_amount=Decimal("100.00"),
    )
    with pytest.raises(ValidationError) as exc:
        instance.clean()
    assert "scheduled_end" in exc.value.message_dict


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------
def test_legal_transition_is_applied_and_persisted(booking):
    booking.transition_to(BookingStatus.CONFIRMED)
    booking.refresh_from_db()
    assert booking.status == BookingStatus.CONFIRMED


def test_illegal_transition_raises_rather_than_silently_corrupting_state(booking):
    """A completed booking must never be walked back to pending."""
    booking.transition_to(BookingStatus.CONFIRMED)
    booking.transition_to(BookingStatus.COMPLETED)
    with pytest.raises(InvalidStateTransitionError):
        booking.transition_to(BookingStatus.PENDING_PAYMENT)


def test_transitioning_to_the_current_status_is_a_harmless_no_op(booking):
    booking.transition_to(BookingStatus.PENDING_PAYMENT)
    assert booking.status == BookingStatus.PENDING_PAYMENT


def test_payment_mark_failed_records_the_reason(payment):
    payment.mark_failed(event_id="evt_1", payload={}, reason="Card declined")
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.FAILED
    assert payment.failure_reason == "Card declined"


def test_overlapping_queryset_uses_half_open_intervals(booking, parent):
    """Back-to-back sessions must not be treated as a clash."""
    adjacent_start = booking.scheduled_end
    adjacent_end = adjacent_start + timedelta(hours=1)

    clashes = Booking.objects.overlapping(booking.lsa_id, adjacent_start, adjacent_end)
    assert clashes.count() == 0
