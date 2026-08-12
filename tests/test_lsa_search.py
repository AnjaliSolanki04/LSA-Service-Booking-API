"""Tests for GET /api/v1/lsas/search/ - including the N+1 regression guard.

Author: Anjali Solanki <anjalisolanki0104@gmail.com>
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.bookings.models import Booking, BookingStatus, LSAProfile, Skill

pytestmark = pytest.mark.django_db

URL = "/api/v1/lsas/search/"


# ---------------------------------------------------------------------------
# Success cases
# ---------------------------------------------------------------------------
def test_search_returns_only_bookable_lsas(api_client, lsa, unverified_lsa):
    """Unverified assistants must never surface in search results."""
    response = api_client.get(URL)
    assert response.status_code == 200

    emails = {row["email"] for row in response.data["results"]}
    assert lsa.email in emails
    assert unverified_lsa.email not in emails


def test_filtering_by_a_single_skill_slug(api_client, lsa, second_lsa):
    response = api_client.get(URL, {"skills": "speech-and-language"})
    assert response.status_code == 200
    assert len(response.data["results"]) == 1
    assert response.data["results"][0]["email"] == second_lsa.email


def test_filtering_by_multiple_skills_matches_any_by_default(api_client, lsa, second_lsa):
    response = api_client.get(URL, {"skills": "adhd-coaching,speech-and-language"})
    assert response.status_code == 200
    assert len(response.data["results"]) == 2


def test_match_all_skills_requires_every_requested_skill(api_client, lsa, second_lsa):
    """ANY vs ALL is the difference between 'either' and 'both'."""
    response = api_client.get(
        URL,
        {"skills": "dyslexia-support,adhd-coaching", "match_all_skills": "true"},
    )
    assert response.status_code == 200
    assert len(response.data["results"]) == 1
    assert response.data["results"][0]["email"] == lsa.email


def test_filtering_by_city_experience_and_rate(api_client, lsa, second_lsa):
    response = api_client.get(
        URL, {"city": "bengaluru", "min_experience": 5, "max_hourly_rate": "1500.00"}
    )
    assert response.status_code == 200
    assert len(response.data["results"]) == 1
    assert response.data["results"][0]["email"] == lsa.email


def test_availability_window_excludes_an_already_booked_lsa(api_client, lsa, second_lsa, booking):
    """An LSA holding a confirmed session must disappear from that window."""
    start = booking.scheduled_start.isoformat()
    end = booking.scheduled_end.isoformat()

    response = api_client.get(URL, {"available_from": start, "available_to": end})
    assert response.status_code == 200

    emails = {row["email"] for row in response.data["results"]}
    assert lsa.email not in emails
    assert second_lsa.email in emails


def test_availability_window_ignores_cancelled_bookings(api_client, lsa, booking):
    """Cancelling frees the slot, so the LSA is searchable again."""
    booking.status = BookingStatus.CANCELLED
    booking.save(update_fields=["status"])

    response = api_client.get(
        URL,
        {
            "available_from": booking.scheduled_start.isoformat(),
            "available_to": booking.scheduled_end.isoformat(),
        },
    )
    emails = {row["email"] for row in response.data["results"]}
    assert lsa.email in emails


# ---------------------------------------------------------------------------
# THE N+1 REGRESSION GUARD
# ---------------------------------------------------------------------------
def _bulk_create_lsas(count: int, skills: dict[str, Skill]) -> None:
    """Create ``count`` bookable LSAs, each with two skills attached."""
    profiles = [
        LSAProfile(
            full_name=f"LSA Number {i}",
            email=f"bulk-lsa-{i}@example.test",
            city="Bengaluru",
            years_of_experience=i % 15,
            hourly_rate=Decimal("900.00"),
            rating=Decimal("4.00"),
            is_verified=True,
            accepting_bookings=True,
        )
        for i in range(count)
    ]
    LSAProfile.objects.bulk_create(profiles)

    through = LSAProfile.skills.through
    links = []
    skill_ids = [skills["dyslexia-support"].id, skills["adhd-coaching"].id]
    for profile in LSAProfile.objects.filter(email__startswith="bulk-lsa-"):
        for skill_id in skill_ids:
            links.append(through(lsaprofile_id=profile.id, skill_id=skill_id))
    through.objects.bulk_create(links)


def test_search_query_count_is_constant_regardless_of_result_size(
    api_client, skills, django_assert_num_queries=None
):
    """The core optimisation claim, asserted rather than described.

    Without ``prefetch_related`` the serializer would emit one extra query per
    LSA to load ``lsa.skills``. This test runs the endpoint against 5 rows and
    then against 40 rows and requires the query count to be *identical*. If
    anyone deletes ``.with_related()`` from the view, this fails immediately.
    """
    _bulk_create_lsas(5, skills)
    with CaptureQueriesContext(connection) as small:
        response = api_client.get(URL, {"page_size": 100})
        assert response.status_code == 200
    small_count = len(small.captured_queries)

    _bulk_create_lsas_offset(35, skills)
    with CaptureQueriesContext(connection) as large:
        response = api_client.get(URL, {"page_size": 100})
        assert response.status_code == 200
    large_count = len(large.captured_queries)

    assert small_count == large_count, (
        f"Query count grew from {small_count} to {large_count} as rows increased - "
        f"the N+1 problem has been reintroduced.\n"
        + "\n".join(q["sql"][:160] for q in large.captured_queries)
    )
    # Page (1) + prefetched skills (1) + pagination COUNT (1) = 3, no more.
    assert large_count <= 4


def _bulk_create_lsas_offset(count: int, skills: dict[str, Skill]) -> None:
    """Add a further batch of LSAs without colliding on the unique email."""
    profiles = [
        LSAProfile(
            full_name=f"Extra LSA {i}",
            email=f"extra-lsa-{i}@example.test",
            city="Bengaluru",
            years_of_experience=i % 15,
            hourly_rate=Decimal("950.00"),
            rating=Decimal("4.10"),
            is_verified=True,
            accepting_bookings=True,
        )
        for i in range(count)
    ]
    LSAProfile.objects.bulk_create(profiles)

    through = LSAProfile.skills.through
    skill_ids = [skills["dyslexia-support"].id, skills["adhd-coaching"].id]
    links = [
        through(lsaprofile_id=p.id, skill_id=sid)
        for p in LSAProfile.objects.filter(email__startswith="extra-lsa-")
        for sid in skill_ids
    ]
    through.objects.bulk_create(links)


def test_serializing_skills_triggers_no_additional_queries(api_client, skills):
    """Reading every nested skill after the response must cost zero extra queries."""
    _bulk_create_lsas(10, skills)

    with CaptureQueriesContext(connection) as ctx:
        response = api_client.get(URL, {"page_size": 100})
        # Force full evaluation of the nested representation.
        _ = [row["skills"] for row in response.data["results"]]

    assert len(ctx.captured_queries) <= 4


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------
def test_unknown_skill_slug_returns_an_empty_result_not_an_error(api_client, lsa):
    response = api_client.get(URL, {"skills": "underwater-basket-weaving"})
    assert response.status_code == 200
    assert response.data["results"] == []


def test_malformed_availability_datetime_is_ignored_gracefully(api_client, lsa):
    """A bad date must not 500 the search endpoint."""
    response = api_client.get(
        URL, {"available_from": "not-a-date", "available_to": "also-not-a-date"}
    )
    assert response.status_code == 200
    assert len(response.data["results"]) >= 1


def test_results_are_paginated(api_client, skills):
    _bulk_create_lsas(30, skills)
    response = api_client.get(URL)
    assert response.status_code == 200
    assert response.data["count"] == 30
    assert len(response.data["results"]) == 20  # settings.PAGE_SIZE
    assert response.data["next"] is not None
