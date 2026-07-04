from django.urls import include, path

urlpatterns = [
    path("calendar/", include("stapel_calendar.urls")),
]
