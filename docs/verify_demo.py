"""End-to-end smoke test and N+1 demonstration.

    python docs/verify_demo.py

Runs against the seeded development database using Django's test client, so it
needs no running server. Prints the actual query counts for the optimised and
deliberately de-optimised versions of the search endpoint side by side.

Author: Vansh Mehta <mehtavansh6626@gmail.com>
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.db import connection, reset_queries  # noqa: E402
from django.test import Client  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.bookings.models import LSAProfile, Parent  # noqa: E402

client = Client()


def banner(text: str) -> None:
    print(f"\n{'=' * 70}\n{text}\n{'=' * 70}")


def main() -> int:
    failures = 0
    settings.DEBUG = True  # required for connection.queries to populate

    # -- 1. health ---------------------------------------------------------
    banner("1. Health check")
    response = client.get("/health/")
    print(f"  GET /health/ -> {response.status_code} {response.json()}")
    failures += response.status_code != 200

    # -- 2. search ---------------------------------------------------------
    banner("2. LSA search")
    response = client.get("/api/v1/lsas/search/", {"skills": "dyslexia-support"})
    data = response.json()
    print(f"  GET /api/v1/lsas/search/?skills=dyslexia-support -> {response.status_code}")
    print(f"  matched {data['count']} assistants")
    if data["results"]:
        first = data["results"][0]
        print(f"  top result: {first['full_name']} - rating {first['rating']}")
        print(f"              skills: {[s['slug'] for s in first['skills']]}")
    failures += response.status_code != 200

    # -- 3. N+1 proof ------------------------------------------------------
    banner("3. N+1 query-count proof")

    reset_queries()
    client.get("/api/v1/lsas/search/")
    optimised = len(connection.queries)

    # Deliberately de-optimise by evaluating without the prefetch.
    reset_queries()
    for profile in LSAProfile.objects.available()[:20]:
        _ = [s.slug for s in profile.skills.all()]
    naive = len(connection.queries)

    print(f"  Optimised endpoint (prefetch_related): {optimised} queries")
    print(f"  Naive equivalent (no prefetch):        {naive} queries")
    print(f"  Reduction: {naive - optimised} fewer round trips for the same data")
    if optimised > 5:
        print("  FAIL: optimised path used more queries than expected")
        failures += 1

    # -- 4. booking --------------------------------------------------------
    banner("4. Create a booking")
    parent = Parent.objects.first()
    lsa = LSAProfile.objects.available().first()
    start = (timezone.now() + timedelta(days=45)).replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=1)

    payload = {
        "parent_id": str(parent.id),
        "lsa_id": str(lsa.id),
        "scheduled_start": start.isoformat(),
        "scheduled_end": end.isoformat(),
        "session_mode": "ONLINE",
        "notes": "Smoke test booking.",
    }
    response = client.post(
        "/api/v1/bookings/", data=json.dumps(payload), content_type="application/json"
    )
    print(f"  POST /api/v1/bookings/ -> {response.status_code}")
    if response.status_code != 201:
        print(f"  FAIL: {response.json()}")
        return failures + 1

    booking = response.json()
    reference = booking["reference"]
    print(f"  reference:    {reference}")
    print(f"  status:       {booking['status']}")
    print(f"  total_amount: {booking['total_amount']} {booking['currency']}")

    # -- 5. double booking -------------------------------------------------
    banner("5. Double-booking rejection")
    overlap = dict(payload, scheduled_start=(start + timedelta(minutes=30)).isoformat())
    response = client.post(
        "/api/v1/bookings/", data=json.dumps(overlap), content_type="application/json"
    )
    print(f"  POST overlapping slot -> {response.status_code} (expected 409)")
    body = response.json()
    print(f"  code:    {body['error']['code']}")
    print(f"  blocked by: {body['error']['details']['conflicting_booking_reference']}")
    failures += response.status_code != 409

    # -- 6. adjacent booking allowed --------------------------------------
    banner("6. Back-to-back booking allowed")
    adjacent = dict(
        payload,
        scheduled_start=end.isoformat(),
        scheduled_end=(end + timedelta(hours=1)).isoformat(),
    )
    response = client.post(
        "/api/v1/bookings/", data=json.dumps(adjacent), content_type="application/json"
    )
    print(f"  POST adjacent slot -> {response.status_code} (expected 201)")
    failures += response.status_code != 201

    # -- 7. webhook --------------------------------------------------------
    banner("7. Payment webhook confirms the booking")
    event = {
        "id": f"evt_smoke_{int(time.time())}",
        "type": "payment.succeeded",
        "data": {
            "booking_reference": reference,
            "gateway_reference": f"pi_smoke_{int(time.time())}",
            "amount": booking["total_amount"],
            "currency": booking["currency"],
            "method": "card",
        },
    }
    body_bytes = json.dumps(event).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        settings.PAYMENT_WEBHOOK_SECRET.encode(),
        f"{timestamp}.".encode() + body_bytes,
        hashlib.sha256,
    ).hexdigest()

    response = client.post(
        "/api/v1/payments/webhook/",
        data=body_bytes,
        content_type="application/json",
        HTTP_X_HABOT_SIGNATURE=signature,
        HTTP_X_HABOT_TIMESTAMP=timestamp,
    )
    print(f"  POST signed webhook -> {response.status_code}")
    print(f"  {response.json()}")
    failures += response.status_code != 200

    # -- 8. replay ---------------------------------------------------------
    banner("8. Webhook replay is idempotent")
    response = client.post(
        "/api/v1/payments/webhook/",
        data=body_bytes,
        content_type="application/json",
        HTTP_X_HABOT_SIGNATURE=signature,
        HTTP_X_HABOT_TIMESTAMP=timestamp,
    )
    print(f"  POST same event again -> {response.status_code}")
    print(f"  status: {response.json()['status']} (expected 'duplicate')")
    failures += response.json().get("status") != "duplicate"

    # -- 9. unsigned webhook ----------------------------------------------
    banner("9. Unsigned webhook is rejected")
    response = client.post(
        "/api/v1/payments/webhook/", data=body_bytes, content_type="application/json"
    )
    print(f"  POST without signature -> {response.status_code} (expected 401)")
    failures += response.status_code != 401

    # -- 10. detail --------------------------------------------------------
    banner("10. Booking detail reflects the confirmation")
    response = client.get(f"/api/v1/bookings/{reference}/")
    detail = response.json()
    print(f"  GET /api/v1/bookings/{reference}/ -> {response.status_code}")
    print(f"  booking status: {detail['status']}")
    print(f"  payment status: {detail['payment']['status'] if detail['payment'] else None}")
    failures += detail["status"] != "CONFIRMED"

    banner("RESULT")
    if failures:
        print(f"  {failures} check(s) FAILED")
    else:
        print("  All checks passed.")
    return failures


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
