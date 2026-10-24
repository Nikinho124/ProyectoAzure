from django.contrib import messages
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from farmacia.forms import RegistroUsuarioForm
from farmacia.models import SolicitudCuenta


class IniciarSesionView(LoginView):
    template_name = "farmacia/login.html"

    def get_success_url(self):
        if self.request.user.is_staff:
            return reverse("admin:index")
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
