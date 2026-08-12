"""Shared pytest fixtures.

Author: Anjali Solanki <anjalisolanki0104@gmail.com>
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import timedelta
from decimal import Decimal

import pytest
from django.conf import settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.bookings.models import (
    Booking,
    BookingStatus,
    LSAProfile,
    Parent,
    Payment,
    PaymentStatus,
    Skill,
)


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def skills(db) -> dict[str, Skill]:
    data = [
        ("dyslexia-support", "Dyslexia Support"),
        ("adhd-coaching", "ADHD Coaching"),
        ("autism-spectrum-support", "Autism Spectrum Support"),
        ("speech-and-language", "Speech and Language Therapy"),
    ]
    return {slug: Skill.objects.create(slug=slug, name=name) for slug, name in data}


@pytest.fixture
def parent(db) -> Parent:
    return Parent.objects.create(
        full_name="Anjali Mehta",
        email="anjali.mehta@example.test",
        phone_number="+919876543210",
        city="Bengaluru",
        child_name="Aarav",
        child_age=9,
    )


@pytest.fixture
def lsa(db, skills) -> LSAProfile:
    profile = LSAProfile.objects.create(
        full_name="Priya Nair",
        email="priya.nair@example.test",
        city="Bengaluru",
        years_of_experience=7,
        hourly_rate=Decimal("1200.00"),
        rating=Decimal("4.80"),
        is_verified=True,
        accepting_bookings=True,
    )
    profile.skills.set([skills["dyslexia-support"], skills["adhd-coaching"]])
    return profile


@pytest.fixture
def second_lsa(db, skills) -> LSAProfile:
    profile = LSAProfile.objects.create(
        full_name="Rohan Iyer",
        email="rohan.iyer@example.test",
        city="Mumbai",
        years_of_experience=3,
        hourly_rate=Decimal("800.00"),
        rating=Decimal("4.10"),
        is_verified=True,
        accepting_bookings=True,
    )
    profile.skills.set([skills["speech-and-language"]])
    return profile


@pytest.fixture
def unverified_lsa(db, skills) -> LSAProfile:
    profile = LSAProfile.objects.create(
        full_name="Kabir Bose",
        email="kabir.bose@example.test",
        city="Delhi",
        years_of_experience=1,
        hourly_rate=Decimal("500.00"),
        rating=Decimal("3.50"),
        is_verified=False,
        accepting_bookings=True,
    )
    profile.skills.set([skills["dyslexia-support"]])
    return profile


@pytest.fixture
def future_slot():
    """A clean, hour-aligned window one week from now."""
    start = (timezone.now() + timedelta(days=7)).replace(minute=0, second=0, microsecond=0)
    return start, start + timedelta(hours=1)


@pytest.fixture
def booking(db, parent, lsa, future_slot) -> Booking:
    start, end = future_slot
    return Booking.objects.create(
        parent=parent,
        lsa=lsa,
        scheduled_start=start,
        scheduled_end=end,
        total_amount=Decimal("1200.00"),
        status=BookingStatus.PENDING_PAYMENT,
    )


@pytest.fixture
def payment(db, booking) -> Payment:
    return Payment.objects.create(
        booking=booking,
        gateway_reference="pi_mock_00001",
        amount=booking.total_amount,
        currency=booking.currency,
        status=PaymentStatus.INITIATED,
    )


@pytest.fixture
def signed_webhook():
    """Return ``(body_bytes, headers)`` for a correctly signed webhook request."""

    def _build(event: dict, *, secret: str | None = None, timestamp: str | None = None):
        secret = secret or settings.PAYMENT_WEBHOOK_SECRET
        timestamp = timestamp or str(int(time.time()))
        body = json.dumps(event).encode()
        signature = hmac.new(
            secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
        ).hexdigest()
        headers = {
            "HTTP_X_HABOT_SIGNATURE": signature,
            "HTTP_X_HABOT_TIMESTAMP": timestamp,
        }
        return body, headers

    return _build
