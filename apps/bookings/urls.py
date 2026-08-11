"""URL routing for the bookings app (mounted under /api/v1/).

Author: Vansh Mehta <mehtavansh6626@gmail.com>
"""

from django.urls import path

from apps.bookings import views

app_name = "bookings"

urlpatterns = [
    path("lsas/search/", views.LSASearchView.as_view(), name="lsa-search"),
    path("bookings/", views.BookingListCreateView.as_view(), name="booking-list-create"),
    path(
        "bookings/<str:reference>/",
        views.BookingDetailView.as_view(),
        name="booking-detail",
    ),
    path("payments/webhook/", views.payment_webhook, name="payment-webhook"),
]
