from django.db import models


class FuelStation(models.Model):
    class LocationAccuracy(models.TextChoices):
        EXACT = "exact", "Exact"
        APPROXIMATE = "approximate", "Approximate"
        CITY = "city", "City centroid"

    opis_id = models.PositiveIntegerField(unique=True, db_index=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=512)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.PositiveIntegerField(null=True, blank=True)
    retail_price = models.DecimalField(max_digits=10, decimal_places=6)
    latitude = models.FloatField(null=True, blank=True, db_index=True)
    longitude = models.FloatField(null=True, blank=True, db_index=True)
    location_accuracy = models.CharField(
        max_length=16,
        choices=LocationAccuracy.choices,
        null=True,
        blank=True,
        db_index=True,
    )
    geocode_confidence = models.FloatField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["latitude", "longitude"]),
            models.Index(fields=["state", "city"]),
            models.Index(fields=["location_accuracy", "state"]),
        ]
        ordering = ["opis_id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state})"

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def is_exact_location(self) -> bool:
        return self.location_accuracy == self.LocationAccuracy.EXACT
