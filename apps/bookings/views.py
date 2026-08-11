"""API views.

The views stay deliberately thin - parse, delegate, serialise. Business rules
live in ``apps.bookings.services`` so they are unit-testable without HTTP and
reusable from management commands or background jobs.

Endpoints
---------
GET  /api/v1/lsas/search/         Search available LSAs (N+1 free)
POST /api/v1/bookings/            Create a booking (double-booking proof)
GET  /api/v1/bookings/            List bookings
GET  /api/v1/bookings/{ref}/      Retrieve one booking
POST /api/v1/payments/webhook/    Gateway callback (signed, idempotent)

Author: Vansh Mehta <mehtavansh6626@gmail.com>
"""

from __future__ import annotations

import json
import logging

from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.bookings.filters import LSAProfileFilter
from apps.bookings.models import Booking, LSAProfile
from apps.bookings.serializers import (
    BookingCreateSerializer,
    BookingReadSerializer,
    LSAProfileSerializer,
    PaymentWebhookSerializer,
)
from apps.bookings.services.booking_service import create_booking
from apps.bookings.services.payment_gateway import verify_webhook_signature
from apps.bookings.services.webhook_service import process_payment_event
from apps.common.exceptions import BookingConflictError, InvalidWebhookSignatureError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GET /api/v1/lsas/search/
# ---------------------------------------------------------------------------
@extend_schema(
    summary="Search available Learning Support Assistants",
    parameters=[
        OpenApiParameter("skills", str, description="Comma-separated skill slugs."),
        OpenApiParameter("match_all_skills", bool, description="Require every requested skill."),
        OpenApiParameter("city", str),
        OpenApiParameter("min_experience", int),
        OpenApiParameter("max_hourly_rate", float),
        OpenApiParameter("min_rating", float),
        OpenApiParameter(
            "available_from",
            str,
            description="ISO-8601 datetime; combined with available_to to exclude "
            "LSAs already booked in that window.",
        ),
        OpenApiParameter("available_to", str, description="ISO-8601 datetime."),
    ],
    responses=LSAProfileSerializer(many=True),
)
class LSASearchView(generics.ListAPIView):
    """Return LSAs matching the requested skills and availability.

    Query optimisation
    ------------------
    The naive version of this endpoint issues ``1 + N`` queries: one for the LSA
    page, then one more per LSA to load that LSA's skills when the serializer
    touches ``lsa.skills``. With a page of 20 that is 21 round trips, and the
    cost grows linearly with the page size.

    ``.with_related()`` attaches a ``Prefetch`` so Django loads *all* skills for
    the page in a single second query using ``WHERE skill_id IN (...)``. Total
    cost becomes a constant 2 queries (3 with pagination's COUNT) no matter how
    many LSAs come back.

    Availability filtering uses a correlated ``NOT EXISTS`` subquery rather than
    loading bookings into memory, so the database does the elimination and only
    matching rows cross the wire.
    """

    serializer_class = LSAProfileSerializer
    filterset_class = LSAProfileFilter
    permission_classes = [AllowAny]

    def get_queryset(self):
        # 1. Only bookable LSAs, resolved by the lsa_availability_idx index.
        queryset = LSAProfile.objects.available()

        # 2. Optional calendar filter, as a single NOT EXISTS subquery.
        start = self._parse_dt(self.request.query_params.get("available_from"))
        end = self._parse_dt(self.request.query_params.get("available_to"))
        if start and end and end > start:
            queryset = queryset.free_between(start, end)

        # 3. THE N+1 FIX - prefetch the M2M the serializer will read.
        return queryset.with_related().order_by("-rating", "full_name")

    @staticmethod
    def _parse_dt(raw: str | None):
        if not raw:
            return None
        try:
            return parse_datetime(raw)
        except (TypeError, ValueError):
            logger.info("Ignoring unparseable datetime query parameter: %r", raw)
            return None


# ---------------------------------------------------------------------------
# /api/v1/bookings/
# ---------------------------------------------------------------------------
class BookingListCreateView(APIView):
    """Create a booking, or list existing ones."""

    permission_classes = [AllowAny]

    @extend_schema(
        summary="List bookings",
        parameters=[
            OpenApiParameter("status", str),
            OpenApiParameter("parent_id", str),
            OpenApiParameter("lsa_id", str),
        ],
        responses=BookingReadSerializer(many=True),
    )
    def get(self, request):
        # with_related() collapses parent + lsa + payment into the same query,
        # so listing 100 bookings costs the same number of queries as listing 1.
        queryset = Booking.objects.with_related()

        if status_param := request.query_params.get("status"):
            queryset = queryset.filter(status=status_param.upper())
        if parent_id := request.query_params.get("parent_id"):
            queryset = queryset.filter(parent_id=parent_id)
        if lsa_id := request.query_params.get("lsa_id"):
            queryset = queryset.filter(lsa_id=lsa_id)

        serializer = BookingReadSerializer(queryset[:200], many=True)
        return Response({"count": queryset.count(), "results": serializer.data})

    @extend_schema(
        summary="Create a booking request",
        request=BookingCreateSerializer,
        responses={201: BookingReadSerializer},
        description=(
            "Validates the payload, then creates the booking under a row lock so "
            "two concurrent requests cannot double-book the same assistant. "
            "Returns 409 when the requested window overlaps an existing session."
        ),
    )
    def post(self, request):
        serializer = BookingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)  # -> 400 with a field map
        data = serializer.validated_data

        # validate_parent_id / validate_lsa_id stashed the fetched rows, so the
        # service layer does not have to look them up a second time.
        parent = serializer.context["parent"]
        lsa = serializer.context["lsa"]

        try:
            booking = create_booking(
                parent=parent,
                lsa=lsa,
                scheduled_start=data["scheduled_start"],
                scheduled_end=data["scheduled_end"],
                session_mode=data["session_mode"],
                notes=data.get("notes", ""),
                initiate_payment=data.get("initiate_payment", False),
            )
        except BookingConflictError as exc:
            # Surfaced as 409 Conflict, not 400: the payload is well-formed, it
            # is the current state of the resource that makes it impossible.
            return Response(
                {
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    }
                },
                status=status.HTTP_409_CONFLICT,
            )

        booking = Booking.objects.with_related().get(pk=booking.pk)
        return Response(BookingReadSerializer(booking).data, status=status.HTTP_201_CREATED)


@extend_schema(summary="Retrieve a booking by reference", responses=BookingReadSerializer)
class BookingDetailView(generics.RetrieveAPIView):
    serializer_class = BookingReadSerializer
    permission_classes = [AllowAny]
    lookup_field = "reference"

    def get_queryset(self):
        return Booking.objects.with_related()


# ---------------------------------------------------------------------------
# POST /api/v1/payments/webhook/
# ---------------------------------------------------------------------------
@extend_schema(
    summary="Payment gateway webhook",
    request=PaymentWebhookSerializer,
    responses={
        200: OpenApiResponse(description="Event applied, or already applied."),
        202: OpenApiResponse(description="Acknowledged but not actionable."),
        400: OpenApiResponse(description="Malformed body."),
        401: OpenApiResponse(description="Missing or invalid signature."),
    },
    parameters=[
        OpenApiParameter(
            "X-Habot-Signature",
            str,
            location=OpenApiParameter.HEADER,
            required=True,
            description="Hex HMAC-SHA256 of '{timestamp}.{raw_body}'.",
        ),
        OpenApiParameter(
            "X-Habot-Timestamp",
            str,
            location=OpenApiParameter.HEADER,
            required=True,
            description="Unix seconds; rejected outside a 300 second window.",
        ),
    ],
    description=(
        "Consumes payment.succeeded / payment.failed / payment.refunded events "
        "and transitions the associated booking. Requires a valid "
        "X-Habot-Signature HMAC-SHA256 header. Idempotent: replaying an event "
        "returns 200 without changing state."
    ),
)
@api_view(["POST"])
@permission_classes([AllowAny])
def payment_webhook(request):
    """Verify, parse and apply an inbound gateway event.

    Status code policy, chosen so the gateway retries the right things:

    * 401 - bad signature. Never retried by us; the sender is not who it claims.
    * 400 - malformed body. Retrying identical garbage would not help.
    * 202 - understood but not actionable (unknown booking, unsupported event).
            Acknowledged so the gateway stops retrying an event we will never
            be able to apply.
    * 200 - applied, or already applied.
    """
    # Signature must be checked against the *raw* bytes: re-serialising parsed
    # JSON would change key order or whitespace and break the HMAC.
    raw_body = request.body
    signature = request.headers.get("X-Habot-Signature", "")
    timestamp = request.headers.get("X-Habot-Timestamp", "")

    if not verify_webhook_signature(raw_body, timestamp, signature):
        logger.warning("Rejected webhook with an invalid or missing signature.")
        return Response(
            {
                "error": {
                    "code": InvalidWebhookSignatureError.code,
                    "message": InvalidWebhookSignatureError.message,
                    "details": None,
                }
            },
            status=status.HTTP_401_UNAUTHORIZED,
        )

    try:
        event = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Webhook body was not valid JSON: %s", exc)
        return Response(
            {
                "error": {
                    "code": "invalid_payload",
                    "message": "Request body must be valid JSON.",
                    "details": None,
                }
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    serializer = PaymentWebhookSerializer(data=event)
    serializer.is_valid(raise_exception=True)

    result = process_payment_event(event)

    if not result.handled:
        # Acknowledge so the gateway stops retrying something unactionable.
        return Response(
            {"status": "ignored", "message": result.message},
            status=status.HTTP_202_ACCEPTED,
        )

    return Response(
        {
            "status": "duplicate" if result.duplicate else "processed",
            "message": result.message,
            "booking_reference": result.booking_reference,
            "booking_status": result.booking_status,
            "payment_status": result.payment_status,
        },
        status=status.HTTP_200_OK,
    )
