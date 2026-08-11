"""Operational endpoints that are not part of the business API."""

from __future__ import annotations

from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def health_check(request):
    """Liveness + database readiness probe used by CI and container orchestration."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        database_ok = True
    except Exception:  # pragma: no cover - only fires when the DB is down
        database_ok = False

    payload = {
        "status": "ok" if database_ok else "degraded",
        "database": "up" if database_ok else "down",
        "service": "habot-lsa-booking-api",
        "version": "1.0.0",
    }
    return JsonResponse(payload, status=200 if database_ok else 503)
