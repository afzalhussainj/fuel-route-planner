#!/bin/sh
set -eu

echo "Applying migrations..."
uv run --no-sync python manage.py migrate --noinput

if [ -f data/processed/fuel_stations.csv ]; then
  if ! uv run --no-sync python -c "
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()
from apps.fuel.models import FuelStation
raise SystemExit(0 if FuelStation.objects.exists() else 1)
"; then
    echo "Importing processed fuel stations..."
    uv run --no-sync python manage.py import_fuel_stations
  fi
fi

exec "$@"
