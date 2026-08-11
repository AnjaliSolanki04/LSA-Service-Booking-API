"""Relational schema for the LSA Service Booking module.

Entities
--------
Parent          A guardian who books sessions for a child.
Skill           A normalised lookup table of support specialisations.
LSAProfile      A Learning Support Assistant, linked to Skill via M2M.
Booking         A requested session between a Parent and an LSA.
Payment         The money movement attached to a Booking (1:1).

Design notes
------------
* ``Skill`` is a separate table rather than a comma-separated text column so the
  search endpoint can filter on an indexed join instead of a ``LIKE '%...%'``
  scan, which no index can serve.
* Integrity rules live at the *database* level (CheckConstraint,
  UniqueConstraint, ExclusionConstraint) as well as in the serializer. Validation
  in Python alone is advisory - two concurrent requests can both pass it. A
  database constraint is the only thing that cannot be raced.
* Indexes are declared for the exact predicates the API filters on, not
  speculatively; every index here is justified in the README.

Author: Vansh Mehta <mehtavansh6626@gmail.com>
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.common.exceptions import InvalidStateTransitionError
from apps.common.models import TimeStampedModel, UUIDPrimaryKeyModel

logger = logging.getLogger(__name__)

PHONE_VALIDATOR = RegexValidator(
    regex=r"^\+?[1-9]\d{7,14}$",
    message="Enter a valid phone number in E.164 format, e.g. +919876543210.",
)


# ---------------------------------------------------------------------------
# Choice enumerations
# ---------------------------------------------------------------------------
class BookingStatus(models.TextChoices):
    """Lifecycle of a booking request.

    PENDING_PAYMENT -> CONFIRMED   (payment.succeeded webhook)
    PENDING_PAYMENT -> FAILED      (payment.failed webhook)
    PENDING_PAYMENT -> CANCELLED   (parent cancels before paying)
    CONFIRMED       -> CANCELLED   (either party cancels)
    CONFIRMED       -> COMPLETED   (session delivered)
    """

    PENDING_PAYMENT = "PENDING_PAYMENT", "Pending Payment"
    CONFIRMED = "CONFIRMED", "Confirmed"
    COMPLETED = "COMPLETED", "Completed"
    CANCELLED = "CANCELLED", "Cancelled"
    FAILED = "FAILED", "Failed"


class PaymentStatus(models.TextChoices):
    INITIATED = "INITIATED", "Initiated"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"
    REFUNDED = "REFUNDED", "Refunded"


class SessionMode(models.TextChoices):
    ONLINE = "ONLINE", "Online"
    IN_PERSON = "IN_PERSON", "In Person"


# Statuses that occupy a slot on an LSA's calendar. A cancelled or failed
# booking frees the slot for someone else, so it is excluded from overlap checks.
BLOCKING_BOOKING_STATUSES = (
    BookingStatus.PENDING_PAYMENT,
    BookingStatus.CONFIRMED,
    BookingStatus.COMPLETED,
)

# Legal booking state transitions. Encoded as data so the rule lives in exactly
# one place and cannot drift between the API layer and the webhook handler.
ALLOWED_BOOKING_TRANSITIONS: dict[str, tuple[str, ...]] = {
    BookingStatus.PENDING_PAYMENT: (
        BookingStatus.CONFIRMED,
        BookingStatus.FAILED,
        BookingStatus.CANCELLED,
    ),
    BookingStatus.CONFIRMED: (BookingStatus.COMPLETED, BookingStatus.CANCELLED),
    BookingStatus.COMPLETED: (),
    BookingStatus.CANCELLED: (),
    BookingStatus.FAILED: (BookingStatus.PENDING_PAYMENT,),  # allow a retry
}


# ---------------------------------------------------------------------------
# Parent
# ---------------------------------------------------------------------------
class Parent(UUIDPrimaryKeyModel, TimeStampedModel):
    """A guardian who books Learning Support Assistant sessions for a child."""

    full_name = models.CharField(max_length=150)
    email = models.EmailField(unique=True, db_index=True)
    phone_number = models.CharField(max_length=16, validators=[PHONE_VALIDATOR], blank=True)
    city = models.CharField(max_length=100, blank=True, db_index=True)
    child_name = models.CharField(max_length=150, blank=True)
    child_age = models.PositiveSmallIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "parent"
        ordering = ["full_name"]
        verbose_name = "Parent"
        verbose_name_plural = "Parents"
        constraints = [
            models.CheckConstraint(
                check=Q(child_age__isnull=True) | Q(child_age__lte=25),
                name="parent_child_age_within_range",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} <{self.email}>"


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------
class Skill(TimeStampedModel):
    """A normalised support specialisation, e.g. "Dyslexia Support".

    Kept as its own table (rather than a free-text column on LSAProfile) so the
    search endpoint filters through an indexed join instead of a substring scan.
    """

    slug = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        db_table = "skill"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


# ---------------------------------------------------------------------------
# LSA profile
# ---------------------------------------------------------------------------
class LSAProfileQuerySet(models.QuerySet):
    """Query helpers for the LSA search endpoint.

    ``with_related`` is the single place that knows how to load an LSA together
    with everything the serializer will touch. Because the serializer never
    triggers its own queries, adding a hundred LSAs to the result set does not
    add a hundred queries - this is the N+1 fix.
    """

    def with_related(self) -> LSAProfileQuerySet:
        return self.prefetch_related(
            models.Prefetch("skills", queryset=Skill.objects.only("id", "slug", "name"))
        )

    def available(self) -> LSAProfileQuerySet:
        return self.filter(is_active=True, is_verified=True, accepting_bookings=True)

    def with_skills(self, slugs: list[str], match_all: bool = False):
        """Filter by skill slugs.

        ``match_all=False`` (default) -> LSA has *any* of the requested skills.
        ``match_all=True``            -> LSA has *every* requested skill.

        The ANY case is a single ``IN`` against the join table plus a DISTINCT.
        The ALL case uses a grouped count, which is still one round trip - it is
        deliberately not a chain of ``.filter()`` calls, each of which would add
        another JOIN to the same table.
        """
        if not slugs:
            return self
        if not match_all:
            return self.filter(skills__slug__in=slugs).distinct()
        return (
            self.filter(skills__slug__in=slugs)
            .annotate(_matched=models.Count("skills", distinct=True))
            .filter(_matched=len(set(slugs)))
        )

    def free_between(self, start, end) -> LSAProfileQuerySet:
        """Exclude LSAs holding a blocking booking that overlaps ``[start, end)``.

        Implemented as a single correlated ``NOT EXISTS`` subquery rather than
        fetching bookings into Python and filtering there.
        """
        clashing = Booking.objects.filter(
            lsa_id=models.OuterRef("pk"),
            status__in=BLOCKING_BOOKING_STATUSES,
            scheduled_start__lt=end,
            scheduled_end__gt=start,
        )
        return self.exclude(models.Exists(clashing))


class LSAProfile(UUIDPrimaryKeyModel, TimeStampedModel):
    """A Learning Support Assistant offering sessions on the platform."""

    full_name = models.CharField(max_length=150, db_index=True)
    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=16, validators=[PHONE_VALIDATOR], blank=True)
    bio = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True, db_index=True)

    skills = models.ManyToManyField(Skill, related_name="lsas", blank=True)

    years_of_experience = models.PositiveSmallIntegerField(
        default=0, validators=[MinValueValidator(0)]
    )
    hourly_rate = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Rate in the platform's base currency.",
    )
    rating = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Average parent rating from 0.00 to 5.00.",
    )

    is_active = models.BooleanField(default=True)
    is_verified = models.BooleanField(
        default=False, help_text="Background check and credentials confirmed."
    )
    accepting_bookings = models.BooleanField(default=True)

    objects = LSAProfileQuerySet.as_manager()

    class Meta:
        db_table = "lsa_profile"
        ordering = ["-rating", "full_name"]
        verbose_name = "LSA profile"
        verbose_name_plural = "LSA profiles"
        constraints = [
            models.CheckConstraint(
                check=Q(rating__gte=Decimal("0.00")) & Q(rating__lte=Decimal("5.00")),
                name="lsa_rating_between_zero_and_five",
            ),
            models.CheckConstraint(
                check=Q(hourly_rate__gte=Decimal("0.00")),
                name="lsa_hourly_rate_non_negative",
            ),
        ]
        indexes = [
            # Serves the availability filter, which every search request applies.
            models.Index(
                fields=["is_active", "is_verified", "accepting_bookings"],
                name="lsa_availability_idx",
            ),
            # Serves ordering + the common "cheap and experienced" filters.
            models.Index(fields=["-rating", "hourly_rate"], name="lsa_rating_rate_idx"),
            models.Index(fields=["city", "years_of_experience"], name="lsa_city_exp_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} ({self.years_of_experience}y)"

    @property
    def is_bookable(self) -> bool:
        return self.is_active and self.is_verified and self.accepting_bookings


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------
class BookingQuerySet(models.QuerySet):
    def with_related(self) -> BookingQuerySet:
        """Load a booking with its parent, LSA and payment in one round trip."""
        return self.select_related("parent", "lsa", "payment").prefetch_related("lsa__skills")

    def blocking(self) -> BookingQuerySet:
        return self.filter(status__in=BLOCKING_BOOKING_STATUSES)

    def overlapping(self, lsa_id, start, end, exclude_pk=None) -> BookingQuerySet:
        """Bookings for ``lsa_id`` that collide with the half-open ``[start, end)``.

        Two intervals overlap when ``existing.start < new.end`` and
        ``existing.end > new.start``. Treating the interval as half-open means a
        session ending at 10:00 and one starting at 10:00 are *not* a clash,
        which matches how people actually schedule back-to-back appointments.
        """
        qs = self.blocking().filter(lsa_id=lsa_id, scheduled_start__lt=end, scheduled_end__gt=start)
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        return qs


class Booking(UUIDPrimaryKeyModel, TimeStampedModel):
    """A requested session between a Parent and an LSA."""

    reference = models.CharField(
        max_length=24,
        unique=True,
        db_index=True,
        help_text="Human-friendly identifier shared with the parent, e.g. HB-7F3A9C2D.",
    )
    parent = models.ForeignKey(Parent, on_delete=models.PROTECT, related_name="bookings")
    lsa = models.ForeignKey(LSAProfile, on_delete=models.PROTECT, related_name="bookings")

    scheduled_start = models.DateTimeField()
    scheduled_end = models.DateTimeField()
    session_mode = models.CharField(
        max_length=16, choices=SessionMode.choices, default=SessionMode.ONLINE
    )
    status = models.CharField(
        max_length=20,
        choices=BookingStatus.choices,
        default=BookingStatus.PENDING_PAYMENT,
        db_index=True,
    )
    total_amount = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.00"))]
    )
    currency = models.CharField(max_length=3, default="INR")
    notes = models.TextField(blank=True)
    cancelled_reason = models.CharField(max_length=255, blank=True)

    objects = BookingQuerySet.as_manager()

    class Meta:
        db_table = "booking"
        ordering = ["-scheduled_start"]
        constraints = [
            models.CheckConstraint(
                check=Q(scheduled_end__gt=models.F("scheduled_start")),
                name="booking_end_after_start",
            ),
            models.CheckConstraint(
                check=Q(total_amount__gte=Decimal("0.00")),
                name="booking_total_amount_non_negative",
            ),
            # Belt-and-braces uniqueness: even without PostgreSQL's exclusion
            # constraint, the exact same slot can never be inserted twice for an
            # LSA while it is still active.
            models.UniqueConstraint(
                fields=["lsa", "scheduled_start", "scheduled_end"],
                condition=Q(status__in=BLOCKING_BOOKING_STATUSES),
                name="uniq_active_booking_per_lsa_slot",
            ),
        ]
        indexes = [
            # The overlap query filters on exactly these columns, in this order.
            models.Index(
                fields=["lsa", "status", "scheduled_start", "scheduled_end"],
                name="booking_overlap_idx",
            ),
            models.Index(fields=["parent", "-scheduled_start"], name="booking_parent_idx"),
            models.Index(fields=["status", "scheduled_start"], name="booking_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.reference} - {self.lsa_id} @ {self.scheduled_start:%Y-%m-%d %H:%M}"

    # -- derived values ----------------------------------------------------
    @property
    def duration_minutes(self) -> int:
        return int((self.scheduled_end - self.scheduled_start).total_seconds() // 60)

    @property
    def is_active(self) -> bool:
        return self.status in BLOCKING_BOOKING_STATUSES

    # -- behaviour ---------------------------------------------------------
    @staticmethod
    def generate_reference() -> str:
        """Return a collision-resistant, human-readable booking reference."""
        import uuid

        return f"HB-{uuid.uuid4().hex[:8].upper()}"

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in ALLOWED_BOOKING_TRANSITIONS.get(self.status, ())

    def transition_to(self, new_status: str, *, reason: str = "") -> Booking:
        """Move the booking to ``new_status``, refusing illegal transitions.

        Centralising this means the webhook handler cannot accidentally resurrect
        a cancelled booking, and no caller has to remember the state chart.
        """
        if new_status == self.status:
            logger.info(
                "Booking %s already in status %s - no-op transition",
                self.reference,
                new_status,
            )
            return self
        if not self.can_transition_to(new_status):
            raise InvalidStateTransitionError(
                f"Cannot move booking {self.reference} from {self.status} to {new_status}.",
                details={"from": self.status, "to": new_status},
            )
        previous = self.status
        self.status = new_status
        update_fields = ["status", "updated_at"]
        if reason:
            self.cancelled_reason = reason[:255]
            update_fields.append("cancelled_reason")
        self.save(update_fields=update_fields)
        logger.info("Booking %s transitioned %s -> %s", self.reference, previous, new_status)
        return self

    def clean(self) -> None:
        """Model-level validation, enforced by ``full_clean`` and the admin."""
        super().clean()
        errors: dict[str, str] = {}

        if self.scheduled_start and self.scheduled_end:
            if self.scheduled_end <= self.scheduled_start:
                errors["scheduled_end"] = "Session end must be after session start."
            else:
                minutes = self.duration_minutes
                if minutes < settings.BOOKING_MIN_DURATION_MINUTES:
                    errors["scheduled_end"] = (
                        f"A session must last at least "
                        f"{settings.BOOKING_MIN_DURATION_MINUTES} minutes."
                    )
                elif minutes > settings.BOOKING_MAX_DURATION_MINUTES:
                    errors["scheduled_end"] = (
                        f"A session cannot exceed "
                        f"{settings.BOOKING_MAX_DURATION_MINUTES} minutes."
                    )

        if self.scheduled_start:
            now = timezone.now()
            if self.scheduled_start <= now:
                errors["scheduled_start"] = "Sessions must be scheduled in the future."
            elif self.scheduled_start > now + timedelta(days=settings.BOOKING_MAX_ADVANCE_DAYS):
                errors["scheduled_start"] = (
                    f"Sessions cannot be booked more than "
                    f"{settings.BOOKING_MAX_ADVANCE_DAYS} days in advance."
                )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = self.generate_reference()
        return super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------
class Payment(UUIDPrimaryKeyModel, TimeStampedModel):
    """Money movement for a Booking. One booking has at most one payment."""

    booking = models.OneToOneField(Booking, on_delete=models.CASCADE, related_name="payment")
    gateway_reference = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text="Identifier returned by the payment gateway.",
    )
    amount = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.00"))]
    )
    currency = models.CharField(max_length=3, default="INR")
    status = models.CharField(
        max_length=16,
        choices=PaymentStatus.choices,
        default=PaymentStatus.INITIATED,
        db_index=True,
    )
    method = models.CharField(max_length=40, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    # Idempotency: the gateway may deliver the same event more than once.
    # Storing the event id with a unique index makes replays a no-op.
    last_event_id = models.CharField(
        max_length=120, unique=True, null=True, blank=True, db_index=True
    )
    raw_payload = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "payment"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                check=Q(amount__gte=Decimal("0.00")), name="payment_amount_non_negative"
            ),
        ]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="payment_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.gateway_reference} - {self.status} {self.amount} {self.currency}"

    def mark_succeeded(self, *, event_id: str, payload: dict) -> Payment:
        self.status = PaymentStatus.SUCCEEDED
        self.failure_reason = ""
        self.processed_at = timezone.now()
        self.last_event_id = event_id
        self.raw_payload = payload
        self.save(
            update_fields=[
                "status",
                "failure_reason",
                "processed_at",
                "last_event_id",
                "raw_payload",
                "updated_at",
            ]
        )
        return self

    def mark_failed(self, *, event_id: str, payload: dict, reason: str = "") -> Payment:
        self.status = PaymentStatus.FAILED
        self.failure_reason = (reason or "Payment declined by gateway.")[:255]
        self.processed_at = timezone.now()
        self.last_event_id = event_id
        self.raw_payload = payload
        self.save(
            update_fields=[
                "status",
                "failure_reason",
                "processed_at",
                "last_event_id",
                "raw_payload",
                "updated_at",
            ]
        )
        return self
