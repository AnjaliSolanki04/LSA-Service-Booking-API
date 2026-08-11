"""A browsable console for demonstrating the API.

This exists so the booking flow can be shown to a non-technical audience without
reading raw JSON. It is a *thin client over the real API* - every action on the
page issues an ordinary HTTP request to the same endpoints an external consumer
would call. No business logic lives here.

The payment-simulation endpoint is development-only. It exists because a real
webhook is signed by the gateway with a shared secret, which a browser cannot
hold without leaking it. Rather than weaken the webhook's security for a demo,
this view performs the signing server-side and replays the request through the
genuine signature-verified endpoint.

Author: Vansh Mehta <mehtavansh6626@gmail.com>
"""

from __future__ import annotations

import json
import logging
import time

from django.conf import settings
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.test import RequestFactory
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.bookings.models import LSAProfile, Parent, Skill
from apps.bookings.services.payment_gateway import compute_webhook_signature
from apps.bookings.views import payment_webhook

logger = logging.getLogger(__name__)


def console(request):
    """Render the demo console, seeded with the parents and skills that exist."""
    context = {
        "parents": Parent.objects.filter(is_active=True).order_by("full_name")[:50],
        "skills": Skill.objects.all().order_by("name"),
        "lsa_count": LSAProfile.objects.available().count(),
        "debug": settings.DEBUG,
    }
    return render(request, "demo/console.html", context)


@csrf_exempt
@require_POST
def simulate_payment(request):
    """Development-only: sign a gateway event and replay it through the webhook.

    Returns 404 when DEBUG is off, so this can never be reached in production.
    """
    if not settings.DEBUG:
        raise Http404("The payment simulator is only available in development.")

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Request body must be valid JSON."}, status=400)

    reference = str(payload.get("booking_reference") or "").strip()
    outcome = str(payload.get("outcome") or "succeeded").strip()

    if not reference:
        return JsonResponse({"error": "booking_reference is required."}, status=400)
    if outcome not in {"succeeded", "failed", "refunded"}:
        return JsonResponse({"error": "outcome must be succeeded, failed or refunded."}, status=400)

    # Reusing an event id is how the console demonstrates idempotency: the
    # second delivery of the same id must be recognised as a replay.
    event_id = str(payload.get("event_id") or f"evt_demo_{int(time.time() * 1000)}")

    event = {
        "id": event_id,
        "type": f"payment.{outcome}",
        "data": {
            "booking_reference": reference,
            "gateway_reference": f"pi_demo_{int(time.time())}",
            "method": "card",
        },
    }
    if outcome == "failed":
        event["data"]["failure_reason"] = payload.get(
            "failure_reason", "Card declined by issuing bank."
        )

    body = json.dumps(event).encode()
    timestamp = str(int(time.time()))
    signature = compute_webhook_signature(body, timestamp, settings.PAYMENT_WEBHOOK_SECRET)

    # Replay through the genuine endpoint so signature verification, idempotency
    # and the state machine all run exactly as they would in production.
    factory = RequestFactory()
    replayed = factory.post(
        "/api/v1/payments/webhook/",
        data=body,
        content_type="application/json",
        HTTP_X_HABOT_SIGNATURE=signature,
        HTTP_X_HABOT_TIMESTAMP=timestamp,
    )
    response = payment_webhook(replayed)
    response.render() if hasattr(response, "render") else None

    logger.info(
        "Demo console simulated payment.%s for booking %s -> %s",
        outcome,
        reference,
        response.status_code,
    )

    return JsonResponse(
        {
            "sent_event": event,
            "signature_header": f"{signature[:24]}…",
            "webhook_status": response.status_code,
            "webhook_response": json.loads(response.content.decode()),
        },
        status=200,
    )
