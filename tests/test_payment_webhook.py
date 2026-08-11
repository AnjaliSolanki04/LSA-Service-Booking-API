"""Tests for POST /api/v1/payments/webhook/ - signature, state transitions, replays.

Author: Vansh Mehta <mehtavansh6626@gmail.com>
"""

from __future__ import annotations

import json
import time

import pytest

from apps.bookings.models import Booking, BookingStatus, Payment, PaymentStatus

pytestmark = pytest.mark.django_db

URL = "/api/v1/payments/webhook/"


def event_for(booking, event_type="payment.succeeded", event_id="evt_001", **data):
    body = {
        "id": event_id,
        "type": event_type,
        "data": {
            "booking_reference": booking.reference,
            "gateway_reference": "pi_mock_00001",
            "amount": str(booking.total_amount),
            "currency": booking.currency,
            "method": "card",
            **data,
        },
    }
    return body


def post(api_client, signed_webhook, event, **kwargs):
    body, headers = signed_webhook(event, **kwargs)
    return api_client.post(URL, data=body, content_type="application/json", **headers)


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------
def test_payment_succeeded_confirms_the_booking(api_client, signed_webhook, booking, payment):
    response = post(api_client, signed_webhook, event_for(booking))
    assert response.status_code == 200
    assert response.data["status"] == "processed"

    booking.refresh_from_db()
    payment.refresh_from_db()
    assert booking.status == BookingStatus.CONFIRMED
    assert payment.status == PaymentStatus.SUCCEEDED
    assert payment.processed_at is not None


def test_payment_failed_marks_the_booking_failed_and_frees_the_slot(
    api_client, signed_webhook, booking, payment
):
    response = post(
        api_client,
        signed_webhook,
        event_for(
            booking,
            event_type="payment.failed",
            event_id="evt_fail_1",
            failure_reason="Insufficient funds",
        ),
    )
    assert response.status_code == 200

    booking.refresh_from_db()
    payment.refresh_from_db()
    assert booking.status == BookingStatus.FAILED
    assert payment.status == PaymentStatus.FAILED
    assert payment.failure_reason == "Insufficient funds"
    # A failed booking no longer blocks the LSA's calendar.
    assert not booking.is_active


def test_refund_event_cancels_a_confirmed_booking(api_client, signed_webhook, booking, payment):
    post(api_client, signed_webhook, event_for(booking, event_id="evt_ok"))
    booking.refresh_from_db()
    assert booking.status == BookingStatus.CONFIRMED

    response = post(
        api_client,
        signed_webhook,
        event_for(booking, event_type="payment.refunded", event_id="evt_refund"),
    )
    assert response.status_code == 200

    booking.refresh_from_db()
    payment.refresh_from_db()
    assert booking.status == BookingStatus.CANCELLED
    assert payment.status == PaymentStatus.REFUNDED


def test_webhook_creates_a_payment_row_when_none_exists_locally(
    api_client, signed_webhook, booking
):
    """A charge started out-of-band must still reconcile."""
    assert not Payment.objects.filter(booking=booking).exists()

    response = post(api_client, signed_webhook, event_for(booking))
    assert response.status_code == 200

    payment = Payment.objects.get(booking=booking)
    assert payment.status == PaymentStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_replaying_the_same_event_is_a_no_op(api_client, signed_webhook, booking, payment):
    """Gateways deliver at-least-once; a replay must not corrupt state."""
    event = event_for(booking, event_id="evt_replay")

    first = post(api_client, signed_webhook, event)
    assert first.status_code == 200
    assert first.data["status"] == "processed"

    second = post(api_client, signed_webhook, event)
    assert second.status_code == 200
    assert second.data["status"] == "duplicate"

    booking.refresh_from_db()
    assert booking.status == BookingStatus.CONFIRMED
    assert Payment.objects.filter(booking=booking).count() == 1


# ---------------------------------------------------------------------------
# Security failures
# ---------------------------------------------------------------------------
def test_missing_signature_is_rejected_with_401(api_client, booking):
    response = api_client.post(
        URL, data=json.dumps(event_for(booking)), content_type="application/json"
    )
    assert response.status_code == 401
    assert response.data["error"]["code"] == "invalid_webhook_signature"


def test_signature_computed_with_the_wrong_secret_is_rejected(api_client, signed_webhook, booking):
    response = post(api_client, signed_webhook, event_for(booking), secret="wrong-secret")
    assert response.status_code == 401


def test_a_stale_timestamp_is_rejected_as_a_replay_attempt(api_client, signed_webhook, booking):
    """A signature captured yesterday must not work today."""
    old = str(int(time.time()) - 86_400)
    response = post(api_client, signed_webhook, event_for(booking), timestamp=old)
    assert response.status_code == 401


def test_tampering_with_the_body_after_signing_is_detected(api_client, signed_webhook, booking):
    body, headers = signed_webhook(event_for(booking))
    tampered = body.replace(b"payment.succeeded", b"payment.refunded!")

    response = api_client.post(URL, data=tampered, content_type="application/json", **headers)
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------
def test_unknown_booking_reference_is_acknowledged_not_retried(api_client, signed_webhook, booking):
    """202: understood, but nothing we can ever do about it."""
    event = event_for(booking)
    event["data"]["booking_reference"] = "HB-DOESNOTEXIST"

    response = post(api_client, signed_webhook, event)
    assert response.status_code == 202
    assert response.data["status"] == "ignored"


def test_unsupported_event_type_is_acknowledged(api_client, signed_webhook, booking):
    response = post(
        api_client,
        signed_webhook,
        event_for(booking, event_type="invoice.updated", event_id="evt_unsupported"),
    )
    assert response.status_code == 202


def test_malformed_json_body_is_rejected_with_400(api_client, signed_webhook):
    import hashlib
    import hmac

    from django.conf import settings

    body = b"{not json at all"
    timestamp = str(int(time.time()))
    signature = hmac.new(
        settings.PAYMENT_WEBHOOK_SECRET.encode(),
        f"{timestamp}.".encode() + body,
        hashlib.sha256,
    ).hexdigest()

    response = api_client.post(
        URL,
        data=body,
        content_type="application/json",
        HTTP_X_HABOT_SIGNATURE=signature,
        HTTP_X_HABOT_TIMESTAMP=timestamp,
    )
    assert response.status_code == 400


def test_missing_booking_reference_in_payload_is_rejected(api_client, signed_webhook, booking):
    event = event_for(booking)
    del event["data"]["booking_reference"]

    response = post(api_client, signed_webhook, event)
    assert response.status_code == 400


def test_success_on_an_already_cancelled_booking_is_flagged_not_applied(
    api_client, signed_webhook, booking, payment
):
    """Money arriving after a cancellation must not silently un-cancel it."""
    booking.transition_to(BookingStatus.CANCELLED, reason="Parent cancelled.")

    response = post(api_client, signed_webhook, event_for(booking, event_id="evt_late"))
    assert response.status_code == 200
    assert "refund review" in response.data["message"]

    booking.refresh_from_db()
    assert booking.status == BookingStatus.CANCELLED
