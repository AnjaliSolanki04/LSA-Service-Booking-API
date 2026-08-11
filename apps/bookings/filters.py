"""django-filter definitions for the LSA search endpoint.

Every filter here resolves to SQL. Nothing is filtered in Python, because a
Python-side filter forces the database to ship rows it will then discard.

Author: Vansh Mehta <mehtavansh6626@gmail.com>
"""

from __future__ import annotations

import django_filters as filters

from apps.bookings.models import LSAProfile


class CommaSeparatedCharFilter(filters.BaseInFilter, filters.CharFilter):
    """Accepts ``?skills=dyslexia,adhd`` and turns it into a single SQL ``IN``."""


class LSAProfileFilter(filters.FilterSet):
    """Filters for ``GET /api/v1/lsas/search/``."""

    skills = CommaSeparatedCharFilter(
        method="filter_skills",
        help_text="Comma-separated skill slugs, e.g. dyslexia-support,adhd-coaching.",
    )
    match_all_skills = filters.BooleanFilter(
        method="noop",
        help_text="When true, only return LSAs holding every requested skill.",
    )
    city = filters.CharFilter(field_name="city", lookup_expr="iexact")
    min_experience = filters.NumberFilter(field_name="years_of_experience", lookup_expr="gte")
    max_hourly_rate = filters.NumberFilter(field_name="hourly_rate", lookup_expr="lte")
    min_rating = filters.NumberFilter(field_name="rating", lookup_expr="gte")
    verified_only = filters.BooleanFilter(method="filter_verified_only")

    class Meta:
        model = LSAProfile
        fields = [
            "skills",
            "match_all_skills",
            "city",
            "min_experience",
            "max_hourly_rate",
            "min_rating",
            "verified_only",
        ]

    def noop(self, queryset, name, value):
        """``match_all_skills`` is consumed by ``filter_skills``; declared so
        django-filter documents it and does not reject it as unknown."""
        return queryset

    def filter_skills(self, queryset, name, value):
        slugs = [s.strip() for s in value if s and s.strip()]
        if not slugs:
            return queryset
        match_all = str(self.data.get("match_all_skills", "")).lower() in {
            "1",
            "true",
            "yes",
        }
        return queryset.with_skills(slugs, match_all=match_all)

    def filter_verified_only(self, queryset, name, value):
        return queryset.filter(is_verified=True) if value else queryset
