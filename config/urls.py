from apps.common.views import HealthView
from apps.routing.views import map_tile_proxy, map_view
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="map", permanent=False), name="home"),
    path("admin/", admin.site.urls),
    path("health/", HealthView.as_view(), name="health"),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path("api/v1/", include("apps.routing.urls")),
    path("map/", map_view, name="map"),
    path(
        "map/tiles/<int:z>/<int:x>/<int:y>.png",
        map_tile_proxy,
        name="map-tiles",
    ),
]
