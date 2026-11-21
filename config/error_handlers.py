from django.http import JsonResponse
from django.shortcuts import render


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
