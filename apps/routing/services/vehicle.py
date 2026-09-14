from __future__ import annotations

from decimal import Decimal

from django.conf import settings

# Assessment-fixed vehicle model (also mirrored in settings for env overrides in tests).
DEFAULT_MPG = Decimal("10")
DEFAULT_MAX_RANGE_MILES = Decimal("500")

# Origin fueling: cheapest eligible station within this route progress window.
ORIGIN_SEARCH_MILES = Decimal("30")
ORIGIN_SEARCH_MAX_MILES = Decimal("50")


def vehicle_mpg() -> Decimal:
    return Decimal(str(settings.VEHICLE_MPG))


def vehicle_max_range_miles() -> Decimal:
    return Decimal(str(settings.VEHICLE_MAX_RANGE_MILES))


def tank_capacity_gallons() -> Decimal:
    return vehicle_max_range_miles() / vehicle_mpg()


def gallons_for_miles(miles: Decimal) -> Decimal:
    return miles / vehicle_mpg()
