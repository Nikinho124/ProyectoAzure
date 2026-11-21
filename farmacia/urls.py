from django.urls import path

from farmacia.views import (
    caja,
    consultas_jefe_farmacia,
    foto_consulta_jefe_farmacia,
    foto_receta_retenida,
    inicio_farmacia,
    recetas_retenidas,
    resolver_excepcion_receta,
    resolver_receta_retenida,
)

app_name = "farmacia"

urlpatterns = [
    path("", inicio_farmacia, name="inicio"),
    path("caja/", caja, name="caja"),
    path("recetas-retenidas/", recetas_retenidas, name="recetas_retenidas"),
    path(
        "recetas-retenidas/<int:venta_id>/resolver/",
        resolver_receta_retenida,
        name="resolver_receta_retenida",
    ),
    path(
        "recetas-retenidas/<int:venta_id>/foto/",
        foto_receta_retenida,
        name="foto_receta_retenida",
    ),
    path(
        "recetas-retenidas/excepciones/<int:autorizacion_id>/resolver/",
        resolver_excepcion_receta,
        name="resolver_excepcion_receta",
    ),
    path("consultas-jefe/", consultas_jefe_farmacia, name="consultas_jefe"),
    path(
        "consultas-jefe/<int:consulta_id>/receta/",
        foto_consulta_jefe_farmacia,
        name="foto_consulta_jefe",
    ),
]
