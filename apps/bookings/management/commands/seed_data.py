"""Populate the database with realistic demo data.

    python manage.py seed_data --lsas 40 --parents 15

Used to demo the API and to eyeball query plans on a non-trivial dataset.

Author: Anjali Solanki <anjalisolanki0104@gmail.com>
"""

from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.bookings.models import (
    Booking,
    BookingStatus,
    LSAProfile,
    Parent,
    SessionMode,
    Skill,
)

SKILLS = [
    ("dyslexia-support", "Dyslexia Support"),
    ("adhd-coaching", "ADHD Coaching"),
    ("autism-spectrum-support", "Autism Spectrum Support"),
    ("speech-and-language", "Speech and Language Therapy"),
    ("dyscalculia-support", "Dyscalculia Support"),
    ("occupational-therapy", "Occupational Therapy"),
    ("behavioural-support", "Behavioural Support"),
    ("early-years-intervention", "Early Years Intervention"),
]

CITIES = ["Bengaluru", "Mumbai", "Delhi", "Hyderabad", "Chennai", "Pune"]

FIRST_NAMES = [
    "Aarav",
    "Diya",
    "Rohan",
    "Ananya",
    "Kabir",
    "Meera",
    "Ishaan",
    "Sara",
    "Vivaan",
    "Nisha",
    "Arjun",
    "Priya",
    "Kiran",
    "Tara",
    "Dev",
    "Anjali",
]
LAST_NAMES = [
    "Sharma",
    "Mehta",
    "Iyer",
    "Reddy",
    "Patel",
    "Nair",
    "Bose",
    "Kulkarni",
]


class Command(BaseCommand):
    help = "Seed the database with demo parents, skills, LSAs and bookings."

    def add_arguments(self, parser):
        parser.add_argument("--lsas", type=int, default=30)
        parser.add_argument("--parents", type=int, default=10)
        parser.add_argument("--bookings", type=int, default=15)
        parser.add_argument("--flush", action="store_true", help="Delete existing demo rows first.")

    @transaction.atomic
    def handle(self, *args, **options):
        random.seed(20260811)

        if options["flush"]:
            Booking.objects.all().delete()
            LSAProfile.objects.all().delete()
            Parent.objects.all().delete()
            Skill.objects.all().delete()
            self.stdout.write(self.style.WARNING("Existing data cleared."))

        # -- skills --------------------------------------------------------
        skills = []
        for slug, name in SKILLS:
            skill, _ = Skill.objects.get_or_create(
                slug=slug, defaults={"name": name, "description": f"Specialist {name}."}
            )
            skills.append(skill)
        self.stdout.write(f"Skills ready: {len(skills)}")

        # -- parents -------------------------------------------------------
        parents = []
        for i in range(options["parents"]):
            name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
            parent, _ = Parent.objects.get_or_create(
                email=f"parent{i + 1}@habot-demo.test",
                defaults={
                    "full_name": name,
                    "phone_number": f"+9198{random.randint(10000000, 99999999)}",
                    "city": random.choice(CITIES),
                    "child_name": random.choice(FIRST_NAMES),
                    "child_age": random.randint(5, 16),
                },
            )
            parents.append(parent)
        self.stdout.write(f"Parents ready: {len(parents)}")

        # -- LSAs ----------------------------------------------------------
        lsas = []
        for i in range(options["lsas"]):
            name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
            lsa, created = LSAProfile.objects.get_or_create(
                email=f"lsa{i + 1}@habot-demo.test",
                defaults={
                    "full_name": name,
                    "phone_number": f"+9197{random.randint(10000000, 99999999)}",
                    "city": random.choice(CITIES),
                    "bio": f"{name} supports children with learning difficulties.",
                    "years_of_experience": random.randint(0, 18),
                    "hourly_rate": Decimal(
                        random.choice(["600.00", "850.00", "1200.00", "1500.00"])
                    ),
                    "rating": Decimal(f"{random.uniform(3.2, 5.0):.2f}"),
                    "is_verified": random.random() > 0.15,
                    "accepting_bookings": random.random() > 0.1,
                },
            )
            if created:
                lsa.skills.set(random.sample(skills, k=random.randint(1, 4)))
            lsas.append(lsa)
        self.stdout.write(f"LSAs ready: {len(lsas)}")

        # -- bookings ------------------------------------------------------
        bookable = [profile for profile in lsas if profile.is_bookable]
        created_bookings = 0
        attempts = 0
        while created_bookings < options["bookings"] and attempts < options["bookings"] * 10:
            attempts += 1
            lsa = random.choice(bookable)
            parent = random.choice(parents)
            start = timezone.now() + timedelta(
                days=random.randint(1, 30), hours=random.randint(0, 8)
            )
            start = start.replace(minute=0, second=0, microsecond=0)
            end = start + timedelta(minutes=random.choice([60, 90, 120]))

            if Booking.objects.overlapping(lsa.pk, start, end).exists():
                continue

            hours = Decimal((end - start).total_seconds()) / Decimal(3600)
            Booking.objects.create(
                parent=parent,
                lsa=lsa,
                scheduled_start=start,
                scheduled_end=end,
                session_mode=random.choice(list(SessionMode.values)),
                status=random.choice([BookingStatus.PENDING_PAYMENT, BookingStatus.CONFIRMED]),
                total_amount=(lsa.hourly_rate * hours).quantize(Decimal("0.01")),
            )
            created_bookings += 1

        self.stdout.write(f"Bookings created: {created_bookings}")
        self.stdout.write(self.style.SUCCESS("Seed complete."))
