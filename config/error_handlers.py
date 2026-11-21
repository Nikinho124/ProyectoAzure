import logging
from functools import wraps

from django.db.utils import OperationalError
from django.http import JsonResponse
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
