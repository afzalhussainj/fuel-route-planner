from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.fuel.services.pipeline import prepare_fuel_data


class Command(BaseCommand):
    help = (
        "Validate, normalize, deduplicate, and geocode the fuel-price CSV into "
        "data/processed/fuel_stations.csv. Geocoding is offline/resumable and is "
        "never performed during route-planning API requests."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--input",
            type=str,
            default=str(settings.FUEL_PRICES_CSV),
            help="Source fuel-price CSV path",
        )
        parser.add_argument(
            "--output",
            type=str,
            default=str(Path(settings.BASE_DIR) / "data" / "processed" / "fuel_stations.csv"),
            help="Processed stations CSV output path",
        )
        parser.add_argument(
            "--cache",
            type=str,
            default=str(Path(settings.BASE_DIR) / "data" / "processed" / "geocode_cache.json"),
            help="Resumable geocode cache path",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Ignore cached coordinates and re-resolve locations",
        )
        parser.add_argument(
            "--skip-geoapify",
            action="store_true",
            help="Skip Geoapify batch geocoding and use city fallback only",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=1000,
            help="Geoapify batch size (max 1000)",
        )

    def handle(self, *args, **options) -> None:
        batch_size = options["batch_size"]
        if batch_size < 1 or batch_size > 1000:
            raise CommandError("--batch-size must be between 1 and 1000")

        input_path = Path(options["input"])
        if not input_path.is_file():
            raise CommandError(f"Input CSV not found: {input_path}")

        summary = prepare_fuel_data(
            input_path=input_path,
            processed_csv_path=Path(options["output"]),
            geocode_cache_path=Path(options["cache"]),
            force_geocode=options["force"],
            skip_geoapify=options["skip_geoapify"],
            batch_size=batch_size,
        )
        self.stdout.write(self.style.SUCCESS("Fuel data preparation complete."))
        self.stdout.write(json.dumps(summary, indent=2))
