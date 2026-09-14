from django.urls import path

from apps.routing.views import RoutePlanView

urlpatterns = [
    path("routes/plan/", RoutePlanView.as_view(), name="routes-plan"),
]
