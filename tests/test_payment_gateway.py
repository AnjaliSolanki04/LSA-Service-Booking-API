"""Tests for the third-party payment gateway client.

Every HTTP call is stubbed with ``responses`` - the suite never touches the
network, so CI is deterministic and offline-safe.

Author: Anjali Solanki <anjalisolanki0104@gmail.com>
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import requests
import responses

from apps.bookings.services.payment_gateway import (
    PaymentGatewayClient,
    compute_webhook_signature,
    verify_webhook_signature,
)
from apps.common.exceptions import PaymentGatewayError

BASE_URL = "https://mock-pay.habotconnect.test/v1"
ENDPOINT = f"{BASE_URL}/payment_intents"


@pytest.fixture
def client():
    # Zero backoff keeps the retry tests fast.
    return PaymentGatewayClient(base_url=BASE_URL, timeout=1.0, max_retries=2)


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------
@responses.activate
def test_create_payment_intent_returns_a_normalised_object(client):
    responses.add(
        responses.POST,
        ENDPOINT,
        json={
            "id": "pi_abc123",
            "status": "INITIATED",
            "amount": "1200.00",
            "currency": "INR",
            "checkout_url": "https://mock-pay.test/checkout/pi_abc123",
        },
        status=201,
    )

    intent = client.create_payment_intent(
        booking_reference="HB-TEST0001",
        amount=Decimal("1200.00"),
        customer_email="parent@example.test",
    )

    assert intent.reference == "pi_abc123"
    assert intent.amount == Decimal("1200.00")
    assert intent.checkout_url.endswith("pi_abc123")


@responses.activate
def test_authorization_and_idempotency_headers_are_sent(client):
    responses.add(
        responses.POST,
        ENDPOINT,
        json={"id": "pi_1", "amount": "500.00", "currency": "INR"},
        status=201,
    )

    client.create_payment_intent(
        booking_reference="HB-TEST0002",
        amount=Decimal("500.00"),
        idempotency_key="key-123",
    )

    sent = responses.calls[0].request
    assert sent.headers["Authorization"].startswith("Bearer ")
    assert sent.headers["Idempotency-Key"] == "key-123"


# ---------------------------------------------------------------------------
# Failure and retry behaviour
# ---------------------------------------------------------------------------
@responses.activate
def test_a_timeout_is_retried_then_raises_a_domain_error(client):
    responses.add(responses.POST, ENDPOINT, body=requests.Timeout("timed out"))
    responses.add(responses.POST, ENDPOINT, body=requests.Timeout("timed out"))
    responses.add(responses.POST, ENDPOINT, body=requests.Timeout("timed out"))

    with pytest.raises(PaymentGatewayError):
        client.create_payment_intent(booking_reference="HB-TEST0003", amount=Decimal("100.00"))

    assert len(responses.calls) == 3  # initial + 2 retries


@responses.activate
def test_a_transient_500_is_retried_and_then_succeeds(client):
    responses.add(responses.POST, ENDPOINT, json={"error": "boom"}, status=503)
    responses.add(
        responses.POST,
        ENDPOINT,
        json={"id": "pi_after_retry", "amount": "100.00", "currency": "INR"},
        status=201,
    )

    intent = client.create_payment_intent(booking_reference="HB-TEST0004", amount=Decimal("100.00"))
    assert intent.reference == "pi_after_retry"
    assert len(responses.calls) == 2


@responses.activate
def test_a_4xx_is_not_retried_because_retrying_cannot_help(client):
    responses.add(responses.POST, ENDPOINT, json={"error": "invalid_currency"}, status=422)

    with pytest.raises(PaymentGatewayError) as exc:
        client.create_payment_intent(booking_reference="HB-TEST0005", amount=Decimal("100.00"))

    assert len(responses.calls) == 1
    assert exc.value.details["status_code"] == 422


@responses.activate
def test_a_non_json_response_raises_a_clean_domain_error(client):
    responses.add(responses.POST, ENDPOINT, body="<html>502 Bad Gateway</html>", status=200)

    with pytest.raises(PaymentGatewayError):
        client.create_payment_intent(booking_reference="HB-TEST0006", amount=Decimal("100.00"))


@responses.activate
def test_a_json_response_missing_the_id_field_is_rejected(client):
    responses.add(responses.POST, ENDPOINT, json={"amount": "100.00"}, status=201)

    with pytest.raises(PaymentGatewayError):
        client.create_payment_intent(booking_reference="HB-TEST0007", amount=Decimal("100.00"))


@responses.activate
def test_connection_error_surfaces_as_a_domain_error(client):
    responses.add(responses.POST, ENDPOINT, body=requests.ConnectionError("no route"))
    responses.add(responses.POST, ENDPOINT, body=requests.ConnectionError("no route"))
    responses.add(responses.POST, ENDPOINT, body=requests.ConnectionError("no route"))

    with pytest.raises(PaymentGatewayError):
        client.create_payment_intent(booking_reference="HB-TEST0008", amount=Decimal("100.00"))


# ---------------------------------------------------------------------------
# Booking creation resilience
# ---------------------------------------------------------------------------
@responses.activate
@pytest.mark.django_db
def test_a_gateway_outage_does_not_lose_the_booking(parent, lsa, future_slot):
    """The booking must survive so it can be retried, rather than vanishing."""
    from apps.bookings.models import BookingStatus, Payment
    from apps.bookings.services.booking_service import create_booking

    responses.add(responses.POST, ENDPOINT, body=requests.Timeout("down"))
    responses.add(responses.POST, ENDPOINT, body=requests.Timeout("down"))
    responses.add(responses.POST, ENDPOINT, body=requests.Timeout("down"))

    start, end = future_slot
    booking = create_booking(
        parent=parent,
        lsa=lsa,
        scheduled_start=start,
        scheduled_end=end,
        session_mode="ONLINE",
        initiate_payment=True,
        gateway_client=PaymentGatewayClient(base_url=BASE_URL, timeout=1.0, max_retries=2),
    )

    booking.refresh_from_db()
    assert booking.status == BookingStatus.PENDING_PAYMENT
    assert not Payment.objects.filter(booking=booking).exists()


# ---------------------------------------------------------------------------
# Signature helpers
# ---------------------------------------------------------------------------
def test_a_correctly_computed_signature_verifies():
    import time as _time

    payload = b'{"id":"evt_1"}'
    timestamp = str(int(_time.time()))
    signature = compute_webhook_signature(payload, timestamp, "test-secret")

    assert verify_webhook_signature(
        payload, timestamp, signature, secret="test-secret", tolerance_seconds=300
    )


def test_a_signature_over_different_bytes_does_not_verify():
    import time as _time

    timestamp = str(int(_time.time()))
    signature = compute_webhook_signature(b'{"id":"evt_1"}', timestamp, "test-secret")

    assert not verify_webhook_signature(
        b'{"id":"evt_2"}', timestamp, signature, secret="test-secret"
    )


def test_a_non_numeric_timestamp_header_is_rejected():
    assert not verify_webhook_signature(b"{}", "not-a-timestamp", "deadbeef", secret="test-secret")
