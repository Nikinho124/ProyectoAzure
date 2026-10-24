from datetime import timedelta

from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, IntegerField, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone

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

    def index(self, request, extra_context=None):
        hoy = timezone.localdate()
        hasta_30_dias = hoy + timedelta(days=30)
        ingresos = _ingresos_con_stock_disponible().filter(stock_restante__gt=0)
        medicamentos = Medicamento.objects.annotate(
            _ingresado=Coalesce(
                Subquery(
                    IngresoInventario.objects.filter(
                        medicamento_id=OuterRef("pk")
                    )
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
                        ingreso__medicamento_id=OuterRef("pk")
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
                },
            },
        }
        return super().index(request, extra_context)


farmacia_admin_site = FarmaAdminSite(name="admin")
farmacia_admin_site.register(User, UserAdmin)
farmacia_admin_site.register(Group)


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
        ("Datos del producto", {"fields": ("nombre", "codigo", "descripcion")}),
        ("Control de inventario", {"fields": ("stock_minimo", "activo")}),
    )

    def get_queryset(self, request):
        ingresado = (
            IngresoInventario.objects.filter(medicamento_id=OuterRef("pk"))
            .values("medicamento_id")
            .annotate(total=Sum("cantidad"))
            .values("total")[:1]
        )
        mermado = (
            Merma.objects.filter(ingreso__medicamento_id=OuterRef("pk"))
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
        "revisada_por",
    )
    list_filter = ("estado", "fecha_solicitud")
    search_fields = ("usuario__username", "usuario__email", "usuario__first_name")
    readonly_fields = ("usuario", "rol_solicitado", "fecha_solicitud")
    actions = ("aprobar_solicitudes", "rechazar_solicitudes")
    list_per_page = 25

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
