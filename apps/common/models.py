"""Abstract base models shared across the project."""

from __future__ import annotations

import uuid

from django.db import models


class TimeStampedModel(models.Model):
    """Adds automatic ``created_at`` / ``updated_at`` bookkeeping columns.

    Inheriting from a single abstract base keeps audit columns consistent across
    every table without repeating the field definitions (Poka-Yoke: a developer
    cannot forget to add them).
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class UUIDPrimaryKeyModel(models.Model):
    """Uses a UUID surrogate key.

    Public identifiers are UUIDs rather than sequential integers so that record
    counts are not leaked to API consumers and IDs cannot be enumerated.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True
