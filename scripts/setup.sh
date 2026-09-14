#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example — set GEOAPIFY_API_KEY and DJANGO_SECRET_KEY."
fi

uv sync --group dev
uv run python manage.py migrate --noinput
uv run python manage.py check

if [ -f data/processed/fuel_stations.csv ]; then
  uv run python manage.py import_fuel_stations
fi

echo
echo "Ready. Start with:"
echo "  uv run python manage.py runserver"
echo "Health:  http://127.0.0.1:8000/health/"
echo "Swagger: http://127.0.0.1:8000/api/docs/"
echo "Map:     http://127.0.0.1:8000/map/"
echo "Postman: postman/Fuel_Route_API.postman_collection.json"
