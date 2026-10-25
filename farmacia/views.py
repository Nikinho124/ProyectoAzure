from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied
from django.db.models import IntegerField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST
from django.contrib.auth.decorators import login_required

from farmacia.forms import RegistroUsuarioForm
from farmacia.models import (
    GRUPO_FARMACEUTICO,
    IngresoInventario,
    Medicamento,
    Merma,
    SolicitudCuenta,
)


class IniciarSesionView(LoginView):
    template_name = "farmacia/login.html"

    def get_success_url(self):
        if self.request.user.is_staff:
            return reverse("admin:index")
        if self.request.user.groups.filter(name=GRUPO_FARMACEUTICO).exists():
            return reverse("farmacia:inicio")
        return super().get_success_url()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("registro_form", RegistroUsuarioForm())
        return context


@require_POST
def registrar_usuario(request):
    registro_form = RegistroUsuarioForm(request.POST)
    if registro_form.is_valid():
        es_cliente = (
            registro_form.cleaned_data["rol_solicitado"]
            == SolicitudCuenta.RolSolicitado.CLIENTE
        )
        registro_form.save()
        if es_cliente:
            messages.success(
                request,
                "Tu cuenta ya está creada. Inicia sesión para continuar.",
            )
        else:
            messages.success(
                request,
                "Tu solicitud fue enviada. Podrás iniciar sesión cuando el administrador la apruebe.",
            )
        return redirect("login")

    return render(
        request,
        "farmacia/login.html",
        {
            "form": AuthenticationForm(request=request),
            "registro_form": registro_form,
        },
        status=400,
    )


def catalogo(request):
    medicamentos = [
        {
            "nombre": "Medicamento de ejemplo 1",
            "codigo": "DEMO-001",
            "descripcion": "Reemplaza este registro por información real.",
        },
        {
            "nombre": "Medicamento de ejemplo 2",
            "codigo": "DEMO-002",
            "descripcion": "Aquí se mostrarán los detalles del producto.",
        },
        {
            "nombre": "Medicamento de ejemplo 3",
            "codigo": "DEMO-003",
            "descripcion": "Catálogo demostrativo, sin productos reales.",
        },
    ]
    busqueda = request.GET.get("q", "").strip()
    if busqueda:
        termino = busqueda.casefold()
        medicamentos = [
            medicamento
            for medicamento in medicamentos
            if termino in medicamento["nombre"].casefold()
            or termino in medicamento["codigo"].casefold()
        ]

    return render(
        request,
        "farmacia/catalogo.html",
        {"medicamentos": medicamentos, "busqueda": busqueda},
    )


def _es_personal_farmacia(user):
    return (
        user.is_authenticated
        and (
            user.is_staff
            or user.groups.filter(name=GRUPO_FARMACEUTICO).exists()
        )
    )


def _medicamentos_con_stock():
    ingresado = (
        IngresoInventario.objects.filter(
            medicamento_id=OuterRef("pk"),
            fecha_vencimiento__gte=timezone.localdate(),
        )
        .values("medicamento_id")
        .annotate(total=Sum("cantidad"))
        .values("total")[:1]
    )
    mermado = (
        Merma.objects.filter(
            ingreso__medicamento_id=OuterRef("pk"),
            ingreso__fecha_vencimiento__gte=timezone.localdate(),
        )
        .values("ingreso__medicamento_id")
        .annotate(total=Sum("cantidad"))
        .values("total")[:1]
    )
    return Medicamento.objects.filter(activo=True).annotate(
        stock_vendible=Coalesce(
            Subquery(ingresado, output_field=IntegerField()),
            Value(0),
        )
        - Coalesce(
            Subquery(mermado, output_field=IntegerField()),
            Value(0),
        )
    )


@login_required
def inicio_farmacia(request):
    if not _es_personal_farmacia(request.user):
        raise PermissionDenied

    medicamentos = _medicamentos_con_stock()
    disponibles = medicamentos.filter(stock_vendible__gt=0)
    return render(
        request,
        "farmacia/inicio_farmacia.html",
        {
            "medicamentos": medicamentos.order_by("nombre"),
            "total_medicamentos": medicamentos.count(),
            "total_disponibles": disponibles.count(),
        },
    )


def _obtener_carrito(request):
    carrito = request.session.get("farmacia_caja_carrito", {})
    if not isinstance(carrito, dict):
        carrito = {}
    return carrito


def _datos_carrito(carrito):
    productos = []
    total = Decimal("0.00")
    unidades = 0
    for medicamento_id, cantidad in carrito.items():
        medicamento = Medicamento.objects.filter(
            pk=medicamento_id,
            activo=True,
        ).first()
        if medicamento is None:
            continue

        try:
            cantidad = int(cantidad)
        except (TypeError, ValueError):
            continue
        if cantidad <= 0:
            continue

        subtotal = None
        if medicamento.precio is not None:
            subtotal = medicamento.precio * cantidad
            total += subtotal
        productos.append(
            {
                "medicamento": medicamento,
                "cantidad": cantidad,
                "subtotal": subtotal,
            }
        )
        unidades += cantidad
    return productos, total, unidades


@login_required
@require_http_methods(["GET", "POST"])
def caja(request):
    if not _es_personal_farmacia(request.user):
        raise PermissionDenied

    carrito = _obtener_carrito(request)
    if request.method == "POST":
        accion = request.POST.get("accion")
        if accion == "agregar":
            medicamento = get_object_or_404(
                Medicamento,
                pk=request.POST.get("medicamento_id"),
                activo=True,
            )
            try:
                cantidad = int(request.POST.get("cantidad", "1"))
            except (TypeError, ValueError):
                cantidad = 0

            if cantidad < 1:
                messages.error(request, "Ingresa una cantidad mayor que cero.")
                return redirect("farmacia:caja")

            medicamentos = _medicamentos_con_stock()
            stock = medicamentos.filter(pk=medicamento.pk).values_list(
                "stock_vendible",
                flat=True,
            ).first() or 0
            try:
                cantidad_actual = int(carrito.get(str(medicamento.pk), 0))
            except (TypeError, ValueError):
                cantidad_actual = 0
            if cantidad_actual + cantidad > stock:
                messages.error(
                    request,
                    f"Stock disponible para {medicamento.nombre}: {stock} unidades.",
                )
                return redirect("farmacia:caja")

            carrito[str(medicamento.pk)] = cantidad_actual + cantidad
            request.session["farmacia_caja_carrito"] = carrito
            messages.success(request, f"{medicamento.nombre} agregado a la caja.")
            return redirect("farmacia:caja")

        if accion == "quitar":
            carrito.pop(request.POST.get("medicamento_id"), None)
            request.session["farmacia_caja_carrito"] = carrito
            messages.info(request, "Producto quitado de la caja.")
            return redirect("farmacia:caja")

        if accion == "vaciar":
            request.session["farmacia_caja_carrito"] = {}
            messages.info(request, "La caja quedó vacía.")
            return redirect("farmacia:caja")

        messages.error(request, "No se reconoció la acción solicitada.")
        return redirect("farmacia:caja")

    busqueda = request.GET.get("q", "").strip()
    medicamentos = _medicamentos_con_stock().order_by("nombre")
    if busqueda:
        medicamentos = medicamentos.filter(
            Q(nombre__icontains=busqueda)
            | Q(codigo__icontains=busqueda)
        )

    productos_carrito, total_carrito, unidades_carrito = _datos_carrito(carrito)
    return render(
        request,
        "farmacia/caja.html",
        {
            "medicamentos": medicamentos,
            "busqueda": busqueda,
            "productos_carrito": productos_carrito,
            "total_carrito": total_carrito,
            "unidades_carrito": unidades_carrito,
            "carrito_tiene_precios": all(
                producto["subtotal"] is not None for producto in productos_carrito
            ),
        },
    )
