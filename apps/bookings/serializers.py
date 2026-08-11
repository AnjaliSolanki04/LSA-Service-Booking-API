"""DRF serializers - the request/response contract of the API.

Serializers validate *shape and business rules*; the service layer owns
concurrency and persistence. Keeping that boundary sharp means validation errors
come back as a clean 400 field map while genuine conflicts come back as 409.

Author: Vansh Mehta <mehtavansh6626@gmail.com>
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers

from apps.bookings.models import (
    Booking,
    LSAProfile,
    Parent,
    Payment,
    SessionMode,
    Skill,
)


# ---------------------------------------------------------------------------
# Read serializers
# ---------------------------------------------------------------------------
class SkillSerializer(serializers.ModelSerializer):
    class Meta:
        model = Skill
        fields = ["id", "slug", "name"]


class ParentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Parent
        fields = [
            "id",
            "full_name",
            "email",
            "phone_number",
            "city",
            "child_name",
            "child_age",
            "is_active",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class LSAProfileSerializer(serializers.ModelSerializer):
    """Read representation of an LSA.

    ``skills`` is a nested serializer, which is exactly the pattern that causes
    N+1 queries when the queryset is not prefetched. The view is responsible for
    calling ``.with_related()``; the query-count test in
    ``tests/test_lsa_search.py`` fails loudly if anyone removes it.
    """

    skills = SkillSerializer(many=True, read_only=True)
    is_bookable = serializers.BooleanField(read_only=True)

    class Meta:
        model = LSAProfile
        fields = [
            "id",
            "full_name",
            "email",
            "city",
            "bio",
            "skills",
            "years_of_experience",
            "hourly_rate",
            "rating",
            "is_verified",
            "accepting_bookings",
            "is_bookable",
        ]


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = [
            "id",
            "gateway_reference",
            "amount",
            "currency",
            "status",
            "method",
            "failure_reason",
            "processed_at",
        ]
        read_only_fields = fields


class BookingReadSerializer(serializers.ModelSerializer):
    parent = ParentSerializer(read_only=True)
    lsa = LSAProfileSerializer(read_only=True)
    payment = PaymentSerializer(read_only=True)
    duration_minutes = serializers.IntegerField(read_only=True)

    class Meta:
        model = Booking
        fields = [
            "id",
            "reference",
            "parent",
            "lsa",
            "scheduled_start",
            "scheduled_end",
            "duration_minutes",
            "session_mode",
            "status",
            "total_amount",
            "currency",
            "notes",
            "cancelled_reason",
            "payment",
            "created_at",
        ]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Write serializer
# ---------------------------------------------------------------------------
class BookingCreateSerializer(serializers.Serializer):
    """Validates an incoming POST /api/v1/bookings/ payload.

    Deliberately a plain ``Serializer`` rather than a ``ModelSerializer``: the
    request body is not a 1:1 mirror of the table (no status, no total_amount -
    both are derived server-side so a client cannot book a session for zero).
    """

    parent_id = serializers.UUIDField()
    lsa_id = serializers.UUIDField()
    scheduled_start = serializers.DateTimeField()
    scheduled_end = serializers.DateTimeField()
    session_mode = serializers.ChoiceField(choices=SessionMode.choices, default=SessionMode.ONLINE)
    notes = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")
    initiate_payment = serializers.BooleanField(default=False)

    # -- field-level checks ------------------------------------------------
    def validate_parent_id(self, value):
        try:
            parent = Parent.objects.get(pk=value)
        except Parent.DoesNotExist as exc:
            raise serializers.ValidationError("No parent exists with this identifier.") from exc
        if not parent.is_active:
            raise serializers.ValidationError("This parent account is deactivated.")
        self.context["parent"] = parent
        return value

    def validate_lsa_id(self, value):
        try:
            lsa = LSAProfile.objects.get(pk=value)
        except LSAProfile.DoesNotExist as exc:
            raise serializers.ValidationError(
                "No Learning Support Assistant exists with this identifier."
            ) from exc
        if not lsa.is_active:
            raise serializers.ValidationError(
                "This Learning Support Assistant is no longer active."
            )
        if not lsa.is_verified:
            raise serializers.ValidationError(
                "This Learning Support Assistant has not completed verification."
            )
        if not lsa.accepting_bookings:
            raise serializers.ValidationError(
                "This Learning Support Assistant is not accepting new bookings."
            )
        self.context["lsa"] = lsa
        return value

    # -- object-level checks -----------------------------------------------
    def validate(self, attrs):
        start = attrs["scheduled_start"]
        end = attrs["scheduled_end"]
        now = timezone.now()
        errors: dict[str, list[str]] = {}

        if end <= start:
            errors.setdefault("scheduled_end", []).append(
                "Session end must be strictly after session start."
            )
        else:
            minutes = int((end - start).total_seconds() // 60)
            if minutes < settings.BOOKING_MIN_DURATION_MINUTES:
                errors.setdefault("scheduled_end", []).append(
                    f"A session must last at least "
                    f"{settings.BOOKING_MIN_DURATION_MINUTES} minutes "
                    f"(received {minutes})."
                )
            elif minutes > settings.BOOKING_MAX_DURATION_MINUTES:
                errors.setdefault("scheduled_end", []).append(
                    f"A session cannot exceed "
                    f"{settings.BOOKING_MAX_DURATION_MINUTES} minutes "
                    f"(received {minutes})."
                )

        if start <= now:
            errors.setdefault("scheduled_start", []).append(
                "Sessions must be scheduled in the future."
            )
        elif start > now + timedelta(days=settings.BOOKING_MAX_ADVANCE_DAYS):
            errors.setdefault("scheduled_start", []).append(
                f"Sessions cannot be booked more than "
                f"{settings.BOOKING_MAX_ADVANCE_DAYS} days in advance."
            )

        if errors:
            raise serializers.ValidationError(errors)
        return attrs


# ---------------------------------------------------------------------------
# Webhook serializer
# ---------------------------------------------------------------------------
class PaymentWebhookSerializer(serializers.Serializer):
    """Shape-check for an inbound gateway event, before any state is touched."""

    id = serializers.CharField(max_length=120)
    type = serializers.CharField(max_length=60)
    data = serializers.DictField()

    def validate_data(self, value):
        if not value.get("booking_reference"):
            raise serializers.ValidationError(
                "data.booking_reference is required to route the event."
            )
        return value
