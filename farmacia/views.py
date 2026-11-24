import logging
import hashlib
from decimal import Decimal
from functools import wraps
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from PIL import Image
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import (
    LoginView,
    PasswordResetConfirmView,
    PasswordResetView,
)
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.core.paginator import EmptyPage, Paginator
from django.http import (
    FileResponse,
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    JsonResponse,
)
from django.db.models import F, IntegerField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import (
    require_GET,
    require_http_methods,
    require_POST,
)
from redis.exceptions import RedisError

from config.error_handlers import (
    handler404 as _handler404,
    handler500 as _handler500,
    manejar_bloqueo_base_datos,
)
from farmacia.auth_forms import (
    BoundedPasswordField,
    BoundedSetPasswordForm,
    EmployeeAuthenticationForm,
    FarmaciaPasswordResetForm,
)
from farmacia.models import (
    GRUPO_FARMACEUTICO,
    GRUPOS_JEFE_FARMACIA,
    AutorizacionExcepcional,
    ConsultaJefeFarmacia,
    IngresoInventario,
    MAX_UNIDADES_POR_RECETA,
    Medicamento,
    Merma,
    SimulacionPago,
    DetalleVentaCaja,
    VentaRecetaRetenida,
    VentaCaja,
    normalizar_texto_seguro,
)
from farmacia.consulta_forms import (
    ConsultaJefeFarmaciaForm,
    ResponderConsultaJefeForm,
)
from farmacia.inventario_forms import AjustarStockLoteForm, RevertirMermaForm
from farmacia.receta_forms import (
    ResolverAutorizacionForm,
    RevisionRecetaForm,
    VentaRecetaRetenidaForm,
)

logger = logging.getLogger(__name__)

MAX_SOLICITUDES_PENDIENTES_POR_USUARIO = 3
COOLDOWN_SOLICITUD_SEGUNDOS = 120
MENSAJE_LIMITE_SOLICITUDES = (
    "Has alcanzado el límite de 3 solicitudes pendientes. "
    "Espera la respuesta del Jefe de Farmacia."
)
MENSAJE_COOLDOWN_SOLICITUD = (
    "Debes esperar 2 minutos antes de enviar otra solicitud."
)
INTENTOS_MAXIMOS_ELIMINAR_CONSULTA = 5
VENTANA_RATE_LIMIT_ELIMINAR_CONSULTA = 60
INTENTOS_MAXIMOS_AJUSTE_INVENTARIO = 5
VENTANA_RATE_LIMIT_AJUSTE_INVENTARIO = 60
INTENTOS_MAXIMOS_PASSWORD_RESET = 3
VENTANA_RATE_LIMIT_PASSWORD_RESET = 15 * 60
MENSAJE_BLOQUEO_LOGIN = (
    "Has superado el límite de 5 intentos fallidos. Tu cuenta ha sido "
    "bloqueada por 15 minutos. Si no recuerdas tu contraseña, puedes "
    "restablecerla con el enlace enviado a tu correo o desde la opción "
    "de recuperación."
)


def error_404_view(request, exception):
    return _handler404(request, exception)


def error_500_view(request):
    return _handler500(request)


def _incrementar_contador_rate_limit(clave, ventana):
    if cache.add(clave, 1, timeout=ventana):
        return 1
    try:
        return cache.incr(clave)
    except ValueError:
        cache.add(clave, 1, timeout=ventana)
        return cache.get(clave, 1)


def limitar_frecuencia_solicitudes(scope, methods=("POST",)):
    limite = settings.FARMACIA_RATE_LIMIT_REQUESTS
    ventana = settings.FARMACIA_RATE_LIMIT_WINDOW_SECONDS

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method not in methods:
                return view(request, *args, **kwargs)

            identidades = []
            ip = request.META.get("REMOTE_ADDR")
            if ip:
                identidades.append(("ip", ip))
            else:
                identidades.append(("ip", "unknown"))
            session_key = request.session.session_key
            if session_key:
                identidades.append(("session", session_key))
            if not identidades:
                identidades.append(("ip", "unknown"))

            excedido = False
            try:
                for tipo, identidad in identidades:
                    digest = hashlib.sha256(
                        identidad.encode("utf-8")
                    ).hexdigest()
                    clave = f"farmacia:rate:{scope}:{tipo}:{digest}"
                    if _incrementar_contador_rate_limit(clave, ventana) > limite:
                        excedido = True
            except RedisError:
                logger.exception("El caché compartido del rate limit no responde.")
                datos = {
                    "status": "error",
                    "codigo": "LIMITADOR_NO_DISPONIBLE",
                    "mensaje": (
                        "El servicio de protección está temporalmente "
                        "no disponible. Intenta nuevamente."
                    ),
                }
                if "application/json" in request.headers.get("Accept", ""):
                    respuesta = JsonResponse(datos, status=503)
                else:
                    respuesta = HttpResponse(
                        "<!doctype html><title>Servicio no disponible</title>"
                        "<h1>Servicio no disponible</h1>"
                        "<p>Intenta nuevamente en unos segundos.</p>",
                        status=503,
                    )
                respuesta["Retry-After"] = "2"
                respuesta["Cache-Control"] = "no-store"
                return respuesta

            if excedido:
                datos = {
                    "status": "error",
                    "codigo": "LIMITE_DE_SOLICITUDES",
                    "mensaje": (
                        "Se superó el límite de solicitudes. "
                        "Intenta nuevamente en un minuto."
                    ),
                }
                if "application/json" in request.headers.get("Accept", ""):
                    respuesta = JsonResponse(datos, status=429)
                else:
                    respuesta = HttpResponse(
                        "<!doctype html><title>Demasiadas solicitudes</title>"
                        "<h1>Demasiadas solicitudes</h1>"
                        "<p>Intenta nuevamente en un minuto.</p>",
                        status=429,
                    )
                respuesta["Retry-After"] = str(ventana)
                respuesta["Cache-Control"] = "no-store"
                return respuesta

            return view(request, *args, **kwargs)

        return wrapped

    return decorator


def _respuesta_cache_solicitudes_no_disponible():
    respuesta = HttpResponse(
        "<!doctype html><title>Servicio no disponible</title>"
        "<h1>Servicio no disponible</h1>"
        "<p>No se pudo verificar el límite de solicitudes. Intenta nuevamente.</p>",
        status=503,
    )
    respuesta["Retry-After"] = "2"
    respuesta["Cache-Control"] = "no-store"
    return respuesta


class FarmaciaPasswordResetView(PasswordResetView):
    form_class = FarmaciaPasswordResetForm

    def post(self, request, *args, **kwargs):
        identidades = []
        ip = request.META.get("REMOTE_ADDR")
        if ip:
            identidades.append(("ip", ip))

        email = request.POST.get("email", "").strip().casefold()
        if email and len(email) <= 100:
            identidades.append(("email", email))

        excedido = False
        try:
            for tipo, identidad in identidades:
                digest = hashlib.sha256(
                    identidad.encode("utf-8")
                ).hexdigest()
                clave = f"farmacia:rate:password-reset:{tipo}:{digest}"
                intentos = _incrementar_contador_rate_limit(
                    clave,
                    VENTANA_RATE_LIMIT_PASSWORD_RESET,
                )
                excedido = excedido or (
                    intentos > INTENTOS_MAXIMOS_PASSWORD_RESET
                )
        except RedisError:
            logger.exception(
                "El caché del límite de restablecimiento no responde."
            )
            return _respuesta_cache_solicitudes_no_disponible()

        if excedido:
            respuesta = HttpResponse(
                "<!doctype html><title>Demasiadas solicitudes</title>"
                "<h1>Demasiadas solicitudes</h1>"
                "<p>Espera 15 minutos antes de volver a solicitar "
                "un restablecimiento.</p>",
                status=429,
            )
            respuesta["Retry-After"] = str(
                VENTANA_RATE_LIMIT_PASSWORD_RESET
            )
            respuesta["Cache-Control"] = "no-store"
            return respuesta

        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        # Invalid addresses get the same neutral response as unknown users.
        return redirect(self.get_success_url())


def _bloquear_solicitud_si_corresponde(
    request,
    *,
    clave_cooldown,
    solicitudes_pendientes,
    vista,
):
    try:
        en_cooldown = cache.get(clave_cooldown)
    except RedisError:
        logger.exception("El caché del cooldown de solicitudes no responde.")
        return _respuesta_cache_solicitudes_no_disponible()

    if en_cooldown:
        messages.error(request, MENSAJE_COOLDOWN_SOLICITUD)
        return redirect(vista)

    if solicitudes_pendientes.count() >= MAX_SOLICITUDES_PENDIENTES_POR_USUARIO:
        messages.error(request, MENSAJE_LIMITE_SOLICITUDES)
        return redirect(vista)

    return None


def _reservar_cooldown_solicitud(request, clave_cooldown, vista):
    try:
        reservada = cache.add(
            clave_cooldown,
            "enviando",
            timeout=COOLDOWN_SOLICITUD_SEGUNDOS,
        )
    except RedisError:
        logger.exception("No se pudo reservar el cooldown de solicitudes.")
        return _respuesta_cache_solicitudes_no_disponible()

    if not reservada:
        messages.error(request, MENSAJE_COOLDOWN_SOLICITUD)
        return redirect(vista)

    return None


def _liberar_cooldown_solicitud(clave_cooldown):
    try:
        cache.delete(clave_cooldown)
    except RedisError:
        logger.exception("No se pudo liberar el cooldown de una solicitud fallida.")


def _respuesta_limite_ajustes_inventario(request):
    mensaje = "Se superó el límite de 5 intentos. Espera un minuto antes de reintentar."
    if "application/json" in request.headers.get("Accept", "").lower():
        respuesta = JsonResponse(
            {"error": "rate_limited", "message": mensaje},
            status=429,
        )
    else:
        respuesta = HttpResponse(
            "<!doctype html><title>Demasiados intentos</title>"
            "<h1>Demasiados intentos</h1>"
            "<p>Espera un minuto antes de reintentar.</p>",
            status=429,
        )
    respuesta["Retry-After"] = str(VENTANA_RATE_LIMIT_AJUSTE_INVENTARIO)
    respuesta["Cache-Control"] = "no-store"
    return respuesta


def limitar_ajustes_inventario(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.method != "POST":
            return view(request, *args, **kwargs)

        clave = f"farmacia:rate:ajuste-inventario:usuario:{request.user.pk}"
        try:
            intentos = _incrementar_contador_rate_limit(
                clave,
                VENTANA_RATE_LIMIT_AJUSTE_INVENTARIO,
            )
        except RedisError:
            logger.exception(
                "El caché del límite de ajustes de inventario no responde."
            )
            return _respuesta_cache_solicitudes_no_disponible()

        if intentos > INTENTOS_MAXIMOS_AJUSTE_INVENTARIO:
            return _respuesta_limite_ajustes_inventario(request)
        return view(request, *args, **kwargs)

    return wrapped


@method_decorator(limitar_frecuencia_solicitudes("login"), name="dispatch")
class IniciarSesionView(LoginView):
    template_name = "farmacia/login.html"
    authentication_form = EmployeeAuthenticationForm
    redirect_authenticated_user = True

    def _username_para_bloqueo(self):
        username = self.request.POST.get("username", "")
        field = self.get_form_class().base_fields.get("username")
        max_length = getattr(field, "max_length", None) or 150
        if not username or len(username) > max_length:
            return ""
        if field is not None:
            username = field.to_python(username)
        return username

    def _clave_intentos_fallidos(self, username):
        return f"failed_login_{username}"

    def _respuesta_login_bloqueado(self):
        messages.error(self.request, MENSAJE_BLOQUEO_LOGIN)
        respuesta = self.render_to_response(
            self.get_context_data(form=self.get_form()),
            status=429,
        )
        respuesta["Retry-After"] = str(settings.FARMACIA_LOGIN_LOCKOUT_SECONDS)
        respuesta["Cache-Control"] = "no-store"
        return respuesta

    def post(self, request, *args, **kwargs):
        self.request = request
        username = self._username_para_bloqueo()
        if username:
            try:
                intentos = cache.get(
                    self._clave_intentos_fallidos(username),
                    0,
                )
            except RedisError:
                logger.exception("El caché del bloqueo de inicio de sesión no responde.")
                return _respuesta_cache_solicitudes_no_disponible()
            if intentos >= settings.FARMACIA_LOGIN_MAX_FAILED_ATTEMPTS:
                return self._respuesta_login_bloqueado()
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        username = form.cleaned_data.get("username") or self._username_para_bloqueo()
        if username:
            try:
                intentos = _incrementar_contador_rate_limit(
                    self._clave_intentos_fallidos(username),
                    settings.FARMACIA_LOGIN_LOCKOUT_SECONDS,
                )
            except RedisError:
                logger.exception(
                    "El caché del bloqueo de inicio de sesión no responde."
                )
                return _respuesta_cache_solicitudes_no_disponible()
            if intentos >= settings.FARMACIA_LOGIN_MAX_FAILED_ATTEMPTS:
                return self._respuesta_login_bloqueado()
        return super().form_invalid(form)

    def form_valid(self, form):
        username = form.cleaned_data.get("username")
        if username:
            try:
                cache.delete(self._clave_intentos_fallidos(username))
            except RedisError:
                logger.exception(
                    "No se pudo limpiar el contador de intentos del inicio de sesión."
                )
                return _respuesta_cache_solicitudes_no_disponible()
        return super().form_valid(form)

    def get_success_url(self):
        redirect_to = self.request.POST.get("next") or self.request.GET.get("next")
        if redirect_to:
            if url_has_allowed_host_and_scheme(
                redirect_to,
                allowed_hosts={self.request.get_host()},
                require_https=self.request.is_secure(),
            ):
                if urlsplit(redirect_to).path not in {"", "/"}:
                    return redirect_to
            else:
                return reverse("farmacia:inicio")
        if self.request.user.is_staff:
            return reverse("admin:index")
        return reverse("farmacia:inicio")


class ConfirmarRestablecimientoContrasenaView(PasswordResetConfirmView):
    form_class = BoundedSetPasswordForm

    def form_valid(self, form):
        try:
            cache.delete(
                f"failed_login_{form.user.get_username()}"
            )
        except RedisError:
            logger.exception(
                "No se pudo limpiar el bloqueo tras restablecer la contraseña."
            )
            return _respuesta_cache_solicitudes_no_disponible()
        return super().form_valid(form)


def _es_personal_farmacia(user):
    return (
        user.is_authenticated
        and (
            user.is_superuser
            or user.groups.filter(
                name__in=(GRUPO_FARMACEUTICO, *GRUPOS_JEFE_FARMACIA)
            ).exists()
        )
    )


def _validar_parametros_get(request, permitidos):
    if set(request.GET.keys()).difference(permitidos):
        return HttpResponseBadRequest("La solicitud incluye parámetros no admitidos.")
    return None


def _numero_pagina(request, parametro):
    valores = request.GET.getlist(parametro)
    if not valores:
        return 1, None
    valor = valores[0]
    if (
        len(valores) != 1
        or not valor
        or len(valor) > 6
        or not valor.isascii()
        or not valor.isdecimal()
        or valor.startswith("0")
    ):
        return None, HttpResponseBadRequest("El número de página no es válido.")

    pagina = int(valor)
    if pagina > 10_000:
        return None, HttpResponseBadRequest("El número de página no es válido.")
    return pagina, None


def _paginar_queryset(queryset, numero_pagina, cantidad):
    try:
        return Paginator(queryset, cantidad).page(numero_pagina), None
    except EmptyPage:
        return None, HttpResponseBadRequest("La página solicitada no existe.")


def _validar_payload_formulario(request, campos, archivos=()):
    content_type = (request.content_type or "").lower()
    if content_type == "application/json" or content_type.endswith("+json"):
        return JsonResponse(
            {
                "status": "error",
                "codigo": "TIPO_DE_SOLICITUD_NO_ADMITIDO",
                "mensaje": "Este endpoint acepta datos de formulario, no JSON.",
            },
            status=400,
        )

    tiene_repetidos = any(
        len(request.POST.getlist(campo)) > 1 for campo in campos
    ) or any(len(request.FILES.getlist(campo)) > 1 for campo in archivos)
    if tiene_repetidos:
        return JsonResponse(
            {
                "status": "error",
                "codigo": "PARAMETRO_REPETIDO",
                "mensaje": "Envía un solo valor para cada parámetro.",
            },
            status=400,
        )
    return None


def _es_jefe_farmacia(user):
    return user.is_authenticated and (
        user.is_superuser
        or user.groups.filter(name__in=GRUPOS_JEFE_FARMACIA).exists()
    )


def _jefe_farmacia_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not _es_jefe_farmacia(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapped


def _agregar_errores_validacion(form, error):
    if hasattr(error, "message_dict"):
        for campo, errores in error.message_dict.items():
            destino = campo if campo in form.fields else None
            for mensaje in errores:
                form.add_error(destino, mensaje)
        return
    for mensaje in error.messages:
        form.add_error(None, mensaje)


@login_required
@_jefe_farmacia_required
@limitar_ajustes_inventario
@require_http_methods(["GET", "POST"])
@manejar_bloqueo_base_datos
def ajustar_stock_lote(request, lote_id):
    lote = get_object_or_404(
        IngresoInventario.objects.select_related("medicamento", "proveedor"),
        pk=lote_id,
    )
    if request.method == "POST":
        error_payload = _validar_payload_formulario(
            request,
            (
                "cantidad",
                "fecha_vencimiento",
                "motivo",
                "password_confirmacion",
            ),
        )
        if error_payload:
            return error_payload
        campos_permitidos = {
            "cantidad",
            "fecha_vencimiento",
            "motivo",
            "password_confirmacion",
            "csrfmiddlewaretoken",
        }
        if set(request.POST.keys()).difference(campos_permitidos):
            return HttpResponseBadRequest(
                "La solicitud incluye campos no admitidos."
            )
        form = AjustarStockLoteForm(request.POST)
        if form.is_valid():
            if not request.user.check_password(
                form.cleaned_data["password_confirmacion"]
            ):
                messages.error(
                    request,
                    "Contraseña de confirmación incorrecta.",
                )
            else:
                try:
                    lote.ajustar_stock(
                        usuario=request.user,
                        cantidad_nueva=form.cleaned_data["cantidad"],
                        fecha_vencimiento_nueva=form.cleaned_data[
                            "fecha_vencimiento"
                        ],
                        motivo=form.cleaned_data["motivo"],
                    )
                except ValidationError as error:
                    _agregar_errores_validacion(form, error)
                else:
                    messages.success(
                        request,
                        f"El lote {lote.lote} fue corregido y quedó registrado en auditoría.",
                    )
                    return redirect("admin:farmacia_ingresoinventario_changelist")
        status = 400
    else:
        form = AjustarStockLoteForm(
            initial={
                "cantidad": lote.cantidad,
                "fecha_vencimiento": lote.fecha_vencimiento,
            }
        )
        status = 200

    return render(
        request,
        "admin/farmacia/ajustar_stock_lote.html",
        {
            "form": form,
            "lote": lote,
            "changelist_url": reverse(
                "admin:farmacia_ingresoinventario_changelist"
            ),
            "title": "Corregir lote y stock",
        },
        status=status,
    )


@login_required
@_jefe_farmacia_required
@limitar_ajustes_inventario
@require_POST
@manejar_bloqueo_base_datos
def revertir_merma(request, merma_id):
    merma = get_object_or_404(
        Merma.objects.select_related(
            "ingreso__medicamento",
            "registrado_por",
        ),
        pk=merma_id,
    )
    error_payload = _validar_payload_formulario(
        request,
        ("motivo", "password_confirmacion"),
    )
    if error_payload:
        return error_payload
    if set(request.POST.keys()).difference(
        {"motivo", "password_confirmacion", "csrfmiddlewaretoken"}
    ):
        return HttpResponseBadRequest(
            "La solicitud incluye campos no admitidos."
        )

    form = RevertirMermaForm(request.POST)
    if form.is_valid():
        if not request.user.check_password(
            form.cleaned_data["password_confirmacion"]
        ):
            messages.error(
                request,
                "Contraseña de confirmación incorrecta.",
            )
        else:
            try:
                merma.revertir(
                    usuario=request.user,
                    motivo=form.cleaned_data["motivo"],
                )
            except ValidationError as error:
                _agregar_errores_validacion(form, error)
            else:
                messages.success(
                    request,
                    f"La merma #{merma.pk} fue revertida y quedó registrada en auditoría.",
                )
                return redirect("admin:farmacia_merma_changelist")

    return render(
        request,
        "admin/farmacia/revertir_merma.html",
        {
            "form": form,
            "merma": merma,
            "changelist_url": reverse("admin:farmacia_merma_changelist"),
            "title": "Revertir merma",
        },
        status=400,
    )


def _respuesta_limite_eliminar_consulta(request):
    mensaje = "Se superó el límite de intentos. Espera un minuto antes de reintentar."
    if "application/json" in request.headers.get("Accept", "").lower():
        respuesta = JsonResponse(
            {"error": "rate_limited", "message": mensaje},
            status=429,
        )
    else:
        respuesta = HttpResponse(
            "<!doctype html><title>Demasiados intentos</title>"
            "<h1>Demasiados intentos</h1><p>Espera un minuto antes de reintentar.</p>",
            status=429,
        )
    respuesta["Retry-After"] = str(VENTANA_RATE_LIMIT_ELIMINAR_CONSULTA)
    respuesta["Cache-Control"] = "no-store"
    return respuesta


def _url_gestion_inventario(user):
    if user.is_superuser or user.groups.filter(
        name__in=GRUPOS_JEFE_FARMACIA
    ).exists():
        return reverse("admin:farmacia_ingresoinventario_changelist")

    puede_ver_ingresos = user.has_perm("farmacia.view_ingresoinventario")
    puede_modificar_ingresos = user.has_perm(
        "farmacia.change_ingresoinventario"
    )
    if puede_ver_ingresos or puede_modificar_ingresos:
        return reverse("admin:farmacia_ingresoinventario_changelist")
    if user.has_perm("farmacia.add_ingresoinventario"):
        return reverse("admin:farmacia_ingresoinventario_add")
    return None


def _eliminar_archivo_consulta(almacen, nombre):
    try:
        almacen.delete(nombre)
    except OSError:
        logger.exception("No se pudo eliminar la imagen de una consulta eliminada.")


def _es_auditor_qf(user):
    return user.groups.filter(name=GRUPO_FARMACEUTICO).exists()


def _revisor_receta_valido(receta):
    if not receta.revisado_por_id:
        return False
    revisor = receta.revisado_por
    return (
        revisor.is_superuser
        or revisor.groups.filter(name__in=GRUPOS_JEFE_FARMACIA).exists()
        or (
            revisor.groups.filter(name=GRUPO_FARMACEUTICO).exists()
            and receta.auditor_qf_id == revisor.pk
        )
    )


def _medicamentos_con_stock():
    ingresado = (
        IngresoInventario.objects.filter(
            medicamento_id=OuterRef("pk"),
            fecha_vencimiento__gte=timezone.localdate(),
        )
        .values("medicamento_id")
        .annotate(total=Sum("stock_disponible"))
        .values("total")[:1]
    )
    return Medicamento.objects.filter(activo=True).annotate(
        stock_vendible=Coalesce(
            Subquery(ingresado, output_field=IntegerField()),
            Value(0),
        )
    )


@require_GET
@login_required
def inicio_farmacia(request):
    respuesta = _validar_parametros_get(request, {"page"})
    if respuesta:
        return respuesta
    numero_pagina, respuesta = _numero_pagina(request, "page")
    if respuesta:
        return respuesta
    if not _es_personal_farmacia(request.user):
        raise PermissionDenied

    medicamentos = _medicamentos_con_stock()
    disponibles = medicamentos.filter(stock_vendible__gt=0)
    consultas_pendientes = ConsultaJefeFarmacia.objects.filter(
        estado=ConsultaJefeFarmacia.Estado.PENDIENTE
    )
    if not _es_jefe_farmacia(request.user):
        consultas_pendientes = consultas_pendientes.filter(
            solicitante=request.user
        )
    pagina_medicamentos, respuesta = _paginar_queryset(
        medicamentos.order_by("nombre"),
        numero_pagina,
        50,
    )
    if respuesta:
        return respuesta
    return render(
        request,
        "farmacia/inicio_farmacia.html",
        {
            "medicamentos": pagina_medicamentos,
            "pagina_medicamentos": pagina_medicamentos,
            "total_medicamentos": medicamentos.count(),
            "total_disponibles": disponibles.count(),
            "consultas_pendientes": consultas_pendientes.count(),
            "consultas_jefe_disponible": (
                request.user.is_superuser
                or request.user.groups.filter(
                    name__in=(GRUPO_FARMACEUTICO, *GRUPOS_JEFE_FARMACIA)
                ).exists()
            ),
            "url_gestion_inventario": _url_gestion_inventario(request.user),
        },
    )


@manejar_bloqueo_base_datos
@login_required
@require_http_methods(["GET", "POST"])
def recetas_retenidas(request):
    es_jefe = _es_jefe_farmacia(request.user)
    es_auditor = _es_auditor_qf(request.user)
    if not (es_jefe or es_auditor):
        raise PermissionDenied
    parametros_pagina = (
        "pagina_pendientes",
        "pagina_historial",
        "pagina_excepciones",
    )
    respuesta = _validar_parametros_get(request, set(parametros_pagina))
    if respuesta:
        return respuesta
    paginas = {}
    for parametro in parametros_pagina:
        paginas[parametro], respuesta = _numero_pagina(request, parametro)
        if respuesta:
            return respuesta

    form = None
    if request.method == "POST":
        if es_jefe or not es_auditor:
            raise PermissionDenied

        clave_cooldown = (
            f"farmacia:cooldown:receta-retenida:{request.user.pk}"
        )
        respuesta = _bloquear_solicitud_si_corresponde(
            request,
            clave_cooldown=clave_cooldown,
            solicitudes_pendientes=VentaRecetaRetenida.objects.filter(
                auditor_qf=request.user,
                estado=VentaRecetaRetenida.Estado.PENDIENTE_REVISION,
            ),
            vista="farmacia:recetas_retenidas",
        )
        if respuesta:
            return respuesta

        respuesta = _validar_payload_formulario(
            request,
            (
                "cliente_rut",
                "medicamento",
                "cantidad",
                "medico_prescriptor",
                "motivo_excepcion",
            ),
            ("foto_receta",),
        )
        if respuesta:
            return respuesta
        form = VentaRecetaRetenidaForm(request.POST, request.FILES)
        if form.is_valid():
            respuesta = _reservar_cooldown_solicitud(
                request,
                clave_cooldown,
                "farmacia:recetas_retenidas",
            )
            if respuesta:
                return respuesta

            venta = form.save(commit=False)
            venta.auditor_qf = request.user
            guardado = False
            try:
                with transaction.atomic():
                    venta.save()
                    if venta.cantidad > MAX_UNIDADES_POR_RECETA:
                        autorizacion = AutorizacionExcepcional(
                            venta=venta,
                            tipo=AutorizacionExcepcional.Tipo.TOPE_MEDICAMENTOS,
                            motivo=form.cleaned_data["motivo_excepcion"],
                            solicitada_por=request.user,
                        )
                        autorizacion.full_clean()
                        autorizacion.save()
                guardado = True
            finally:
                if not guardado:
                    _liberar_cooldown_solicitud(clave_cooldown)
            try:
                cache.set(
                    clave_cooldown,
                    "enviado",
                    timeout=COOLDOWN_SOLICITUD_SEGUNDOS,
                )
            except RedisError:
                logger.exception("No se pudo extender el cooldown de la receta.")
            messages.success(
                request,
                f"Receta #{venta.pk} enviada al Jefe de Farmacia para revisión.",
            )
            return redirect("farmacia:recetas_retenidas")
    elif es_auditor and not es_jefe:
        form = VentaRecetaRetenidaForm()

    ventas = VentaRecetaRetenida.objects.select_related(
        "auditor_qf",
        "jefe_farmacia",
    ).prefetch_related("autorizaciones_excepcionales")
    autorizaciones = AutorizacionExcepcional.objects.select_related(
        "venta",
        "solicitada_por",
    ).filter(tipo=AutorizacionExcepcional.Tipo.TOPE_MEDICAMENTOS)
    if es_jefe:
        pendientes = ventas.filter(
            estado=VentaRecetaRetenida.Estado.PENDIENTE_REVISION
        )
        historial = ventas.exclude(
            estado=VentaRecetaRetenida.Estado.PENDIENTE_REVISION
        )
        excepciones_pendientes = autorizaciones.filter(
            estado=AutorizacionExcepcional.Estado.PENDIENTE
        )
    else:
        pendientes = ventas.filter(
            auditor_qf=request.user,
            estado=VentaRecetaRetenida.Estado.PENDIENTE_REVISION,
        )
        historial = ventas.filter(auditor_qf=request.user).exclude(
            estado=VentaRecetaRetenida.Estado.PENDIENTE_REVISION
        )
        excepciones_pendientes = AutorizacionExcepcional.objects.none()

    pendientes, respuesta = _paginar_queryset(
        pendientes.order_by("-fecha_registro", "-pk"),
        paginas["pagina_pendientes"],
        25,
    )
    if respuesta:
        return respuesta
    historial, respuesta = _paginar_queryset(
        historial.order_by("-fecha_registro", "-pk"),
        paginas["pagina_historial"],
        25,
    )
    if respuesta:
        return respuesta
    excepciones_pendientes, respuesta = _paginar_queryset(
        excepciones_pendientes.order_by("-fecha_solicitud", "-pk"),
        paginas["pagina_excepciones"],
        25,
    )
    if respuesta:
        return respuesta

    return render(
        request,
        "farmacia/recetas_retenidas.html",
        {
            "es_jefe": es_jefe,
            "url_gestion_inventario": _url_gestion_inventario(request.user),
            "form": form,
            "pendientes": pendientes,
            "historial": historial,
            "excepciones_pendientes": excepciones_pendientes,
            "max_unidades_receta": MAX_UNIDADES_POR_RECETA,
            "catalogo_recetas_disponible": Medicamento.objects.filter(
                activo=True,
                requiere_receta=True,
            ).exists(),
        },
    )


@manejar_bloqueo_base_datos
@login_required
@require_http_methods(["POST"])
def resolver_receta_retenida(request, venta_id):
    if not _es_jefe_farmacia(request.user):
        raise PermissionDenied

    respuesta = _validar_payload_formulario(
        request,
        ("decision", "observacion"),
    )
    if respuesta:
        return respuesta

    venta_id = _validar_pk_ruta(VentaRecetaRetenida, venta_id)
    venta = get_object_or_404(VentaRecetaRetenida, pk=venta_id)
    form = RevisionRecetaForm(request.POST)
    if not form.is_valid():
        messages.error(request, " ".join(
            error for errors in form.errors.values() for error in errors
        ))
        return redirect("farmacia:recetas_retenidas")

    try:
        if form.cleaned_data["decision"] == "aprobar":
            venta.aprobar(request.user)
            messages.success(request, f"La receta #{venta.pk} fue aprobada.")
        else:
            venta.rechazar(request.user, form.cleaned_data["observacion"])
            messages.success(request, f"La receta #{venta.pk} fue rechazada.")
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    return redirect("farmacia:recetas_retenidas")


@manejar_bloqueo_base_datos
@login_required
@require_http_methods(["POST"])
def resolver_excepcion_receta(request, autorizacion_id):
    if not _es_jefe_farmacia(request.user):
        raise PermissionDenied

    respuesta = _validar_payload_formulario(
        request,
        ("decision", "respuesta"),
    )
    if respuesta:
        return respuesta

    autorizacion_id = _validar_pk_ruta(AutorizacionExcepcional, autorizacion_id)
    autorizacion = get_object_or_404(
        AutorizacionExcepcional,
        pk=autorizacion_id,
        tipo=AutorizacionExcepcional.Tipo.TOPE_MEDICAMENTOS,
    )
    form = ResolverAutorizacionForm(request.POST)
    if not form.is_valid():
        messages.error(request, " ".join(
            error for errors in form.errors.values() for error in errors
        ))
        return redirect("farmacia:recetas_retenidas")

    aprobado = form.cleaned_data["decision"] == "aprobar"
    try:
        autorizacion.resolver(
            request.user,
            aprobado,
            form.cleaned_data["respuesta"],
        )
        resultado = "aprobada" if aprobado else "rechazada"
        messages.success(
            request,
            f"La excepción para la receta #{autorizacion.venta_id} fue {resultado}.",
        )
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    return redirect("farmacia:recetas_retenidas")


@require_GET
@login_required
def foto_receta_retenida(request, venta_id):
    venta_id = _validar_pk_ruta(VentaRecetaRetenida, venta_id)
    venta = get_object_or_404(
        VentaRecetaRetenida.objects.select_related("auditor_qf"),
        pk=venta_id,
    )
    es_auditor_autorizado = (
        _es_auditor_qf(request.user)
        and venta.auditor_qf_id == request.user.pk
    )
    if not (_es_jefe_farmacia(request.user) or es_auditor_autorizado):
        raise PermissionDenied

    try:
        archivo = venta.foto_receta.open("rb")
    except OSError as error:
        raise Http404 from error
    try:
        with Image.open(archivo) as imagen:
            content_type = Image.MIME.get(imagen.format)
    except (OSError, Image.DecompressionBombError) as error:
        archivo.close()
        raise Http404 from error
    if content_type not in ("image/jpeg", "image/png", "image/webp"):
        archivo.close()
        raise Http404

    archivo.seek(0)
    respuesta = FileResponse(archivo, content_type=content_type)
    respuesta["X-Content-Type-Options"] = "nosniff"
    respuesta["Cache-Control"] = "private, no-store"
    respuesta["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return respuesta


@manejar_bloqueo_base_datos
@login_required
@require_http_methods(["GET", "POST"])
def consultas_jefe_farmacia(request):
    parametros_pagina = ("pagina_pendientes", "pagina_historial")
    respuesta = _validar_parametros_get(request, set(parametros_pagina))
    if respuesta:
        return respuesta
    paginas = {}
    for parametro in parametros_pagina:
        paginas[parametro], respuesta = _numero_pagina(request, parametro)
        if respuesta:
            return respuesta
    if not (
        request.user.is_superuser
        or request.user.groups.filter(
            name__in=(GRUPO_FARMACEUTICO, *GRUPOS_JEFE_FARMACIA)
        ).exists()
    ):
        raise PermissionDenied

    es_jefe = _es_jefe_farmacia(request.user)
    if request.method == "POST":
        if es_jefe:
            respuesta = _validar_payload_formulario(
                request,
                ("consulta_id", "respuesta"),
            )
            if respuesta:
                return respuesta
            if set(request.POST.keys()).difference(
                {"consulta_id", "respuesta", "csrfmiddlewaretoken"}
            ):
                return HttpResponseBadRequest(
                    "La solicitud incluye campos no admitidos."
                )
            consulta_id = _parse_primary_key(
                ConsultaJefeFarmacia,
                request.POST.get("consulta_id"),
            )
            if consulta_id is None:
                raise Http404("La consulta solicitada no existe.")
            consulta = get_object_or_404(ConsultaJefeFarmacia, pk=consulta_id)
            form = ResponderConsultaJefeForm(request.POST)
            if not form.is_valid():
                errores = [
                    error
                    for campo_errores in form.errors.values()
                    for error in campo_errores
                ]
                messages.error(request, " ".join(errores))
                return redirect("farmacia:consultas_jefe")
            try:
                consulta.responder(
                    request.user,
                    form.cleaned_data["respuesta"],
                )
            except ValidationError as error:
                messages.error(request, " ".join(error.messages))
            else:
                messages.success(request, f"La consulta #{consulta.pk} fue respondida.")
            return redirect("farmacia:consultas_jefe")

        if not request.user.groups.filter(name=GRUPO_FARMACEUTICO).exists():
            raise PermissionDenied

        clave_cooldown = (
            f"farmacia:cooldown:consulta-jefe:{request.user.pk}"
        )
        respuesta = _bloquear_solicitud_si_corresponde(
            request,
            clave_cooldown=clave_cooldown,
            solicitudes_pendientes=ConsultaJefeFarmacia.objects.filter(
                solicitante=request.user,
                estado=ConsultaJefeFarmacia.Estado.PENDIENTE,
            ),
            vista="farmacia:consultas_jefe",
        )
        if respuesta:
            return respuesta

        respuesta = _validar_payload_formulario(
            request,
            ("asunto", "pregunta"),
            ("foto_receta",),
        )
        if respuesta:
            return respuesta

        form = ConsultaJefeFarmaciaForm(request.POST, request.FILES)
        if form.is_valid():
            respuesta = _reservar_cooldown_solicitud(
                request,
                clave_cooldown,
                "farmacia:consultas_jefe",
            )
            if respuesta:
                return respuesta

            consulta = form.save(commit=False)
            consulta.solicitante = request.user
            guardado = False
            try:
                with transaction.atomic():
                    consulta.save()
                guardado = True
            finally:
                if not guardado:
                    _liberar_cooldown_solicitud(clave_cooldown)
            try:
                cache.set(
                    clave_cooldown,
                    "enviado",
                    timeout=COOLDOWN_SOLICITUD_SEGUNDOS,
                )
            except RedisError:
                logger.exception("No se pudo extender el cooldown de la consulta.")
            messages.success(
                request,
                f"Tu consulta #{consulta.pk} fue enviada al Jefe de Farmacia.",
            )
            return redirect("farmacia:consultas_jefe")
    else:
        form = ConsultaJefeFarmaciaForm() if not es_jefe else None

    consultas = ConsultaJefeFarmacia.objects.select_related(
        "solicitante",
        "jefe_farmacia",
    )
    if es_jefe:
        pendientes = consultas.filter(estado=ConsultaJefeFarmacia.Estado.PENDIENTE)
        historial = consultas.filter(estado=ConsultaJefeFarmacia.Estado.RESPONDIDA)
    else:
        pendientes = ConsultaJefeFarmacia.objects.none()
        historial = consultas.filter(solicitante=request.user)

    pendientes, respuesta = _paginar_queryset(
        pendientes.order_by("-fecha_solicitud", "-pk"),
        paginas["pagina_pendientes"],
        25,
    )
    if respuesta:
        return respuesta
    historial, respuesta = _paginar_queryset(
        historial.order_by("-fecha_solicitud", "-pk"),
        paginas["pagina_historial"],
        25,
    )
    if respuesta:
        return respuesta
    consultas_pendientes = ConsultaJefeFarmacia.objects.filter(
        estado=ConsultaJefeFarmacia.Estado.PENDIENTE
    ).count()
    return render(
        request,
        "farmacia/consultas_jefe.html",
        {
            "form": form,
            "es_jefe": es_jefe,
            "pendientes": pendientes,
            "historial": historial,
            "consultas_pendientes": consultas_pendientes,
            "url_gestion_inventario": _url_gestion_inventario(request.user),
        },
    )


@manejar_bloqueo_base_datos
@login_required
@_jefe_farmacia_required
@require_POST
def eliminar_consulta_jefe(request, consulta_id):
    clave_intentos = (
        f"farmacia:rate:eliminar-consulta:usuario:{request.user.pk}"
    )
    try:
        intentos = _incrementar_contador_rate_limit(
            clave_intentos,
            VENTANA_RATE_LIMIT_ELIMINAR_CONSULTA,
        )
    except RedisError:
        logger.exception("El caché del límite de eliminación no responde.")
        return _respuesta_cache_solicitudes_no_disponible()
    if intentos > INTENTOS_MAXIMOS_ELIMINAR_CONSULTA:
        return _respuesta_limite_eliminar_consulta(request)

    consulta_id = _validar_pk_ruta(ConsultaJefeFarmacia, consulta_id)
    consulta = get_object_or_404(ConsultaJefeFarmacia, pk=consulta_id)
    respuesta = _validar_payload_formulario(
        request,
        ("password_confirmacion",),
    )
    if respuesta:
        return respuesta
    if set(request.POST.keys()).difference(
        {"password_confirmacion", "csrfmiddlewaretoken"}
    ):
        return HttpResponseBadRequest(
            "La solicitud incluye campos no admitidos."
        )
    try:
        password_confirmacion = BoundedPasswordField(required=False).clean(
            request.POST.get("password_confirmacion", "")
        )
    except ValidationError:
        messages.error(
            request,
            "La contraseña no es correcta. No se eliminó la consulta.",
        )
        return redirect("farmacia:consultas_jefe")
    if not request.user.check_password(password_confirmacion):
        messages.error(
            request,
            "La contraseña no es correcta. No se eliminó la consulta.",
        )
        return redirect("farmacia:consultas_jefe")

    nombre_imagen = consulta.foto_receta.name
    almacen_imagen = consulta.foto_receta.storage
    try:
        with transaction.atomic():
            consulta.delete()
            if nombre_imagen:
                transaction.on_commit(
                    lambda: _eliminar_archivo_consulta(
                        almacen_imagen,
                        nombre_imagen,
                    )
                )
    except ProtectedError:
        messages.error(
            request,
            "No se puede eliminar la consulta porque tiene registros asociados.",
        )
        return redirect("farmacia:consultas_jefe")

    messages.success(request, f"La consulta #{consulta_id} fue eliminada.")
    return redirect("farmacia:consultas_jefe")


@require_GET
@login_required
def foto_consulta_jefe_farmacia(request, consulta_id):
    consulta_id = _validar_pk_ruta(ConsultaJefeFarmacia, consulta_id)
    consulta = get_object_or_404(ConsultaJefeFarmacia, pk=consulta_id)
    es_jefe = _es_jefe_farmacia(request.user)
    es_solicitante = (
        request.user.groups.filter(name=GRUPO_FARMACEUTICO).exists()
        and consulta.solicitante_id == request.user.pk
    )
    if not (es_jefe or es_solicitante):
        raise PermissionDenied

    try:
        archivo = consulta.foto_receta.open("rb")
    except OSError as error:
        raise Http404 from error
    try:
        with Image.open(archivo) as imagen:
            content_type = Image.MIME.get(imagen.format)
    except (OSError, Image.DecompressionBombError) as error:
        archivo.close()
        raise Http404 from error
    if content_type not in ("image/jpeg", "image/png", "image/webp"):
        archivo.close()
        raise Http404

    archivo.seek(0)
    respuesta = FileResponse(archivo, content_type=content_type)
    respuesta["X-Content-Type-Options"] = "nosniff"
    respuesta["Cache-Control"] = "private, no-store"
    respuesta["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return respuesta


def _obtener_carrito(request):
    carrito = request.session.get("farmacia_caja_carrito", {})
    if not isinstance(carrito, dict):
        carrito = {}
    return carrito


def _obtener_recetas_carrito(request):
    recetas = request.session.get("farmacia_caja_recetas", {})
    if not isinstance(recetas, dict):
        return {}
    return recetas


def _error_caja(request, message, code="invalid_sale"):
    accept = request.headers.get("Accept", "").lower()
    if "application/json" in accept or request.headers.get(
        "X-Requested-With"
    ) == "XMLHttpRequest":
        return JsonResponse(
            {
                "error": code,
                "message": message,
            },
            status=400,
        )
    messages.error(request, message)
    return redirect("farmacia:caja")


def _respuesta_receta_ya_utilizada():
    return JsonResponse(
        {
            "status": "error",
            "codigo": "RECETA_YA_UTILIZADA",
            "mensaje": "Esta receta ya fue procesada en otra transacción.",
        },
        status=400,
    )


def _parse_primary_key(model, value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        primary_key = value
    elif isinstance(value, str):
        value = value.strip()
        if not value or not value.isascii() or not value.isdecimal():
            return None
        try:
            primary_key = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if value != str(primary_key):
            return None
    else:
        return None

    if primary_key < 1:
        return None
    _, maximum = connection.ops.integer_field_range(
        model._meta.pk.get_internal_type()
    )
    if maximum is not None and primary_key > maximum:
        return None
    if primary_key > 2**63 - 1:
        return None
    return primary_key


def _validar_pk_ruta(model, value):
    primary_key = _parse_primary_key(model, value)
    if primary_key is None:
        raise Http404
    return primary_key


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


def _registrar_venta_simulada(
    request,
    carrito,
    recetas_por_medicamento,
    medio_pago,
    checkout_key,
):
    try:
        clave = UUID(checkout_key)
    except (TypeError, ValueError, AttributeError) as error:
        raise ValidationError("La operación de caja no es válida; vuelve a intentarlo.") from error

    venta_existente = VentaCaja.objects.filter(
        idempotency_key=clave,
        cajero=request.user,
    ).first()
    if venta_existente:
        return venta_existente

    clave_sesion = request.session.get("farmacia_caja_checkout_key", "")
    if not isinstance(clave_sesion, str) or not constant_time_compare(
        clave_sesion,
        str(clave),
    ):
        raise ValidationError("La operación ya no está vigente; revisa la caja e inténtalo otra vez.")

    try:
        cantidades = {
            int(medicamento_id): int(cantidad)
            for medicamento_id, cantidad in carrito.items()
        }
    except (TypeError, ValueError) as error:
        raise ValidationError("El carrito contiene productos o cantidades no válidas.") from error
    if not cantidades or any(cantidad < 1 for cantidad in cantidades.values()):
        raise ValidationError("Agrega al menos un producto con cantidad válida.")

    try:
        recetas_por_medicamento = {
            _parse_primary_key(Medicamento, medicamento_id): _parse_primary_key(
                VentaRecetaRetenida,
                receta_id,
            )
            for medicamento_id, receta_id in recetas_por_medicamento.items()
        }
    except (AttributeError, TypeError, ValueError) as error:
        raise ValidationError("Los folios de receta no son válidos.") from error
    if any(
        medicamento_id is None or receta_id is None
        for medicamento_id, receta_id in recetas_por_medicamento.items()
    ):
        raise ValidationError("Los folios de receta no son válidos.")

    if medio_pago not in SimulacionPago.MedioPago.values:
        raise ValidationError("Selecciona un medio de pago válido.")

    hoy = timezone.localdate()
    asignaciones = []
    with transaction.atomic():
        usuario_bloqueado = get_user_model().objects.select_for_update().filter(
            pk=request.user.pk
        ).first()
        if usuario_bloqueado is None:
            raise PermissionDenied
        venta_existente = VentaCaja.objects.filter(
            idempotency_key=clave,
            cajero=request.user,
        ).first()
        if venta_existente:
            return venta_existente

        medicamentos = {
            medicamento.pk: medicamento
            for medicamento in Medicamento.objects.select_for_update()
            .filter(pk__in=cantidades, activo=True)
            .order_by("pk")
        }
        if len(medicamentos) != len(cantidades):
            raise ValidationError("Uno o más productos ya no están activos.")
        medicamentos_controlados = {
            medicamento_id
            for medicamento_id, medicamento in medicamentos.items()
            if medicamento.requiere_receta
        }
        if set(recetas_por_medicamento) != medicamentos_controlados:
            raise ValidationError(
                "Se requiere receta médica aprobada para este producto."
            )
        if len(set(recetas_por_medicamento.values())) != len(
            recetas_por_medicamento
        ):
            raise ValidationError(
                "Cada producto controlado debe usar un folio de receta diferente."
            )

        recetas = {
            receta.pk: receta
            for receta in VentaRecetaRetenida.objects.select_for_update()
            .filter(pk__in=recetas_por_medicamento.values())
            .order_by("pk")
        }
        if len(recetas) != len(recetas_por_medicamento):
            raise ValidationError(
                "Se requiere receta médica aprobada para este producto."
            )
        for medicamento_id, receta_id in recetas_por_medicamento.items():
            medicamento = medicamentos[medicamento_id]
            receta = recetas[receta_id]
            if (
                receta.venta_caja_id is not None
                or receta.estado == VentaRecetaRetenida.Estado.DISPENSADA
                or receta.detalles_dispensacion.exists()
            ):
                raise ValidationError(
                    "Esta receta ya fue procesada en otra transacción.",
                    code="RECETA_YA_UTILIZADA",
                )
            if (
                receta.estado != VentaRecetaRetenida.Estado.APROBADA
                or not _revisor_receta_valido(receta)
            ):
                raise ValidationError(
                    "Se requiere receta médica aprobada y no dispensada para este producto."
                )
            if receta.medicamento_id != medicamento_id:
                raise ValidationError(
                    "El folio de receta no corresponde al medicamento seleccionado."
                )
            if cantidades[medicamento_id] > receta.cantidad:
                raise ValidationError(
                    f"La cantidad solicitada supera las {receta.cantidad} "
                    "unidades aprobadas en la receta."
                )
        if any(medicamento.precio is None for medicamento in medicamentos.values()):
            raise ValidationError("Configura el precio de todos los productos antes de vender.")
        total = sum(
            (
                medicamentos[medicamento_id].precio * cantidad
                for medicamento_id, cantidad in cantidades.items()
            ),
            Decimal("0.00"),
        )
        if total <= 0:
            raise ValidationError("El total de la venta debe ser mayor que cero.")

        lotes = list(
            IngresoInventario.objects.select_for_update()
            .filter(
                medicamento_id__in=cantidades,
                fecha_vencimiento__gte=hoy,
                stock_disponible__gt=0,
            )
            .order_by("medicamento_id", "fecha_vencimiento", "pk")
        )
        lotes_por_medicamento = {}
        for lote in lotes:
            lotes_por_medicamento.setdefault(lote.medicamento_id, []).append(lote)

        recetas_asociadas = set()
        for medicamento_id, cantidad_requerida in cantidades.items():
            medicamento = medicamentos[medicamento_id]
            restante = cantidad_requerida
            for lote in lotes_por_medicamento.get(medicamento_id, []):
                asignada = min(restante, lote.stock_disponible)
                if asignada:
                    receta_id = recetas_por_medicamento.get(medicamento_id)
                    receta = recetas.get(receta_id)
                    if receta_id in recetas_asociadas:
                        receta = None
                    elif receta is not None:
                        recetas_asociadas.add(receta_id)
                    asignaciones.append(
                        (lote, asignada, medicamento.precio, receta)
                    )
                    restante -= asignada
                if restante == 0:
                    break
            if restante:
                disponible = cantidad_requerida - restante
                raise ValidationError(
                    f"Remedio agotado. Stock disponible: {disponible}."
                )

        venta = VentaCaja.objects.create(
            idempotency_key=clave,
            cajero=request.user,
            total=total,
        )
        for lote, cantidad, precio, receta in asignaciones:
            actualizado = IngresoInventario.objects.filter(
                pk=lote.pk,
                stock_disponible__gte=cantidad,
            ).update(stock_disponible=F("stock_disponible") - cantidad)
            if not actualizado:
                raise ValidationError(
                    f"El stock del lote {lote.lote} cambió; vuelve a revisar la caja."
                )
            DetalleVentaCaja.objects.create(
                venta=venta,
                ingreso=lote,
                receta=receta,
                cantidad=cantidad,
                precio_unitario=precio,
            )

        for receta_id in recetas_por_medicamento.values():
            dispensada = VentaRecetaRetenida.objects.filter(
                pk=receta_id,
                estado=VentaRecetaRetenida.Estado.APROBADA,
                venta_caja__isnull=True,
            ).update(
                estado=VentaRecetaRetenida.Estado.DISPENSADA,
                venta_caja=venta,
            )
            if not dispensada:
                raise ValidationError(
                    "La receta ya fue dispensada o dejó de estar aprobada."
                )

        SimulacionPago.objects.create(
            venta_caja=venta,
            monto=total,
            medio_pago=medio_pago,
            estado=SimulacionPago.Estado.APROBADO,
            referencia_simulada=f"SIM-{venta.pk}",
        )
    return venta


@manejar_bloqueo_base_datos
@limitar_frecuencia_solicitudes("caja")
@login_required
@require_http_methods(["GET", "POST"])
def caja(request):
    respuesta = _validar_parametros_get(request, {"q", "page"})
    if respuesta:
        return respuesta
    numero_pagina, respuesta = _numero_pagina(request, "page")
    if respuesta:
        return respuesta
    busquedas = request.GET.getlist("q")
    if len(busquedas) > 1:
        return HttpResponseBadRequest("La búsqueda debe enviarse una sola vez.")
    busqueda = (
        normalizar_texto_seguro(busquedas[0]).strip()
        if busquedas
        else ""
    )
    if len(busqueda) > 200:
        return HttpResponseBadRequest(
            "La búsqueda no puede superar los 200 caracteres."
        )

    if not _es_personal_farmacia(request.user):
        raise PermissionDenied

    carrito = _obtener_carrito(request)
    recetas_carrito = _obtener_recetas_carrito(request)
    if request.method == "POST":
        respuesta = _validar_payload_formulario(
            request,
            (
                "accion",
                "medicamento_id",
                "cantidad",
                "receta_id",
                "medio_pago",
                "checkout_key",
            ),
        )
        if respuesta:
            return respuesta

        accion = request.POST.get("accion")
        if accion == "agregar":
            medicamento_id = _parse_primary_key(
                Medicamento,
                request.POST.get("medicamento_id"),
            )
            if medicamento_id is None:
                return _error_caja(
                    request,
                    "Selecciona un medicamento válido.",
                    "invalid_medicine_id",
                )
            medicamento = get_object_or_404(
                Medicamento,
                pk=medicamento_id,
                activo=True,
            )
            try:
                cantidad = int(request.POST.get("cantidad", "1"))
            except (TypeError, ValueError):
                cantidad = 0

            if cantidad < 1:
                return _error_caja(
                    request,
                    "Ingresa una cantidad mayor que cero.",
                    "invalid_quantity",
                )

            stock = _medicamentos_con_stock().filter(pk=medicamento.pk).values_list(
                "stock_vendible",
                flat=True,
            ).first() or 0
            try:
                cantidad_actual = int(carrito.get(str(medicamento.pk), 0))
            except (TypeError, ValueError):
                cantidad_actual = 0

            receta_id = None
            if medicamento.requiere_receta:
                receta_id = _parse_primary_key(
                    VentaRecetaRetenida,
                    request.POST.get("receta_id"),
                )
                if receta_id is None:
                    return _error_caja(
                        request,
                        "Se requiere receta médica aprobada para este producto.",
                        "prescription_required",
                    )
                receta = VentaRecetaRetenida.objects.filter(
                    pk=receta_id,
                    medicamento=medicamento,
                    estado=VentaRecetaRetenida.Estado.APROBADA,
                    venta_caja__isnull=True,
                    revisado_por__isnull=False,
                ).select_related(
                    "revisado_por",
                    "auditor_qf",
                ).first()
                if receta is None or not _revisor_receta_valido(receta):
                    return _error_caja(
                        request,
                        "Se requiere receta médica aprobada para este producto.",
                        "prescription_required",
                    )
                receta_actual = recetas_carrito.get(str(medicamento.pk))
                if receta_actual is not None and str(receta_actual) != str(receta_id):
                    return _error_caja(
                        request,
                        "Cada medicamento del pedido debe conservar el mismo folio de receta.",
                        "prescription_mismatch",
                    )
                if cantidad_actual + cantidad > receta.cantidad:
                    return _error_caja(
                        request,
                        f"La receta autoriza hasta {receta.cantidad} unidad(es).",
                        "prescription_quantity_exceeded",
                    )

            if cantidad_actual + cantidad > stock:
                return _error_caja(
                    request,
                    f"Remedio agotado. Stock disponible: {stock}.",
                    "insufficient_stock",
                )

            carrito[str(medicamento.pk)] = cantidad_actual + cantidad
            request.session["farmacia_caja_carrito"] = carrito
            if receta_id is not None:
                recetas_carrito[str(medicamento.pk)] = receta_id
                request.session["farmacia_caja_recetas"] = recetas_carrito
            messages.success(request, f"{medicamento.nombre} agregado a la caja.")
            return redirect("farmacia:caja")

        if accion == "quitar":
            medicamento_id = request.POST.get("medicamento_id")
            carrito.pop(medicamento_id, None)
            recetas_carrito.pop(medicamento_id, None)
            request.session["farmacia_caja_carrito"] = carrito
            request.session["farmacia_caja_recetas"] = recetas_carrito
            messages.info(request, "Producto quitado de la caja.")
            return redirect("farmacia:caja")

        if accion == "vaciar":
            request.session["farmacia_caja_carrito"] = {}
            request.session["farmacia_caja_recetas"] = {}
            messages.info(request, "La caja quedó vacía.")
            return redirect("farmacia:caja")

        if accion == "confirmar_venta":
            medio_pago = request.POST.get("medio_pago")
            checkout_key = request.POST.get("checkout_key")
            try:
                venta = _registrar_venta_simulada(
                    request,
                    carrito,
                    recetas_carrito,
                    medio_pago,
                    checkout_key,
                )
            except ValidationError as error:
                if error.code == "RECETA_YA_UTILIZADA":
                    return _respuesta_receta_ya_utilizada()
                return _error_caja(
                    request,
                    " ".join(error.messages),
                    "invalid_sale",
                )
            except IntegrityError:
                receta_ids = [
                    receta_id
                    for receta_id in (
                        _parse_primary_key(VentaRecetaRetenida, raw_id)
                        for raw_id in recetas_carrito.values()
                    )
                    if receta_id is not None
                ]
                receta_ya_utilizada = VentaRecetaRetenida.objects.filter(
                    pk__in=receta_ids,
                ).filter(
                    Q(venta_caja__isnull=False)
                    | Q(estado=VentaRecetaRetenida.Estado.DISPENSADA)
                    | Q(detalles_dispensacion__isnull=False)
                ).exists()
                if not receta_ya_utilizada:
                    raise
                return _respuesta_receta_ya_utilizada()

            request.session["farmacia_caja_carrito"] = {}
            request.session["farmacia_caja_recetas"] = {}
            request.session["farmacia_caja_checkout_key"] = str(uuid4())
            messages.success(
                request,
                f"Venta #{venta.pk} registrada. Pago simulado aprobado por "
                f"${venta.total:,.0f}.".replace(",", "."),
            )
            return redirect("farmacia:caja")

        return _error_caja(
            request,
            "No se reconoció la acción solicitada.",
            "invalid_action",
        )

    medicamentos = _medicamentos_con_stock().order_by("nombre")
    if busqueda:
        medicamentos = medicamentos.filter(
            Q(nombre__icontains=busqueda)
            | Q(codigo__icontains=busqueda)
        )
    pagina_medicamentos, respuesta = _paginar_queryset(
        medicamentos,
        numero_pagina,
        50,
    )
    if respuesta:
        return respuesta

    productos_carrito, total_carrito, unidades_carrito = _datos_carrito(carrito)
    for producto in productos_carrito:
        producto["receta_id"] = recetas_carrito.get(
            str(producto["medicamento"].pk)
        )
    checkout_key = request.session.get("farmacia_caja_checkout_key")
    if not checkout_key:
        checkout_key = str(uuid4())
        request.session["farmacia_caja_checkout_key"] = checkout_key
    pagos_recientes = (
        SimulacionPago.objects.filter(venta_caja__isnull=False)
        .select_related("venta_caja", "venta_caja__cajero")
        .order_by("-fecha_simulacion")[:20]
    )
    return render(
        request,
        "farmacia/caja.html",
        {
            "medicamentos": pagina_medicamentos,
            "pagina_medicamentos": pagina_medicamentos,
            "busqueda": busqueda,
            "productos_carrito": productos_carrito,
            "total_carrito": total_carrito,
            "unidades_carrito": unidades_carrito,
            "checkout_key": checkout_key,
            "medios_pago": SimulacionPago.MedioPago.choices,
            "carrito_tiene_precios": all(
                producto["subtotal"] is not None for producto in productos_carrito
            ),
            "pagos_recientes": pagos_recientes,
            "url_gestion_inventario": _url_gestion_inventario(request.user),
        },
    )
