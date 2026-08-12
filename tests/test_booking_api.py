"""Tests for POST /api/v1/bookings/ - validation and double-booking prevention.

Author: Anjali Solanki <anjalisolanki0104@gmail.com>
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.bookings.models import Booking, BookingStatus
from apps.bookings.services.booking_service import create_booking, quote_session_price
from apps.common.exceptions import BookingConflictError

pytestmark = pytest.mark.django_db

URL = "/api/v1/bookings/"


def payload_for(parent, lsa, start, end, **overrides):
    body = {
        "parent_id": str(parent.id),
        "lsa_id": str(lsa.id),
        "scheduled_start": start.isoformat(),
        "scheduled_end": end.isoformat(),
        "session_mode": "ONLINE",
        "notes": "Please focus on reading comprehension.",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Success cases
# ---------------------------------------------------------------------------
def test_creating_a_valid_booking_returns_201_with_a_reference(
    api_client, parent, lsa, future_slot
):
    start, end = future_slot
    response = api_client.post(URL, payload_for(parent, lsa, start, end), format="json")
    assert response.status_code == 201
    assert response.data["reference"].startswith("HB-")
    assert response.data["status"] == BookingStatus.PENDING_PAYMENT
    assert response.data["duration_minutes"] == 60
    assert Booking.objects.count() == 1


def test_total_amount_is_calculated_server_side_from_the_hourly_rate(
    api_client, parent, lsa, future_slot
):
    """A client cannot dictate the price - it is derived, never accepted."""
    start, end = future_slot
    end = start + timedelta(minutes=90)

    response = api_client.post(
        URL,
        payload_for(parent, lsa, start, end, total_amount="1.00"),
        format="json",
    )
    assert response.status_code == 201
    # 1.5 hours at 1200.00/hr
    assert Decimal(response.data["total_amount"]) == Decimal("1800.00")


def test_back_to_back_sessions_are_allowed(api_client, parent, lsa, future_slot):
    """Half-open intervals: 09:00-10:00 and 10:00-11:00 do not collide."""
    start, end = future_slot
    first = api_client.post(URL, payload_for(parent, lsa, start, end), format="json")
    assert first.status_code == 201

    second = api_client.post(
        URL,
        payload_for(parent, lsa, end, end + timedelta(hours=1)),
        format="json",
    )
    assert second.status_code == 201
    assert Booking.objects.count() == 2


def test_two_parents_can_book_different_lsas_in_the_same_window(
    api_client, parent, lsa, second_lsa, future_slot
):
    start, end = future_slot
    assert (
        api_client.post(URL, payload_for(parent, lsa, start, end), format="json").status_code == 201
    )
    assert (
        api_client.post(URL, payload_for(parent, second_lsa, start, end), format="json").status_code
        == 201
    )


# ---------------------------------------------------------------------------
# THE DOUBLE-BOOKING GUARD
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "offset_start_minutes,offset_end_minutes,description",
    [
        (0, 60, "exactly the same window"),
        (30, 90, "starts inside the existing session"),
        (-30, 30, "ends inside the existing session"),
        (-30, 90, "completely swallows the existing session"),
        (15, 45, "sits entirely inside the existing session"),
    ],
)
def test_overlapping_bookings_are_rejected_with_409(
    api_client, parent, lsa, future_slot, offset_start_minutes, offset_end_minutes, description
):
    """Every geometry of overlap must be caught, not just the identical case."""
    start, end = future_slot
    first = api_client.post(URL, payload_for(parent, lsa, start, end), format="json")
    assert first.status_code == 201

    clash_start = start + timedelta(minutes=offset_start_minutes)
    clash_end = start + timedelta(minutes=offset_end_minutes)

    response = api_client.post(URL, payload_for(parent, lsa, clash_start, clash_end), format="json")
    assert response.status_code == 409, f"Failed for case: {description}"
    assert response.data["error"]["code"] == "booking_conflict"
    assert Booking.objects.count() == 1


def test_conflict_response_names_the_blocking_booking(api_client, parent, lsa, future_slot):
    """The error must be actionable - tell the caller what is in the way."""
    start, end = future_slot
    first = api_client.post(URL, payload_for(parent, lsa, start, end), format="json")

    response = api_client.post(URL, payload_for(parent, lsa, start, end), format="json")
    assert response.status_code == 409
    details = response.data["error"]["details"]
    assert details["conflicting_booking_reference"] == first.data["reference"]


def test_a_cancelled_booking_releases_its_slot(api_client, parent, lsa, future_slot):
    start, end = future_slot
    first = api_client.post(URL, payload_for(parent, lsa, start, end), format="json")

    booking = Booking.objects.get(reference=first.data["reference"])
    booking.transition_to(BookingStatus.CANCELLED, reason="Parent changed plans.")

    retry = api_client.post(URL, payload_for(parent, lsa, start, end), format="json")
    assert retry.status_code == 201


def test_service_layer_raises_conflict_independently_of_the_api(parent, lsa, future_slot):
    """The rule lives in the service, so it holds outside HTTP too."""
    start, end = future_slot
    create_booking(
        parent=parent,
        lsa=lsa,
        scheduled_start=start,
        scheduled_end=end,
        session_mode="ONLINE",
    )
    with pytest.raises(BookingConflictError):
        create_booking(
            parent=parent,
            lsa=lsa,
            scheduled_start=start + timedelta(minutes=15),
            scheduled_end=end,
            session_mode="ONLINE",
        )


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------
def test_booking_in_the_past_is_rejected_with_400(api_client, parent, lsa):
    start = timezone.now() - timedelta(hours=2)
    response = api_client.post(
        URL, payload_for(parent, lsa, start, start + timedelta(hours=1)), format="json"
    )
    assert response.status_code == 400
    assert "scheduled_start" in response.data["error"]["details"]


def test_end_before_start_is_rejected_with_400(api_client, parent, lsa, future_slot):
    start, end = future_slot
    response = api_client.post(URL, payload_for(parent, lsa, end, start), format="json")
    assert response.status_code == 400
    assert "scheduled_end" in response.data["error"]["details"]


def test_session_shorter_than_the_minimum_is_rejected(api_client, parent, lsa, future_slot):
    start, _ = future_slot
    response = api_client.post(
        URL, payload_for(parent, lsa, start, start + timedelta(minutes=10)), format="json"
    )
    assert response.status_code == 400
    assert "scheduled_end" in response.data["error"]["details"]


def test_session_longer_than_the_maximum_is_rejected(api_client, parent, lsa, future_slot):
    start, _ = future_slot
    response = api_client.post(
        URL, payload_for(parent, lsa, start, start + timedelta(hours=9)), format="json"
    )
    assert response.status_code == 400


def test_booking_too_far_in_advance_is_rejected(api_client, parent, lsa):
    start = timezone.now() + timedelta(days=400)
    response = api_client.post(
        URL, payload_for(parent, lsa, start, start + timedelta(hours=1)), format="json"
    )
    assert response.status_code == 400
    assert "scheduled_start" in response.data["error"]["details"]


def test_booking_an_unverified_lsa_is_rejected(api_client, parent, unverified_lsa, future_slot):
    start, end = future_slot
    response = api_client.post(URL, payload_for(parent, unverified_lsa, start, end), format="json")
    assert response.status_code == 400
    assert "lsa_id" in response.data["error"]["details"]


def test_unknown_parent_id_is_rejected(api_client, lsa, future_slot):
    import uuid

    start, end = future_slot
    response = api_client.post(
        URL,
        {
            "parent_id": str(uuid.uuid4()),
            "lsa_id": str(lsa.id),
            "scheduled_start": start.isoformat(),
            "scheduled_end": end.isoformat(),
        },
        format="json",
    )
    assert response.status_code == 400
    assert "parent_id" in response.data["error"]["details"]


def test_missing_required_fields_returns_a_field_map(api_client):
    response = api_client.post(URL, {}, format="json")
    assert response.status_code == 400
    details = response.data["error"]["details"]
    assert {"parent_id", "lsa_id", "scheduled_start", "scheduled_end"} <= set(details)


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------
def test_booking_detail_is_retrievable_by_reference(api_client, booking):
    response = api_client.get(f"{URL}{booking.reference}/")
    assert response.status_code == 200
    assert response.data["reference"] == booking.reference
    assert response.data["lsa"]["email"] == booking.lsa.email


def test_booking_list_can_be_filtered_by_status(api_client, booking):
    response = api_client.get(URL, {"status": "pending_payment"})
    assert response.status_code == 200
    assert response.data["count"] == 1

    response = api_client.get(URL, {"status": "confirmed"})
    assert response.data["count"] == 0


def test_quote_session_price_rounds_to_two_decimals(lsa):
    start = timezone.now() + timedelta(days=1)
    price = quote_session_price(lsa, start, start + timedelta(minutes=45))
    assert price == Decimal("900.00")  # 0.75h * 1200
