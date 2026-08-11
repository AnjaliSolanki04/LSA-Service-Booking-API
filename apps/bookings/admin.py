"""Django admin registration - useful for demoing the schema during the panel review."""

from django.contrib import admin

from apps.bookings.models import Booking, LSAProfile, Parent, Payment, Skill


@admin.register(Parent)
class ParentAdmin(admin.ModelAdmin):
    list_display = ("full_name", "email", "city", "child_name", "is_active")
    search_fields = ("full_name", "email", "child_name")
    list_filter = ("is_active", "city")


@admin.register(Skill)
class SkillAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name", "slug")


@admin.register(LSAProfile)
class LSAProfileAdmin(admin.ModelAdmin):
    list_display = (
        "full_name",
        "email",
        "city",
        "years_of_experience",
        "hourly_rate",
        "rating",
        "is_verified",
        "accepting_bookings",
    )
    list_filter = ("is_verified", "accepting_bookings", "is_active", "city", "skills")
    search_fields = ("full_name", "email")
    filter_horizontal = ("skills",)

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("skills")


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "parent",
        "lsa",
        "scheduled_start",
        "scheduled_end",
        "status",
        "total_amount",
    )
    list_filter = ("status", "session_mode")
    search_fields = ("reference", "parent__email", "lsa__email")
    date_hierarchy = "scheduled_start"
    readonly_fields = ("reference", "created_at", "updated_at")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("parent", "lsa")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("gateway_reference", "booking", "amount", "currency", "status", "processed_at")
    list_filter = ("status", "currency")
    search_fields = ("gateway_reference", "booking__reference")
    readonly_fields = ("created_at", "updated_at", "raw_payload")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("booking")
