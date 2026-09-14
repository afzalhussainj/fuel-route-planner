from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.fuel.models import FuelStation
from apps.fuel.services.pipeline import load_processed_csv


class Command(BaseCommand):
    help = (
        "Import processed fuel stations from data/processed/fuel_stations.csv "
        "into the database. Idempotent via unique OPIS ID upserts."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--input",
            type=str,
            default=str(Path(settings.BASE_DIR) / "data" / "processed" / "fuel_stations.csv"),
            help="Processed stations CSV path",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete existing stations before import",
        )

    def handle(self, *args, **options) -> None:
        input_path = Path(options["input"])
        if not input_path.is_file():
            raise CommandError(
                f"Processed CSV not found: {input_path}. "
                "Run `python manage.py prepare_fuel_data` first."
            )

        records = load_processed_csv(input_path)
        if options["clear"]:
            deleted, _ = FuelStation.objects.all().delete()
            self.stdout.write(f"Cleared {deleted} existing station rows.")

        created = 0
        updated = 0
        with transaction.atomic():
            for record in records:
                defaults = {
                    "name": record["name"],
                    "address": record["address"],
                    "city": record["city"],
                    "state": record["state"],
                    "rack_id": record["rack_id"],
                    "retail_price": record["retail_price"],
                    "latitude": record["latitude"],
                    "longitude": record["longitude"],
                    "location_accuracy": record["location_accuracy"],
                    "geocode_confidence": record["geocode_confidence"],
                }
                _, was_created = FuelStation.objects.update_or_create(
                    opis_id=record["opis_id"],
                    defaults=defaults,
                )
                if was_created:
                    created += 1
                else:
                    updated += 1

        total = FuelStation.objects.count()
        with_coords = FuelStation.objects.filter(
            latitude__isnull=False, longitude__isnull=False
        ).count()
        from apps.routing.services.candidates import clear_station_records_cache

        clear_station_records_cache()
        self.stdout.write(
            self.style.SUCCESS(
                f"Import complete. created={created} updated={updated} "
                f"total={total} with_coordinates={with_coords}"
            )
        )
