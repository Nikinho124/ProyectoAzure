from django.apps import AppConfig
from django.conf import settings
from django.core.checks import Tags, Warning, register
from django.db.utils import OperationalError


@register(Tags.security, deploy=True)
def verificar_cache_compartido(app_configs, **kwargs):
    backend = settings.CACHES["default"]["BACKEND"]
    if not settings.DEBUG and backend.endswith(".LocMemCache"):
        return [
            Warning(
                "El rate limit usa caché local y no se comparte entre workers.",
                hint="Configura DJANGO_REDIS_URL para despliegues multi-worker.",
                id="farmacia.W001",
            )
        ]
    return []


def configurar_pragmas_sqlite(sender, connection, **kwargs):
    if connection.vendor != "sqlite":
        return

    nombre_base_datos = str(connection.settings_dict["NAME"])
    es_memoria = (
        nombre_base_datos == ":memory:"
        or "mode=memory" in nombre_base_datos
    )
    with connection.cursor() as cursor:
        if not es_memoria:
            cursor.execute("PRAGMA journal_mode")
            modo_actual = cursor.fetchone()[0].lower()
            if modo_actual != "wal":
                try:
                    cursor.execute("PRAGMA journal_mode=WAL")
                    modo_actual = cursor.fetchone()[0].lower()
                except OperationalError:
                    cursor.execute("PRAGMA journal_mode")
                    modo_actual = cursor.fetchone()[0].lower()
                    if modo_actual != "wal":
                        raise
                if modo_actual != "wal":
                    raise OperationalError(
                        "No fue posible activar WAL para la base SQLite."
                    )
        cursor.execute("PRAGMA synchronous=FULL")


class FarmaciaConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "farmacia"

    def ready(self):
        from django.db.backends.signals import connection_created

        connection_created.connect(
            configurar_pragmas_sqlite,
            dispatch_uid="farmacia.configurar_pragmas_sqlite",
        )
