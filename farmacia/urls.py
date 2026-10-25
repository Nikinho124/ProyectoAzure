from django.urls import path

from farmacia.views import caja, inicio_farmacia

app_name = "farmacia"

urlpatterns = [
    path("", inicio_farmacia, name="inicio"),
    path("caja/", caja, name="caja"),
]
