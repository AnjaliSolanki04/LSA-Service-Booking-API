"""Client for the mock external payment gateway.

Everything that talks to the outside world is isolated here so the rest of the
codebase never imports ``requests`` directly. That gives three things:

1. One place to configure timeouts, retries and headers.
2. One place to translate transport failures into a domain exception, so a view
   never has to catch ``requests.Timeout``.
3. A single seam to mock in tests (the suite stubs HTTP with ``responses``, so
   no test ever touches the network).

Author: Anjali Solanki <anjalisolanki0104@gmail.com>
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal

import requests
from django.conf import settings

from apps.common.exceptions import PaymentGatewayError

logger = logging.getLogger(__name__)

# Transport-level failures worth retrying. A 4xx is the caller's fault and is
# never retried - retrying it just burns the gateway's rate limit.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class PaymentIntent:
    """Normalised result of creating a charge, independent of gateway wire format."""

    reference: str
    status: str
    amount: Decimal
    currency: str
    checkout_url: str | None = None
    raw: dict | None = None


class PaymentGatewayClient:
    """Thin, defensive wrapper around the mock payment provider's REST API."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = (base_url or settings.PAYMENT_GATEWAY_BASE_URL).rstrip("/")
        self.api_key = api_key or settings.PAYMENT_GATEWAY_API_KEY
        self.timeout = timeout if timeout is not None else settings.PAYMENT_GATEWAY_TIMEOUT
        self.max_retries = (
            max_retries if max_retries is not None else settings.PAYMENT_GATEWAY_MAX_RETRIES
        )
        self._session = session or requests.Session()

    # -- public API --------------------------------------------------------
    def create_payment_intent(
        self,
        *,
        booking_reference: str,
        amount: Decimal,
        currency: str = "INR",
        customer_email: str = "",
        idempotency_key: str | None = None,
    ) -> PaymentIntent:
        """Ask the gateway to open a charge for a booking.

        The idempotency key means a retry after a network timeout re-uses the
        original charge instead of double-billing the parent.
        """
        payload = {
            "reference": booking_reference,
            "amount": str(amount),
            "currency": currency,
            "customer_email": customer_email,
            "metadata": {"source": "habot-lsa-booking-api"},
        }
        headers = {"Idempotency-Key": idempotency_key or str(uuid.uuid4())}

        data = self._request("POST", "/payment_intents", json=payload, headers=headers)

        try:
            return PaymentIntent(
                reference=data["id"],
                status=data.get("status", "INITIATED"),
                amount=Decimal(str(data.get("amount", amount))),
                currency=data.get("currency", currency),
                checkout_url=data.get("checkout_url"),
                raw=data,
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.error(
                "Payment gateway returned an unusable payload for booking %s: %r",
                booking_reference,
                data,
            )
            raise PaymentGatewayError(
                "Payment gateway returned a malformed response.",
                details={"booking_reference": booking_reference},
            ) from exc

    def fetch_payment_intent(self, reference: str) -> dict:
        """Read the current server-side state of a charge (used for reconciliation)."""
        return self._request("GET", f"/payment_intents/{reference}")

    # -- transport ---------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        headers: dict | None = None,
    ) -> dict:
        """Perform an HTTP call with bounded retries and exhaustive error handling."""
        url = f"{self.base_url}{path}"
        request_headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "habot-lsa-booking-api/1.0",
        }
        request_headers.update(headers or {})

        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            started = time.monotonic()
            try:
                response = self._session.request(
                    method,
                    url,
                    json=json,
                    headers=request_headers,
                    timeout=self.timeout,
                )
            except requests.Timeout as exc:
                last_error = exc
                logger.warning(
                    "Payment gateway timeout on %s %s (attempt %s/%s)",
                    method,
                    path,
                    attempt + 1,
                    self.max_retries + 1,
                )
            except requests.ConnectionError as exc:
                last_error = exc
                logger.warning(
                    "Payment gateway connection error on %s %s (attempt %s/%s): %s",
                    method,
                    path,
                    attempt + 1,
                    self.max_retries + 1,
                    exc,
                )
            except requests.RequestException as exc:  # catch-all for requests
                last_error = exc
                logger.error("Payment gateway request failed on %s %s: %s", method, path, exc)
                break  # not worth retrying an unknown client-side failure
            else:
                elapsed_ms = (time.monotonic() - started) * 1000
                logger.info(
                    "Payment gateway %s %s -> %s in %.0fms",
                    method,
                    path,
                    response.status_code,
                    elapsed_ms,
                )

                if response.status_code in RETRYABLE_STATUS_CODES:
                    last_error = requests.HTTPError(
                        f"Gateway returned {response.status_code}", response=response
                    )
                    logger.warning(
                        "Payment gateway returned retryable status %s (attempt %s/%s)",
                        response.status_code,
                        attempt + 1,
                        self.max_retries + 1,
                    )
                elif 400 <= response.status_code < 500:
                    detail = self._safe_json(response)
                    logger.error(
                        "Payment gateway rejected %s %s with %s: %s",
                        method,
                        path,
                        response.status_code,
                        detail,
                    )
                    raise PaymentGatewayError(
                        "The payment gateway rejected the request.",
                        details={"status_code": response.status_code, "body": detail},
                    )
                else:
                    return self._safe_json(response)

            if attempt < self.max_retries:
                backoff = 0.25 * (2**attempt)  # 0.25s, 0.5s, 1s ...
                time.sleep(backoff)

        logger.error(
            "Payment gateway unreachable after %s attempts for %s %s: %s",
            self.max_retries + 1,
            method,
            path,
            last_error,
        )
        raise PaymentGatewayError(
            "Could not reach the payment gateway after multiple attempts.",
            details={"endpoint": path, "attempts": self.max_retries + 1},
        ) from last_error

    @staticmethod
    def _safe_json(response: requests.Response) -> dict:
        """Decode a JSON body without letting a bad body crash the caller."""
        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            logger.error(
                "Payment gateway returned non-JSON body (%s): %r",
                response.status_code,
                response.text[:500],
            )
            raise PaymentGatewayError("Payment gateway returned a non-JSON response.") from exc
        if not isinstance(data, dict):
            raise PaymentGatewayError("Payment gateway returned an unexpected JSON type.")
        return data


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------
def compute_webhook_signature(payload: bytes, timestamp: str, secret: str) -> str:
    """Return the hex HMAC-SHA256 of ``{timestamp}.{payload}``.

    Binding the timestamp into the signed string is what makes the replay window
    enforceable - an attacker cannot reuse yesterday's valid signature with a
    fresh timestamp header.
    """
    signed_payload = f"{timestamp}.".encode() + payload
    return hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()


def verify_webhook_signature(
    payload: bytes,
    timestamp: str,
    signature: str,
    secret: str | None = None,
    tolerance_seconds: int | None = None,
) -> bool:
    """Constant-time signature check plus a replay-window check."""
    secret = secret or settings.PAYMENT_WEBHOOK_SECRET
    tolerance = (
        tolerance_seconds
        if tolerance_seconds is not None
        else settings.PAYMENT_WEBHOOK_TOLERANCE_SECONDS
    )

    if not signature or not timestamp:
        logger.warning("Webhook rejected: missing signature or timestamp header.")
        return False

    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        logger.warning("Webhook rejected: timestamp header %r is not an integer.", timestamp)
        return False

    drift = abs(time.time() - sent_at)
    if drift > tolerance:
        logger.warning("Webhook rejected: timestamp drift of %.0fs exceeds tolerance.", drift)
        return False

    expected = compute_webhook_signature(payload, timestamp, secret)
    # compare_digest, not ==, so the comparison does not leak the secret by timing.
    if not hmac.compare_digest(expected, signature):
        logger.warning("Webhook rejected: HMAC signature mismatch.")
        return False

    return True
