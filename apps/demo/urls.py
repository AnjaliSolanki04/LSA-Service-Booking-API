"""Routes for the demo console.

Author: Vansh Mehta <mehtavansh6626@gmail.com>
"""

from django.urls import path

from apps.demo import views

app_name = "demo"

urlpatterns = [
    path("", views.console, name="console"),
    path("demo/simulate-payment/", views.simulate_payment, name="simulate-payment"),
]
