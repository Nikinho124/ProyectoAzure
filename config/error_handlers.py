import logging
from functools import wraps

from django.db.utils import OperationalError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render


logger = logging.getLogger(__name__)


def _es_bloqueo_de_base_de_datos(error):
    mensaje = str(error).casefold()
    return any(
        causa in mensaje
        for causa in (
            "database is locked",
            "database table is locked",
            "database schema is locked",
            "database is busy",
        )
    )


def respuesta_bloqueo_base_datos(request, error):
    if not _es_bloqueo_de_base_de_datos(error):
        return None

    logger.warning("SQLite rechazó una escritura por bloqueo temporal.")
    respuesta = JsonResponse(
        {
            "status": "error",
            "codigo": "CONCURRENCIA_EXCESIVA",
            "mensaje": (
                "El sistema se encuentra procesando múltiples solicitudes. "
                "Por favor reintente en unos segundos."
            ),
        },
        status=429,
    )
    respuesta["Retry-After"] = "2"
    return respuesta


def manejar_bloqueo_base_datos(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        try:
            return view_func(request, *args, **kwargs)
        except OperationalError as error:
            respuesta = respuesta_bloqueo_base_datos(request, error)
            if respuesta is None:
                raise
            return respuesta

    return wrapped


def _solicita_json(request):
    for accepted_type in request.headers.get("Accept", "").split(","):
        media_type, *parameters = accepted_type.strip().split(";")
        if media_type.strip().lower() != "application/json":
            continue
        quality = next(
            (
                parameter.strip()[2:]
                for parameter in parameters
                if parameter.strip().lower().startswith("q=")
            ),
            "1",
        )
        try:
            if float(quality) <= 0:
                continue
        except ValueError:
            continue
        return True
    return False


def handler404(request, exception):
    if _solicita_json(request):
        return JsonResponse(
            {
                "error": "not_found",
                "message": "La ruta solicitada no existe.",
            },
            status=404,
        )
    return render(request, "404.html", status=404)


def handler400(request, exception):
    if _solicita_json(request):
        respuesta = JsonResponse(
            {
                "error": "bad_request",
                "message": "La solicitud no tiene un formato válido.",
            },
            status=400,
        )
    else:
        respuesta = HttpResponse(
            "<!doctype html><title>Solicitud no válida</title>"
            "<h1>Solicitud no válida</h1>",
            status=400,
        )
    respuesta["Cache-Control"] = "no-store"
    return respuesta


def handler403(request, exception):
    if _solicita_json(request):
        respuesta = JsonResponse(
            {
                "error": "forbidden",
                "message": "No tienes permiso para acceder a este recurso.",
            },
            status=403,
        )
    else:
        respuesta = HttpResponse(
            "<!doctype html><title>Acceso denegado</title>"
            "<h1>Acceso denegado</h1>",
            status=403,
        )
    respuesta["Cache-Control"] = "no-store"
    return respuesta


def handler500(request):
    if _solicita_json(request):
        return JsonResponse(
            {
                "error": "internal_server_error",
                "message": "Ocurrió un error inesperado. Inténtalo más tarde.",
            },
            status=500,
        )
    return render(request, "500.html", status=500)
