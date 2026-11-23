from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from farmacia.auth_forms import BoundedSetPasswordForm
from farmacia.views import (
    caja,
    ajustar_stock_lote,
    consultas_jefe_farmacia,
    eliminar_consulta_jefe,
    foto_consulta_jefe_farmacia,
    foto_receta_retenida,
    inicio_farmacia,
    recetas_retenidas,
    revertir_merma,
    resolver_excepcion_receta,
    resolver_receta_retenida,
)

app_name = "farmacia"

urlpatterns = [
    path("", inicio_farmacia, name="inicio"),
    path("caja/", caja, name="caja"),
    path(
        "password-reset/",
        auth_views.PasswordResetView.as_view(
            template_name="registration/password_reset_form.html",
            email_template_name="registration/password_reset_email.txt",
            html_email_template_name="registration/password_reset_email.html",
            subject_template_name="registration/password_reset_subject.txt",
            success_url=reverse_lazy("farmacia:password_reset_done"),
        ),
        name="password_reset",
    ),
    path(
        "password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="registration/password_reset_done.html",
        ),
        name="password_reset_done",
    ),
    path(
        "password-reset/confirm/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="registration/password_reset_confirm.html",
            form_class=BoundedSetPasswordForm,
            success_url=reverse_lazy("farmacia:password_reset_complete"),
        ),
        name="password_reset_confirm",
    ),
    path(
        "password-reset/complete/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="registration/password_reset_complete.html",
        ),
        name="password_reset_complete",
    ),
    path(
        "inventario/lotes/<int:lote_id>/ajustar/",
        ajustar_stock_lote,
        name="ajustar_stock_lote",
    ),
    path(
        "inventario/mermas/<int:merma_id>/revertir/",
        revertir_merma,
        name="revertir_merma",
    ),
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
        "consultas-jefe/<int:consulta_id>/eliminar/",
        eliminar_consulta_jefe,
        name="eliminar_consulta_jefe",
    ),
    path(
        "consultas-jefe/<int:consulta_id>/receta/",
        foto_consulta_jefe_farmacia,
        name="foto_consulta_jefe",
    ),
]
