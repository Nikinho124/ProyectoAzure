from datetime import timedelta

from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm as DjangoUserChangeForm
from django.contrib.auth.models import Group, Permission
from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, IntegerField, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.html import format_html
from django.views.decorators.http import require_POST

from farmacia.models import (
    AutorizacionExcepcional,
    IngresoInventario,
    Medicamento,
    Merma,
    SimulacionPago,
    SolicitudCuenta,
    VentaRecetaRetenida,
)

User = get_user_model()

ACCIONES_PERMISO = {
    "add": "Crear",
    "change": "Editar",
    "delete": "Eliminar",
    "view": "Ver",
}

NOMBRES_MODELO = {
    ("admin", "logentry"): "registro de administración",
    ("auth", "group"): "grupo de acceso",
    ("auth", "permission"): "permiso",
    ("auth", "user"): "usuario",
    ("contenttypes", "contenttype"): "tipo de contenido",
    ("sessions", "session"): "sesión",
}


class PermisosConNombreClaro(forms.ModelMultipleChoiceField):
    def label_from_instance(self, permission):
        partes = permission.codename.split("_", 1)
        if len(partes) != 2 or partes[0] not in ACCIONES_PERMISO:
            return permission.name

        accion, nombre_modelo = partes
        modelo = permission.content_type.model_class()
        clave = (permission.content_type.app_label, permission.content_type.model)
        nombre = NOMBRES_MODELO.get(
            clave,
            str(modelo._meta.verbose_name)
            if modelo is not None
            else nombre_modelo.replace("_", " "),
        )
        return f"{ACCIONES_PERMISO[accion]} {nombre}"


class FarmaUserChangeForm(DjangoUserChangeForm):
    user_permissions = PermisosConNombreClaro(
        queryset=Permission.objects.select_related("content_type").order_by(
            "content_type__app_label",
            "content_type__model",
            "codename",
        ),
        required=False,
        label="Permisos asignados directamente",
        help_text=(
            "Concede permisos puntuales a esta cuenta. Para asignar varios "
            "permisos por función, utiliza los grupos de acceso."
        ),
    )


class FarmaUserAdmin(DjangoUserAdmin):
    form = FarmaUserChangeForm
    fieldsets = (
        ("Acceso a la cuenta", {"fields": ("username", "password")}),
        ("Información personal", {"fields": ("first_name", "last_name", "email")}),
        (
            "Estado y permisos",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Fechas de actividad", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            "Crear cuenta de usuario",
            {
                "classes": ("wide",),
                "fields": ("username", "password1", "password2"),
            },
        ),
    )


class FarmaGroupChangeForm(forms.ModelForm):
    permissions = PermisosConNombreClaro(
        queryset=Permission.objects.select_related("content_type").order_by(
            "content_type__app_label",
            "content_type__model",
            "codename",
        ),
        required=False,
        label="Permisos incluidos en este grupo",
        help_text=(
            "Selecciona las acciones que podrán realizar los usuarios de este grupo. "
            "Los nombres describen la acción y el tipo de registro."
        ),
    )

    class Meta:
        model = Group
        fields = ("name", "permissions")
        labels = {"name": "Nombre del grupo"}


class FarmaGroupAdmin(admin.ModelAdmin):
    form = FarmaGroupChangeForm
    filter_horizontal = ("permissions",)
    list_display = ("name",)
    search_fields = ("name",)
    ordering = ("name",)
    verbose_name_plural = "Grupos de acceso"


def _ingresos_con_stock_disponible():
    mermas = (
        Merma.objects.filter(ingreso_id=OuterRef("pk"))
        .values("ingreso_id")
        .annotate(total=Sum("cantidad"))
        .values("total")[:1]
    )
    return IngresoInventario.objects.annotate(
        stock_restante=F("cantidad")
        - Coalesce(
            Subquery(mermas, output_field=IntegerField()),
            Value(0),
        )
    )


class FarmaAdminSite(admin.AdminSite):
    site_header = "FarmaSistem · Administración"
    site_title = "Administración de FarmaSistem"
    index_title = "Resumen de la farmacia"
    index_template = "admin/farmacia_index.html"

    def get_app_list(self, request, app_label=None):
        app_list = super().get_app_list(request, app_label)
        for app in app_list:
            if app["app_label"] != "auth":
                continue
            app["name"] = "Usuarios y permisos"
            for model in app["models"]:
                if model["object_name"] == "User":
                    model["name"] = "Usuarios"
                elif model["object_name"] == "Group":
                    model["name"] = "Grupos de acceso"
        return app_list

    def index(self, request, extra_context=None):
        hoy = timezone.localdate()
        hasta_30_dias = hoy + timedelta(days=30)
        ingresos = _ingresos_con_stock_disponible().filter(stock_restante__gt=0)
        lotes_vigentes = IngresoInventario.objects.filter(
            fecha_vencimiento__gte=hoy
        )
        medicamentos = Medicamento.objects.annotate(
            _ingresado=Coalesce(
                Subquery(
                    lotes_vigentes.filter(medicamento_id=OuterRef("pk"))
                    .values("medicamento_id")
                    .annotate(total=Sum("cantidad"))
                    .values("total")[:1],
                    output_field=IntegerField(),
                ),
                Value(0),
            ),
            _mermado=Coalesce(
                Subquery(
                    Merma.objects.filter(
                        ingreso__in=lotes_vigentes,
                        ingreso__medicamento_id=OuterRef("pk"),
                    )
                    .values("ingreso__medicamento_id")
                    .annotate(total=Sum("cantidad"))
                    .values("total")[:1],
                    output_field=IntegerField(),
                ),
                Value(0),
            ),
        ).annotate(_stock_actual=F("_ingresado") - F("_mermado"))

        extra_context = {
            **(extra_context or {}),
            "resumen_farmacia": {
                "medicamentos_activos": Medicamento.objects.filter(activo=True).count(),
                "stock_bajo": medicamentos.filter(
                    activo=True,
                    _stock_actual__lte=F("stock_minimo"),
                ).count(),
                "vencidos": ingresos.filter(fecha_vencimiento__lt=hoy).count(),
                "por_vencer": ingresos.filter(
                    fecha_vencimiento__gte=hoy,
                    fecha_vencimiento__lte=hasta_30_dias,
                ).count(),
                "quimicos_pendientes": SolicitudCuenta.objects.filter(
                    rol_solicitado=SolicitudCuenta.RolSolicitado.FARMACEUTICO,
                    estado=SolicitudCuenta.Estado.PENDIENTE,
                ).count(),
                "enlaces": {
                    "medicamentos": reverse(
                        "admin:farmacia_medicamento_changelist"
                    ),
                    "ingresos": reverse(
                        "admin:farmacia_ingresoinventario_changelist"
                    ),
                    "mermas": reverse("admin:farmacia_merma_changelist"),
                    "solicitudes": reverse(
                        "admin:farmacia_solicitudcuenta_changelist"
                    ),
                    "caja": reverse("farmacia:caja"),
                },
            },
        }
        return super().index(request, extra_context)


farmacia_admin_site = FarmaAdminSite(name="admin")
farmacia_admin_site.register(User, FarmaUserAdmin)
farmacia_admin_site.register(Group, FarmaGroupAdmin)


class StockBajoFilter(admin.SimpleListFilter):
    title = "nivel de stock"
    parameter_name = "stock"

    def lookups(self, request, model_admin):
        return (("bajo", "En stock mínimo o bajo"),)

    def queryset(self, request, queryset):
        if self.value() == "bajo":
            return queryset.filter(_stock_actual__lte=F("stock_minimo"))
        return queryset


@admin.register(Medicamento, site=farmacia_admin_site)
class MedicamentoAdmin(admin.ModelAdmin):
    list_display = (
        "nombre",
        "codigo",
        "estado_receta",
        "stock_actual_columna",
        "stock_minimo",
        "estado_stock",
        "activo",
    )
    list_filter = ("activo", StockBajoFilter)
    search_fields = ("nombre", "codigo", "descripcion")
    ordering = ("nombre",)
    list_per_page = 25
    fieldsets = (
        (
            "Datos del producto",
            {"fields": ("nombre", "codigo", "descripcion", "requiere_receta")},
        ),
        ("Precio", {"fields": ("precio",)}),
        ("Control de inventario", {"fields": ("stock_minimo", "activo")}),
    )

    def get_queryset(self, request):
        lotes_vigentes = IngresoInventario.objects.filter(
            fecha_vencimiento__gte=timezone.localdate()
        )
        ingresado = (
            lotes_vigentes.filter(medicamento_id=OuterRef("pk"))
            .values("medicamento_id")
            .annotate(total=Sum("cantidad"))
            .values("total")[:1]
        )
        mermado = (
            Merma.objects.filter(
                ingreso__in=lotes_vigentes,
                ingreso__medicamento_id=OuterRef("pk"),
            )
            .values("ingreso__medicamento_id")
            .annotate(total=Sum("cantidad"))
            .values("total")[:1]
        )
        return super().get_queryset(request).annotate(
            _stock_actual=Coalesce(
                Subquery(ingresado, output_field=IntegerField()), Value(0)
            )
            - Coalesce(Subquery(mermado, output_field=IntegerField()), Value(0))
        )

    @admin.display(description="Stock disponible", ordering="_stock_actual")
    def stock_actual_columna(self, obj):
        return obj._stock_actual

    @admin.display(description="Estado")
    def estado_stock(self, obj):
        if obj._stock_actual <= obj.stock_minimo:
            return "Reponer"
        return "Disponible"

    @admin.display(description="Condición de venta")
    def estado_receta(self, obj):
        return "Con receta" if obj.requiere_receta else "Sin receta"


class VencimientoFilter(admin.SimpleListFilter):
    title = "fecha de vencimiento"
    parameter_name = "vencimiento"

    def lookups(self, request, model_admin):
        return (
            ("vencido", "Vencido"),
            ("30", "Vence dentro de 30 días"),
            ("90", "Vence dentro de 90 días"),
        )

    def queryset(self, request, queryset):
        hoy = timezone.localdate()
        if self.value() == "vencido":
            return queryset.filter(fecha_vencimiento__lt=hoy)
        if self.value() in ("30", "90"):
            limite = hoy + timedelta(days=int(self.value()))
            return queryset.filter(
                fecha_vencimiento__gte=hoy,
                fecha_vencimiento__lte=limite,
            )
        return queryset


@admin.register(IngresoInventario, site=farmacia_admin_site)
class IngresoInventarioAdmin(admin.ModelAdmin):
    list_display = (
        "medicamento",
        "lote",
        "cantidad",
        "stock_disponible_columna",
        "fecha_vencimiento",
        "estado_vencimiento",
        "proveedor",
        "fecha_ingreso",
    )
    list_filter = (VencimientoFilter, "medicamento")
    search_fields = ("medicamento__nombre", "medicamento__codigo", "lote", "proveedor")
    autocomplete_fields = ("medicamento",)
    date_hierarchy = "fecha_vencimiento"
    list_select_related = ("medicamento", "registrado_por")
    readonly_fields = ("fecha_ingreso", "registrado_por")
    list_display_links = None
    list_per_page = 25

    def get_queryset(self, request):
        mermas = (
            Merma.objects.filter(ingreso_id=OuterRef("pk"))
            .values("ingreso_id")
            .annotate(total=Sum("cantidad"))
            .values("total")[:1]
        )
        return super().get_queryset(request).annotate(
            _stock_restante=F("cantidad")
            - Coalesce(Subquery(mermas, output_field=IntegerField()), Value(0))
        )

    @admin.display(description="Stock actual del lote", ordering="_stock_restante")
    def stock_disponible_columna(self, obj):
        return obj._stock_restante

    @admin.display(description="Estado del lote")
    def estado_vencimiento(self, obj):
        hoy = timezone.localdate()
        if obj.fecha_vencimiento < hoy:
            return "Vencido"
        if obj.fecha_vencimiento <= hoy + timedelta(days=30):
            return "Vence pronto"
        return "Vigente"

    def save_model(self, request, obj, form, change):
        if not change:
            obj.registrado_por = request.user
        super().save_model(request, obj, form, change)

    def has_change_permission(self, request, obj=None):
        return obj is None and super().has_change_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Merma, site=farmacia_admin_site)
class MermaAdmin(admin.ModelAdmin):
    list_display = (
        "medicamento_columna",
        "lote_columna",
        "cantidad",
        "motivo",
        "fecha_registro",
        "registrado_por",
    )
    list_filter = ("motivo", "fecha_registro")
    search_fields = (
        "ingreso__medicamento__nombre",
        "ingreso__medicamento__codigo",
        "ingreso__lote",
        "observacion",
    )
    autocomplete_fields = ("ingreso",)
    list_select_related = ("ingreso__medicamento", "registrado_por")
    readonly_fields = ("fecha_registro", "registrado_por")
    list_display_links = None
    list_per_page = 25
    fieldsets = (
        ("Lote afectado", {"fields": ("ingreso", "cantidad")}),
        ("Detalle de la merma", {"fields": ("motivo", "observacion")}),
        ("Registro", {"fields": ("fecha_registro", "registrado_por")}),
    )

    @admin.display(description="Medicamento")
    def medicamento_columna(self, obj):
        return obj.ingreso.medicamento

    @admin.display(description="Lote")
    def lote_columna(self, obj):
        return obj.ingreso.lote

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "ingreso":
            kwargs["queryset"] = _ingresos_con_stock_disponible().filter(
                stock_restante__gt=0
            ).select_related("medicamento").order_by("fecha_vencimiento")
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.registrado_por = request.user
        super().save_model(request, obj, form, change)

    def has_change_permission(self, request, obj=None):
        return obj is None and super().has_change_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(VentaRecetaRetenida, site=farmacia_admin_site)
class VentaRecetaRetenidaAdmin(admin.ModelAdmin):
    list_display = (
        "fecha_registro",
        "cliente_rut",
        "medicamento",
        "cantidad",
        "auditor_qf",
        "estado",
    )
    list_filter = ("estado", "fecha_registro")
    search_fields = ("cliente_rut", "medicamento", "medico_prescriptor")
    list_select_related = ("auditor_qf",)
    readonly_fields = tuple(field.name for field in VentaRecetaRetenida._meta.fields)
    date_hierarchy = "fecha_registro"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SimulacionPago, site=farmacia_admin_site)
class SimulacionPagoAdmin(admin.ModelAdmin):
    list_display = (
        "venta",
        "monto",
        "medio_pago",
        "estado",
        "fecha_simulacion",
    )
    list_filter = ("estado", "medio_pago", "fecha_simulacion")
    search_fields = ("venta__cliente_rut", "referencia_simulada")
    list_select_related = ("venta",)
    readonly_fields = tuple(field.name for field in SimulacionPago._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AutorizacionExcepcional, site=farmacia_admin_site)
class AutorizacionExcepcionalAdmin(admin.ModelAdmin):
    list_display = (
        "venta",
        "tipo",
        "estado",
        "solicitada_por",
        "jefe_farmacia",
        "fecha_solicitud",
    )
    list_filter = ("estado", "tipo", "fecha_solicitud")
    search_fields = ("venta__cliente_rut", "motivo", "respuesta_jefe")
    list_select_related = ("venta", "solicitada_por", "jefe_farmacia")
    readonly_fields = tuple(
        field.name for field in AutorizacionExcepcional._meta.fields
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SolicitudCuenta, site=farmacia_admin_site)
class SolicitudCuentaAdmin(admin.ModelAdmin):
    list_display = (
        "usuario",
        "rol_solicitado",
        "estado",
        "fecha_solicitud",
        "acciones",
    )
    list_filter = ("estado", "fecha_solicitud")
    search_fields = ("usuario__username", "usuario__email", "usuario__first_name")
    readonly_fields = ("usuario", "rol_solicitado", "fecha_solicitud")
    actions = ("aprobar_solicitudes", "rechazar_solicitudes")
    list_per_page = 25

    def get_urls(self):
        custom_urls = [
            path(
                "<path:object_id>/revisar/",
                self.admin_site.admin_view(self.revisar_solicitud),
                name="farmacia_solicitudcuenta_revisar",
            ),
            path(
                "<path:object_id>/resolver/",
                self.admin_site.admin_view(self.resolver_solicitud),
                name="farmacia_solicitudcuenta_resolver",
            ),
        ]
        return custom_urls + super().get_urls()

    def get_queryset(self, request):
        return super().get_queryset(request).filter(
            rol_solicitado=SolicitudCuenta.RolSolicitado.FARMACEUTICO
        )

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Revisión")
    def acciones(self, obj):
        if obj.estado != SolicitudCuenta.Estado.PENDIENTE:
            return "Solicitud revisada"
        url = reverse(
            "admin:farmacia_solicitudcuenta_revisar",
            args=(obj.pk,),
        )
        return format_html(
            '<a class="solicitud-accion" href="{}">Revisar solicitud</a>',
            url,
        )

    def revisar_solicitud(self, request, object_id):
        if not request.user.is_superuser:
            raise PermissionDenied
        solicitud = get_object_or_404(
            self.get_queryset(request).select_related("usuario"),
            pk=object_id,
        )
        return render(
            request,
            "admin/farmacia_revisar_solicitud.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "title": "Revisar solicitud de químico",
                "solicitud": solicitud,
                "changelist_url": reverse(
                    "admin:farmacia_solicitudcuenta_changelist"
                ),
            },
        )

    @method_decorator(require_POST)
    def resolver_solicitud(self, request, object_id):
        if not request.user.is_superuser:
            raise PermissionDenied
        solicitud = get_object_or_404(
            self.get_queryset(request).select_related("usuario"),
            pk=object_id,
            estado=SolicitudCuenta.Estado.PENDIENTE,
        )
        decision = request.POST.get("decision")
        try:
            if decision == "aprobar":
                solicitud.aprobar(request.user)
                mensaje = (
                    f"La cuenta de {solicitud.usuario.get_username()} fue aprobada "
                    "y ya puede iniciar sesión."
                )
                nivel = messages.SUCCESS
            elif decision == "rechazar":
                solicitud.rechazar(request.user)
                mensaje = f"La solicitud de {solicitud.usuario.get_username()} fue rechazada."
                nivel = messages.WARNING
            else:
                messages.error(request, "Selecciona aprobar o rechazar la solicitud.")
                return redirect("admin:farmacia_solicitudcuenta_revisar", object_id)
        except ValidationError as error:
            messages.error(request, "No se pudo resolver la solicitud: " + "; ".join(error.messages))
            return redirect("admin:farmacia_solicitudcuenta_revisar", object_id)

        self.message_user(request, mensaje, level=nivel)
        return redirect("admin:farmacia_solicitudcuenta_changelist")

    @admin.action(description="Aprobar químicos seleccionados")
    def aprobar_solicitudes(self, request, queryset):
        self._resolver_solicitudes(request, queryset, aprobar=True)

    @admin.action(description="Rechazar solicitudes seleccionadas")
    def rechazar_solicitudes(self, request, queryset):
        self._resolver_solicitudes(request, queryset, aprobar=False)

    def _resolver_solicitudes(self, request, queryset, *, aprobar):
        if not request.user.is_superuser:
            raise PermissionDenied

        resueltas = 0
        omitidas = 0
        with transaction.atomic():
            for solicitud in queryset.select_related("usuario").filter(
                estado=SolicitudCuenta.Estado.PENDIENTE,
                rol_solicitado=SolicitudCuenta.RolSolicitado.FARMACEUTICO,
            ):
                try:
                    if aprobar:
                        solicitud.aprobar(request.user)
                    else:
                        solicitud.rechazar(request.user)
                except ValidationError:
                    omitidas += 1
                else:
                    resueltas += 1

        verbo = "aprobados" if aprobar else "rechazados"
        self.message_user(
            request,
            f"{resueltas} solicitud(es) de químicos {verbo}; {omitidas} omitida(s).",
            level=messages.SUCCESS if resueltas else messages.WARNING,
        )
