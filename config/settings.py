"""
Django settings for the HabotConnect LSA Service Booking API.

Author : Anjali Solanki
Contact: anjalisolanki0104@gmail.com

Configuration is environment-driven (12-factor). The project runs against
PostgreSQL when database credentials are supplied and silently falls back to a
local SQLite file otherwise, so a reviewer can clone and run the test suite with
zero infrastructure.
"""

from __future__ import annotations

import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "insecure-dev-key-change-me")
DEBUG = _env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [h for h in os.getenv("DJANGO_ALLOWED_HOSTS", "*").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "django_filters",
    "drf_spectacular",
    # Local
    "apps.bookings",
    "apps.demo",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# Database
#
# DATABASE_URL wins if present. Otherwise, if POSTGRES_DB is set we build a
# PostgreSQL connection from discrete variables (the docker-compose path).
# If neither exists we fall back to SQLite so `pytest` works out of the box.
# ---------------------------------------------------------------------------
if os.getenv("DATABASE_URL"):
    DATABASES = {
        "default": dj_database_url.parse(
            os.environ["DATABASE_URL"], conn_max_age=600, conn_health_checks=True
        )
    }
elif os.getenv("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["POSTGRES_DB"],
            "USER": os.getenv("POSTGRES_USER", "postgres"),
            "PASSWORD": os.getenv("POSTGRES_PASSWORD", "postgres"),
            "HOST": os.getenv("POSTGRES_HOST", "localhost"),
            "PORT": os.getenv("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 600,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# Internationalisation
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "apps.common.exceptions.habot_exception_handler",
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"anon": os.getenv("ANON_THROTTLE_RATE", "1000/hour")},
}

SPECTACULAR_SETTINGS = {
    "TITLE": "HabotConnect - LSA Service Booking API",
    "DESCRIPTION": (
        "Backend prototype connecting parents with Learning Support Assistants. "
        "Author: Anjali Solanki <anjalisolanki0104@gmail.com>"
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# ---------------------------------------------------------------------------
# Mock payment gateway (third-party integration)
# ---------------------------------------------------------------------------
PAYMENT_GATEWAY_BASE_URL = os.getenv(
    "PAYMENT_GATEWAY_BASE_URL", "https://mock-pay.habotconnect.test/v1"
)
PAYMENT_GATEWAY_API_KEY = os.getenv("PAYMENT_GATEWAY_API_KEY", "sk_test_habot_mock")
PAYMENT_GATEWAY_TIMEOUT = float(os.getenv("PAYMENT_GATEWAY_TIMEOUT", "5.0"))
PAYMENT_GATEWAY_MAX_RETRIES = int(os.getenv("PAYMENT_GATEWAY_MAX_RETRIES", "2"))

# Shared secret used to sign webhook payloads (HMAC-SHA256).
PAYMENT_WEBHOOK_SECRET = os.getenv("PAYMENT_WEBHOOK_SECRET", "whsec_habot_mock_secret")
# Reject webhooks whose timestamp is older than this many seconds (replay guard).
PAYMENT_WEBHOOK_TOLERANCE_SECONDS = int(os.getenv("PAYMENT_WEBHOOK_TOLERANCE_SECONDS", "300"))

# ---------------------------------------------------------------------------
# Business rules (Poka-Yoke: encoded once, enforced everywhere)
# ---------------------------------------------------------------------------
BOOKING_MIN_DURATION_MINUTES = int(os.getenv("BOOKING_MIN_DURATION_MINUTES", "30"))
BOOKING_MAX_DURATION_MINUTES = int(os.getenv("BOOKING_MAX_DURATION_MINUTES", "240"))
BOOKING_MAX_ADVANCE_DAYS = int(os.getenv("BOOKING_MAX_ADVANCE_DAYS", "180"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name} {module}:{lineno} - {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
    "loggers": {
        "apps": {
            "handlers": ["console"],
            "level": os.getenv("APP_LOG_LEVEL", "DEBUG" if DEBUG else "INFO"),
            "propagate": False,
        },
        "django.db.backends": {
            # Flip to DEBUG to watch the SQL the ORM emits (used to prove the
            # N+1 fix during development).
            "handlers": ["console"],
            "level": os.getenv("SQL_LOG_LEVEL", "WARNING"),
            "propagate": False,
        },
    },
}
