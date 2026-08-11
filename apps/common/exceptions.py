"""Project-wide exception types and a uniform DRF error envelope.

Every error the API returns has the same shape, so clients never have to guess:

    {
      "error": {
        "code": "booking_conflict",
        "message": "This LSA already has a session in the requested window.",
        "details": {...}
      }
    }

Author: Vansh Mehta <mehtavansh6626@gmail.com>
"""

from __future__ import annotations

import logging
from typing import Any

from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain exceptions
# ---------------------------------------------------------------------------
class HabotError(Exception):
    """Base class for every error raised by the domain layer."""

    code = "habot_error"
    message = "An unexpected error occurred."

    def __init__(self, message: str | None = None, details: Any = None) -> None:
        self.message = message or self.message
        self.details = details
        super().__init__(self.message)


class BookingConflictError(HabotError):
    """Raised when a requested slot overlaps an existing active booking."""

    code = "booking_conflict"
    message = "This Learning Support Assistant is already booked for that time slot."


class PaymentGatewayError(HabotError):
    """Raised when the external payment gateway is unreachable or misbehaving."""

    code = "payment_gateway_unavailable"
    message = "The payment gateway is currently unavailable. Please retry shortly."


class InvalidWebhookSignatureError(HabotError):
    """Raised when a webhook payload fails HMAC verification."""

    code = "invalid_webhook_signature"
    message = "Webhook signature verification failed."


class InvalidStateTransitionError(HabotError):
    """Raised when a booking or payment is moved to an illegal next state."""

    code = "invalid_state_transition"
    message = "The requested state transition is not allowed."


# ---------------------------------------------------------------------------
# DRF API exceptions (carry an HTTP status code)
# ---------------------------------------------------------------------------
class ConflictAPIException(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "The request conflicts with the current state of the resource."
    default_code = "conflict"


class ServiceUnavailableAPIException(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Upstream dependency unavailable."
    default_code = "service_unavailable"


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
_DEFAULT_CODES = {
    400: "validation_error",
    401: "not_authenticated",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    429: "throttled",
    503: "service_unavailable",
}


def habot_exception_handler(exc: Exception, context: dict) -> Response | None:
    """Wrap every DRF error in the project's standard envelope."""
    # Translate domain exceptions into their HTTP equivalents first.
    if isinstance(exc, BookingConflictError):
        exc = ConflictAPIException(detail={"code": exc.code, "message": exc.message})
    elif isinstance(exc, PaymentGatewayError):
        exc = ServiceUnavailableAPIException(detail={"code": exc.code, "message": exc.message})

    response = drf_exception_handler(exc, context)
    if response is None:
        # Unhandled -> let Django's 500 machinery deal with it, but log loudly.
        logger.exception("Unhandled exception in %s", context.get("view"))
        return None

    detail = response.data
    code = _DEFAULT_CODES.get(response.status_code, "error")
    message = "Request could not be processed."
    details: Any = None

    if isinstance(detail, dict) and {"code", "message"} <= set(detail):
        code, message = detail["code"], detail["message"]
    elif isinstance(detail, dict) and "detail" in detail and len(detail) == 1:
        raw = detail["detail"]
        if isinstance(raw, dict) and {"code", "message"} <= set(raw):
            code, message = raw["code"], raw["message"]
        else:
            message = str(raw)
    elif isinstance(detail, dict):
        message = "One or more fields failed validation."
        details = detail
    else:
        message = str(detail)

    response.data = {"error": {"code": code, "message": message, "details": details}}
    logger.warning("API error %s -> %s: %s", response.status_code, code, message, exc_info=False)
    return response
