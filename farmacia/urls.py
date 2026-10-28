from django.urls import path

from farmacia.views import (
    caja,
    inicio_farmacia,
    resolver_solicitud_receta,
    revisar_solicitudes_receta,
    solicitar_revision_receta,
    ver_foto_solicitud_receta,
)

app_name = "farmacia"

urlpatterns = [
    path("", inicio_farmacia, name="inicio"),
    path("caja/", caja, name="caja"),
    path("solicitar-receta/", solicitar_revision_receta, name="solicitar_receta"),
    path("recetas/solicitudes/", revisar_solicitudes_receta, name="revisar_recetas"),
    path(
        "recetas/solicitudes/<int:solicitud_id>/resolver/",
        resolver_solicitud_receta,
        name="resolver_receta",
    ),
    path(
        "recetas/solicitudes/<int:solicitud_id>/foto/",
        ver_foto_solicitud_receta,
        name="foto_receta",
    ),
]
