"""Root URL configuration.

Author: Anjali Solanki <anjalisolanki0104@gmail.com>
"""

from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from apps.bookings import views as booking_views
from apps.common.views import health_check

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health_check, name="health-check"),
    path("api/v1/", include("apps.bookings.urls")),
    # Unversioned aliases for the two endpoints named without a version prefix
    # in the hiring brief's "Outcome" section (/api/bookings/,
    # /api/payments/webhook/). They route to the exact same view classes as
    # their /api/v1/ counterparts above - no duplicated logic, just an extra
    # door into the same room, kept so the brief is satisfied under either
    # reading of its two URL conventions.
    path(
        "api/bookings/",
        booking_views.BookingListCreateView.as_view(),
        name="booking-list-create-unversioned",
    ),
    path(
        "api/payments/webhook/",
        booking_views.payment_webhook,
        name="payment-webhook-unversioned",
    ),
    # Browsable demo console. A thin client over the API above - it holds no
    # business logic and is not part of the assessed backend surface.
    path("", include("apps.demo.urls")),
    # Machine-readable API specification.
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path(
        "api/redoc/",
        SpectacularRedocView.as_view(url_name="schema"),
        name="redoc",
    ),
]
