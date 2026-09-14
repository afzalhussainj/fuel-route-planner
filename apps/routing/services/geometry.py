from __future__ import annotations

from dataclasses import dataclass

from pyproj import Transformer
from shapely.geometry import LineString, Point, box
from shapely.ops import transform

METERS_PER_MILE = 1609.344

# Contiguous US equal-area projection for meter-accurate buffers/distances.
_TO_ALBERS = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
_FROM_ALBERS = Transformer.from_crs("EPSG:5070", "EPSG:4326", always_xy=True)


@dataclass(frozen=True, slots=True)
class ProjectedRoute:
    """Route geometry in WGS84 and EPSG:5070, with provider distance as authority."""

    geographic: LineString
    projected: LineString
    provider_distance_miles: float
    geometric_length_miles: float

    @property
    def progress_scale(self) -> float:
        """Scale geometric progress to provider trip distance."""
        if self.geometric_length_miles <= 0:
            return 1.0
        return self.provider_distance_miles / self.geometric_length_miles


def build_route_linestring(coordinates_lonlat: list[list[float]]) -> LineString:
    if len(coordinates_lonlat) < 2:
        raise ValueError("Route geometry requires at least two coordinates.")
    return LineString([(float(c[0]), float(c[1])) for c in coordinates_lonlat])


def project_to_albers(geometry):
    return transform(_TO_ALBERS.transform, geometry)


def build_projected_route(
    coordinates_lonlat: list[list[float]],
    *,
    provider_distance_miles: float,
) -> ProjectedRoute:
    geographic = build_route_linestring(coordinates_lonlat)
    projected = project_to_albers(geographic)
    geometric_length_miles = float(projected.length) / METERS_PER_MILE
    return ProjectedRoute(
        geographic=geographic,
        projected=projected,
        provider_distance_miles=float(provider_distance_miles),
        geometric_length_miles=geometric_length_miles,
    )


def geographic_bbox_with_padding(
    projected_route: LineString,
    *,
    padding_miles: float,
) -> tuple[float, float, float, float]:
    """
    Return (west, south, east, north) in WGS84.

    Pads the projected route envelope by `padding_miles`, then converts corner
    points back to geographic coordinates for a cheap station prefilter.
    """
    pad_m = padding_miles * METERS_PER_MILE
    minx, miny, maxx, maxy = projected_route.bounds
    padded = box(minx - pad_m, miny - pad_m, maxx + pad_m, maxy + pad_m)
    corners = [
        _FROM_ALBERS.transform(padded.bounds[0], padded.bounds[1]),
        _FROM_ALBERS.transform(padded.bounds[0], padded.bounds[3]),
        _FROM_ALBERS.transform(padded.bounds[2], padded.bounds[1]),
        _FROM_ALBERS.transform(padded.bounds[2], padded.bounds[3]),
    ]
    lons = [c[0] for c in corners]
    lats = [c[1] for c in corners]
    return min(lons), min(lats), max(lons), max(lats)


def point_route_metrics(
    route: ProjectedRoute,
    *,
    longitude: float,
    latitude: float,
) -> tuple[float, float]:
    """
    Return (distance_from_route_miles, progress_miles_scaled_to_provider).

    Progress is geometric projection along the route, scaled so the end of the
    line matches `provider_distance_miles`.
    """
    point = Point(_TO_ALBERS.transform(float(longitude), float(latitude)))
    distance_miles = float(route.projected.distance(point)) / METERS_PER_MILE
    along_m = float(route.projected.project(point))
    geometric_progress_miles = along_m / METERS_PER_MILE
    progress_miles = geometric_progress_miles * route.progress_scale
    return distance_miles, progress_miles


def accuracy_corridor_miles(base_corridor_miles: float, location_accuracy: str | None) -> float:
    """
    Exact coordinates use the base corridor.

    Approximate/city centroids get a modest extra tolerance but remain bounded.
    """
    accuracy = (location_accuracy or "").lower()
    if accuracy == "exact":
        return base_corridor_miles
    if accuracy == "approximate":
        return base_corridor_miles * 1.5
    if accuracy == "city":
        return base_corridor_miles * 2.0
    return base_corridor_miles * 1.5


def accuracy_rank(location_accuracy: str | None) -> int:
    """Lower is better when sorting candidates."""
    accuracy = (location_accuracy or "").lower()
    if accuracy == "exact":
        return 0
    if accuracy == "approximate":
        return 1
    if accuracy == "city":
        return 2
    return 3
