from django.contrib import admin

from apps.fuel.models import FuelStation


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = (
        "opis_id",
        "name",
        "city",
        "state",
        "retail_price",
        "location_accuracy",
        "latitude",
        "longitude",
    )
    list_filter = ("state", "location_accuracy")
    search_fields = ("name", "city", "address", "opis_id")
