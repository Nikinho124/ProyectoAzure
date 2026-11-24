from datetime import timedelta
from decimal import Decimal
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.hashers import (
    PBKDF2PasswordHasher,
    check_password,
    identify_hasher,
    make_password,
)
from django.contrib.contenttypes.models import ContentType
from django.conf import settings
from django.core.checks import Tags, run_checks
from django.core.exceptions import (
    PermissionDenied,
    SuspiciousOperation,
    ValidationError,
)
from django.core.cache import cache
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.db.backends.sqlite3.base import DatabaseWrapper
from django.db.models.deletion import ProtectedError
from django.db.utils import OperationalError
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from PIL import Image
from redis.exceptions import RedisError

from farmacia.auth_forms import EmployeeAuthenticationForm
from farmacia.consulta_forms import ConsultaJefeFarmaciaForm
from farmacia.models import (
    GRUPO_FARMACEUTICO,
    GRUPO_JEFE_FARMACIA,
    AutorizacionExcepcional,
    AuditoriaInventario,
    ConsultaJefeFarmacia,
    IngresoInventario,
    MAX_RECETA_UPLOAD_BYTES,
    MAX_RECETA_IMAGE_PIXELS,
    Proveedor,
    DetalleVentaCaja,
    Medicamento,
    Merma,
    SimulacionPago,
    VentaRecetaRetenida,
    VentaCaja,
    validar_archivo_receta,
)
from farmacia.receta_forms import RevisionRecetaForm, VentaRecetaRetenidaForm

User = get_user_model()


class AccesoYFarmaciaTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = User.objects.create_superuser(
            username="jefe",
            email="jefe@example.com",
            password="Admin-password-123",
        )
        self.proveedor = Proveedor.objects.create(nombre="Proveedor de prueba")

    def test_login_es_pagina_inicial_sin_registro_ni_catalogo_publico(self):
        respuesta = self.client.get(reverse("login"))

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Acceso a tu cuenta")
        self.assertNotContains(respuesta, "Registrarse")
        self.assertNotContains(respuesta, "catalogo")
        self.assertNotContains(respuesta, "autofocus")
        self.assertContains(respuesta, "pie-sitio")
        self.assertContains(respuesta, "Tel&eacute;fono")
        self.assertContains(respuesta, "farmacia/sitio.css?v=3")

    def test_cabeceras_y_opciones_de_cookies_de_seguridad(self):
        respuesta = self.client.get(reverse("login"))

        self.assertEqual(respuesta["X-Frame-Options"], "DENY")
        self.assertEqual(respuesta["X-Content-Type-Options"], "nosniff")
        self.assertEqual(
            respuesta["Cross-Origin-Opener-Policy"],
            "same-origin",
        )
        self.assertEqual(respuesta["Referrer-Policy"], "same-origin")
        self.assertIn("default-src 'self'", respuesta["Content-Security-Policy"])
        self.assertEqual(settings.X_FRAME_OPTIONS, "DENY")
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
        self.assertTrue(settings.CSRF_COOKIE_HTTPONLY)
        self.assertEqual(settings.SESSION_COOKIE_SAMESITE, "Lax")
        self.assertEqual(settings.CSRF_COOKIE_SAMESITE, "Lax")
        self.assertTrue(settings.SESSION_EXPIRE_AT_BROWSER_CLOSE)
        self.assertEqual(settings.SECURE_REFERRER_POLICY, "same-origin")
        self.assertEqual(
            settings.SECURE_CROSS_ORIGIN_OPENER_POLICY,
            "same-origin",
        )
        self.assertEqual(settings.DATA_UPLOAD_MAX_MEMORY_SIZE, 2_621_440)
        self.assertEqual(settings.FILE_UPLOAD_MAX_MEMORY_SIZE, 2_621_440)
        self.assertEqual(settings.DATA_UPLOAD_MAX_NUMBER_FIELDS, 100)
        self.assertEqual(settings.DATA_UPLOAD_MAX_NUMBER_FILES, 10)
        self.assertIn(
            "LocMemCache",
            settings.CACHES["default"]["BACKEND"],
        )
        self.assertEqual(
            settings.DATABASES["default"]["OPTIONS"]["timeout"],
            5,
        )
        csrf_cookie = respuesta.cookies[settings.CSRF_COOKIE_NAME]
        self.assertEqual(csrf_cookie["httponly"], True)

    def test_debug_env_false_se_interpreta_como_booleano_false(self):
        from config.settings import _environment_bool

        with patch.dict(os.environ, {"TEST_DJANGO_DEBUG": "False"}):
            self.assertFalse(_environment_bool("TEST_DJANGO_DEBUG", True))

    def test_pbkdf2_prioritario_usa_mas_iteraciones_y_verifica_hashes_anteriores(self):
        nuevo_hash = make_password("Clave-segura-123")
        hasher = identify_hasher(nuevo_hash)
        hash_anterior = PBKDF2PasswordHasher().encode(
            "Clave-anterior-123",
            "sal-de-prueba",
        )

        self.assertEqual(
            settings.PASSWORD_HASHERS[0],
            "farmacia.hashers.FarmaciaPBKDF2PasswordHasher",
        )
        self.assertEqual(hasher.iterations, 1_200_000)
        self.assertTrue(check_password("Clave-segura-123", nuevo_hash))
        self.assertTrue(check_password("Clave-anterior-123", hash_anterior))

    def test_conexiones_sqlite_activan_wal_y_synchronous_full(self):
        with tempfile.TemporaryDirectory() as directorio_temporal:
            nombre_base_datos = str(
                Path(directorio_temporal) / "wal-verificacion.sqlite3"
            )
            for indice in range(2):
                configuracion = settings.DATABASES["default"].copy()
                configuracion["NAME"] = nombre_base_datos
                conexion = DatabaseWrapper(
                    configuracion,
                    alias=f"wal_verificacion_{indice}",
                )
                try:
                    with conexion.cursor() as cursor:
                        cursor.execute("PRAGMA journal_mode")
                        modo_journal = cursor.fetchone()[0].lower()
                        cursor.execute("PRAGMA synchronous")
                        nivel_synchronous = cursor.fetchone()[0]
                finally:
                    conexion.close()

                self.assertEqual(modo_journal, "wal")
                self.assertEqual(nivel_synchronous, 2)

    def test_login_falla_cerrado_si_el_cache_compartido_no_responde(self):
        with patch(
            "farmacia.views._incrementar_contador_rate_limit",
            side_effect=RedisError("Redis no disponible"),
        ), patch("farmacia.views.logger.exception"):
            respuesta = self.client.post(
                reverse("login"),
                {"username": "usuario", "password": "clave"},
                HTTP_ACCEPT="application/json",
            )

        self.assertEqual(respuesta.status_code, 503)
        self.assertEqual(
            respuesta.json()["codigo"],
            "LIMITADOR_NO_DISPONIBLE",
        )
        self.assertNotIn(b"Redis no disponible", respuesta.content)
        self.assertNotIn(b"Traceback", respuesta.content)

    def test_manejadores_400_y_403_no_exponen_excepciones(self):
        from config.error_handlers import handler400, handler403
        from django.contrib.messages.storage.fallback import FallbackStorage

        for accept in ("application/json", "text/html"):
            with self.subTest(accept=accept):
                request = RequestFactory().get(
                    "/ruta-interna/",
                    HTTP_ACCEPT=accept,
                )
                request.user = self.admin
                request.session = self.client.session
                request.resolver_match = None
                request._messages = FallbackStorage(request)
                respuesta_400 = handler400(
                    request,
                    SuspiciousOperation("RUTA_INTERNA_PRIVADA"),
                )
                respuesta_403 = handler403(
                    request,
                    PermissionDenied("DETALLE_PRIVADO"),
                )

                self.assertEqual(respuesta_400.status_code, 400)
                self.assertEqual(respuesta_403.status_code, 403)
                self.assertNotIn(b"RUTA_INTERNA_PRIVADA", respuesta_400.content)
                self.assertNotIn(b"DETALLE_PRIVADO", respuesta_403.content)
                self.assertEqual(respuesta_400["Cache-Control"], "no-store")
                self.assertEqual(respuesta_403["Cache-Control"], "no-store")
                if accept == "text/html":
                    self.assertContains(
                        respuesta_403,
                        "Error 403",
                        status_code=403,
                    )
                    self.assertContains(
                        respuesta_403,
                        "No posees los permisos necesarios",
                        status_code=403,
                    )
                    self.assertContains(
                        respuesta_403,
                        "Volver al Panel Principal",
                        status_code=403,
                    )

    def test_check_deploy_advierte_si_rate_limit_usa_cache_local_en_produccion(self):
        with override_settings(DEBUG=False):
            avisos = run_checks(
                tags=[Tags.security],
                include_deployment_checks=True,
            )

        self.assertIn(
            "farmacia.W001",
            {aviso.id for aviso in avisos},
        )

    @override_settings(ALLOWED_HOSTS=["localhost", "127.0.0.1", "[::1]"])
    def test_login_rechaza_un_host_que_no_esta_permitido(self):
        respuesta = self.client.get(
            reverse("login"),
            HTTP_HOST="sitio-no-autorizado.example",
        )

        self.assertEqual(respuesta.status_code, 400)

    def test_login_cambia_el_id_de_sesion_al_autenticar(self):
        clave_intentos = f"failed_login_{self.admin.username}"
        cache.set(clave_intentos, 3, timeout=900)
        session = self.client.session
        session["dato_anonimo"] = "valor previo al login"
        session.save()
        session_key_anterior = session.session_key

        respuesta = self.client.post(
            reverse("login"),
            {
                "username": self.admin.username,
                "password": "Admin-password-123",
            },
        )

        self.assertRedirects(
            respuesta,
            reverse("admin:index"),
            fetch_redirect_response=False,
        )
        self.assertNotEqual(
            self.client.cookies[settings.SESSION_COOKIE_NAME].value,
            session_key_anterior,
        )
        self.assertIsNone(cache.get(clave_intentos))

    def test_login_bloquea_despues_de_cinco_intentos_por_usuario(self):
        login_url = reverse("login")
        for intento in range(settings.FARMACIA_LOGIN_MAX_FAILED_ATTEMPTS):
            respuesta = self.client.post(
                login_url,
                {
                    "username": self.admin.username,
                    "password": f"Clave-incorrecta-{intento}",
                },
            )
            if intento < settings.FARMACIA_LOGIN_MAX_FAILED_ATTEMPTS - 1:
                self.assertEqual(respuesta.status_code, 200)
            else:
                self.assertEqual(respuesta.status_code, 429)
                self.assertContains(
                    respuesta,
                    "Has superado el límite de 5 intentos fallidos.",
                    status_code=429,
                )
                self.assertEqual(
                    respuesta["Retry-After"],
                    str(settings.FARMACIA_LOGIN_LOCKOUT_SECONDS),
                )

        respuesta = self.client.post(
            login_url,
            {
                "username": self.admin.username,
                "password": "Admin-password-123",
            },
        )

        self.assertEqual(respuesta.status_code, 429)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(
            cache.get(f"failed_login_{self.admin.username}"),
            settings.FARMACIA_LOGIN_MAX_FAILED_ATTEMPTS,
        )

    def test_login_no_permite_evitar_bloqueo_agregando_espacios_al_usuario(self):
        clave_intentos = f"failed_login_{self.admin.username}"
        cache.set(
            clave_intentos,
            settings.FARMACIA_LOGIN_MAX_FAILED_ATTEMPTS,
            timeout=settings.FARMACIA_LOGIN_LOCKOUT_SECONDS,
        )

        respuesta = self.client.post(
            reverse("login"),
            {
                "username": f"{self.admin.username}   ",
                "password": "Admin-password-123",
            },
        )

        self.assertEqual(respuesta.status_code, 429)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(
            cache.get(clave_intentos),
            settings.FARMACIA_LOGIN_MAX_FAILED_ATTEMPTS,
        )

    def test_login_usa_el_mismo_mensaje_para_credenciales_invalidas(self):
        usuario_no_autorizado = User.objects.create_user(
            username="usuario_no_autorizado",
            password="Usuario-password-123",
        )
        intentos = (
            ("usuario_inexistente", "Cualquier-password-123"),
            (self.admin.username, "Contraseña-incorrecta-123"),
            (usuario_no_autorizado.username, "Usuario-password-123"),
        )
        errores = []

        for username, password in intentos:
            respuesta = self.client.post(
                reverse("login"),
                {"username": username, "password": password},
            )
            self.assertEqual(respuesta.status_code, 200)
            self.assertContains(respuesta, "Usuario o contraseña incorrectos.")
            errores.append(str(respuesta.context["form"].non_field_errors()))

        self.assertEqual(len(set(errores)), 1)

    def test_login_y_caja_responden_429_al_superar_quince_post_por_minuto(self):
        login_url = reverse("login")
        for intento in range(settings.FARMACIA_RATE_LIMIT_REQUESTS):
            respuesta = self.client.post(
                login_url,
                {
                    "username": f"usuario_inexistente_{intento}",
                    "password": f"Clave-invalida-{intento}",
                },
                HTTP_ACCEPT="application/json",
            )
            self.assertEqual(respuesta.status_code, 200)

        respuesta = self.client.post(
            login_url,
            {
                "username": "usuario_inexistente_final",
                "password": "Clave-invalida",
            },
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(respuesta.status_code, 429)
        self.assertEqual(
            respuesta.json()["codigo"],
            "LIMITE_DE_SOLICITUDES",
        )
        self.assertNotIn(b"Traceback", respuesta.content)
        self.assertEqual(
            respuesta["Retry-After"],
            str(settings.FARMACIA_RATE_LIMIT_WINDOW_SECONDS),
        )

        cache.clear()
        quimico = self.crear_quimico("quimico_rate_limit_caja")
        self.client.force_login(quimico)
        caja_url = reverse("farmacia:caja")
        for _ in range(settings.FARMACIA_RATE_LIMIT_REQUESTS):
            respuesta = self.client.post(
                caja_url,
                {"accion": "vaciar"},
            )
            self.assertEqual(respuesta.status_code, 302)

        respuesta = self.client.post(
            caja_url,
            {"accion": "vaciar"},
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(respuesta.status_code, 429)
        self.assertEqual(
            respuesta.json()["codigo"],
            "LIMITE_DE_SOLICITUDES",
        )
        self.assertNotIn(b"Traceback", respuesta.content)

    def test_sesion_tiene_expiracion_de_ocho_horas_y_cookies_protegidas(self):
        self.assertEqual(settings.SESSION_COOKIE_AGE, 28800)
        self.assertTrue(settings.SESSION_EXPIRE_AT_BROWSER_CLOSE)

        respuesta = self.client.post(
            reverse("login"),
            {
                "username": self.admin.username,
                "password": "Admin-password-123",
            },
        )
        self.assertEqual(respuesta.status_code, 302)
        cookie_sesion = respuesta.cookies[settings.SESSION_COOKIE_NAME]
        self.assertTrue(cookie_sesion["httponly"])
        self.assertEqual(cookie_sesion["samesite"], "Lax")
        self.assertEqual(
            bool(cookie_sesion["secure"]),
            settings.SESSION_COOKIE_SECURE,
        )
        self.assertEqual(self.client.session.get_expiry_age(), 28800)

    def test_login_rechaza_redireccion_externa_en_next(self):
        quimico = self.crear_quimico("quimico_next_externo")

        for destino in (
            "http://sitio-malicioso.com/",
            "//sitio-malicioso.com/",
            "\\\\sitio-malicioso.com\\",
        ):
            with self.subTest(destino=destino):
                respuesta = Client().post(
                    reverse("login"),
                    {
                        "username": quimico.username,
                        "password": "Quimico-password-123",
                        "next": destino,
                    },
                )
                self.assertEqual(respuesta.status_code, 302)
                self.assertEqual(
                    respuesta["Location"],
                    reverse("farmacia:inicio"),
                )

    def test_politica_csp_usa_nonce_en_scripts_y_estilos_inline(self):
        quimico = self.crear_quimico("quimico_nonce_csp")
        self.client.force_login(quimico)

        caja = self.client.get(reverse("farmacia:caja"))
        nonce_caja = caja.wsgi_request.csp_nonce
        self.assertIn(
            f"script-src 'self' 'nonce-{nonce_caja}'",
            caja["Content-Security-Policy"],
        )
        self.assertContains(
            caja,
            f'<script nonce="{nonce_caja}">',
            html=False,
        )

        recetas = self.client.get(reverse("farmacia:recetas_retenidas"))
        nonce_recetas = recetas.wsgi_request.csp_nonce
        self.assertContains(
            recetas,
            f'<style nonce="{nonce_recetas}">',
            html=False,
        )
        self.assertIn(
            f"style-src 'self' 'nonce-{nonce_recetas}'",
            recetas["Content-Security-Policy"],
        )

    def test_parametros_de_pagina_y_ids_fuera_de_rango_responden_controlados(self):
        self.client.force_login(self.admin)
        casos_get_invalidos = (
            reverse("farmacia:caja")
            + "?page=999999999999999999999",
            reverse("farmacia:caja") + "?id=abc",
            reverse("farmacia:recetas_retenidas")
            + "?pagina_pendientes=1.5",
            reverse("farmacia:consultas_jefe")
            + "?pagina_historial=abc",
        )
        for url in casos_get_invalidos:
            with self.subTest(url=url):
                respuesta = self.client.get(url)
                self.assertEqual(respuesta.status_code, 400)

        id_fuera_de_rango = 2**63
        respuesta = self.client.get(
            f"/farmacia/recetas-retenidas/{id_fuera_de_rango}/foto/"
        )
        self.assertEqual(respuesta.status_code, 404)

    def test_catalogos_de_inicio_y_caja_se_paginan_en_bloques_de_cincuenta(self):
        Medicamento.objects.bulk_create(
            [
                Medicamento(
                    codigo=f"PAG-{indice:03}",
                    nombre=f"Producto paginado {indice:03}",
                )
                for indice in range(51)
            ]
        )
        self.client.force_login(self.admin)

        primera_pagina = self.client.get(reverse("farmacia:inicio"))
        segunda_pagina = self.client.get(
            reverse("farmacia:inicio"),
            {"page": "2"},
        )
        self.assertEqual(len(primera_pagina.context["medicamentos"]), 50)
        self.assertEqual(len(segunda_pagina.context["medicamentos"]), 1)
        self.assertContains(primera_pagina, "Páginas del catálogo", html=False)

        primera_pagina_caja = self.client.get(reverse("farmacia:caja"))
        segunda_pagina_caja = self.client.get(
            reverse("farmacia:caja"),
            {"page": "2"},
        )
        self.assertEqual(
            len(primera_pagina_caja.context["medicamentos"]),
            50,
        )
        self.assertEqual(len(segunda_pagina_caja.context["medicamentos"]), 1)

    def test_campos_de_texto_y_decision_rechazan_payloads_masivos(self):
        with self.assertRaises(ValidationError):
            ConsultaJefeFarmaciaForm.base_fields["asunto"].clean("x" * 101)
        with self.assertRaises(ValidationError):
            ConsultaJefeFarmaciaForm.base_fields["pregunta"].clean("x" * 501)
        with self.assertRaises(ValidationError):
            VentaRecetaRetenidaForm.base_fields["motivo_excepcion"].clean(
                "x" * 2001
            )
        with self.assertRaises(ValidationError):
            RevisionRecetaForm.base_fields["decision"].clean(
                "aprobar" + "x" * 10000
            )

    def test_passwords_limitan_longitud_y_rechazan_caracteres_de_control(self):
        from farmacia.admin import FarmaUserCreationForm
        from farmacia.auth_forms import BoundedSetPasswordForm
        from farmacia.inventario_forms import (
            AjustarStockLoteForm,
            RevertirMermaForm,
        )
        from farmacia.admin import FarmaAdminAuthenticationForm

        password_fields = (
            EmployeeAuthenticationForm().fields["password"],
            FarmaAdminAuthenticationForm().fields["password"],
            AjustarStockLoteForm().fields["password_confirmacion"],
            RevertirMermaForm().fields["password_confirmacion"],
            FarmaUserCreationForm().fields["password1"],
            FarmaUserCreationForm().fields["password2"],
            BoundedSetPasswordForm(user=self.admin).fields["new_password1"],
            BoundedSetPasswordForm(user=self.admin).fields["new_password2"],
        )
        for field in password_fields:
            with self.subTest(field=field.label):
                self.assertEqual(field.max_length, 20)
                self.assertEqual(field.widget.attrs["maxlength"], "20")
                with self.assertRaises(ValidationError):
                    field.clean("x" * 21)
                with self.assertRaises(ValidationError):
                    field.clean("valid\x00password")

    def test_consulta_limita_caracteres_y_rechaza_html_y_controles(self):
        formulario = ConsultaJefeFarmaciaForm()

        self.assertEqual(formulario.fields["asunto"].max_length, 100)
        self.assertEqual(formulario.fields["pregunta"].max_length, 500)
        self.assertIn('maxlength="100"', str(formulario["asunto"]))
        self.assertIn('maxlength="500"', str(formulario["pregunta"]))
        with self.assertRaises(ValidationError):
            formulario.fields["pregunta"].clean("<script>alert(1)</script>")
        with self.assertRaises(ValidationError):
            formulario.fields["asunto"].clean("texto\x00inválido")

    def test_password_largo_o_html_en_respuesta_no_se_guarda(self):
        jefe = self.crear_jefe("jefe_texto_consulta_endurecido")
        quimico = self.crear_quimico("quimico_texto_consulta_endurecido")
        consulta = ConsultaJefeFarmacia.objects.create(
            solicitante=quimico,
            asunto="Consulta válida",
            pregunta="Pregunta válida",
        )
        self.client.force_login(jefe)

        respuesta = self.client.post(
            reverse("farmacia:consultas_jefe"),
            {
                "consulta_id": consulta.pk,
                "respuesta": "<script>alert(1)</script>",
            },
            follow=True,
        )

        self.assertContains(respuesta, "No se permiten etiquetas HTML")
        consulta.refresh_from_db()
        self.assertEqual(consulta.estado, ConsultaJefeFarmacia.Estado.PENDIENTE)
        self.assertEqual(consulta.respuesta, "")

    def test_eliminar_consulta_rechaza_password_mayor_a_20_caracteres(self):
        jefe = self.crear_jefe("jefe_password_largo_eliminacion")
        quimico = self.crear_quimico("quimico_password_largo_eliminacion")
        consulta = ConsultaJefeFarmacia.objects.create(
            solicitante=quimico,
            asunto="Consulta válida",
            pregunta="Pregunta válida",
        )
        self.client.force_login(jefe)

        respuesta = self.client.post(
            reverse("farmacia:eliminar_consulta_jefe", args=(consulta.pk,)),
            {"password_confirmacion": "x" * 21},
            follow=True,
        )

        self.assertContains(respuesta, "La contraseña no es correcta.")
        self.assertTrue(
            ConsultaJefeFarmacia.objects.filter(pk=consulta.pk).exists()
        )

    def test_formularios_de_usuario_admin_limitan_password_a_20(self):
        from farmacia.admin import FarmaUserCreationForm

        formulario = FarmaUserCreationForm(
            data={
                "username": "usuario_nuevo_limitado",
                "password1": "x" * 21,
                "password2": "x" * 21,
            }
        )

        self.assertFalse(formulario.is_valid())
        self.assertIn("password1", formulario.errors)
        self.assertIn("password2", formulario.errors)

    def test_formularios_no_exponen_campos_de_rol_o_estado_interno(self):
        from farmacia.admin import FarmaGroupChangeForm, FarmaUserChangeForm

        campos_privilegiados = {
            "is_staff",
            "is_superuser",
            "groups",
            "user_permissions",
            "auditor_qf",
            "jefe_farmacia",
            "revisado_por",
            "estado",
            "solicitante",
            "fecha_respuesta",
        }
        formularios = (
            EmployeeAuthenticationForm(),
            VentaRecetaRetenidaForm(),
            ConsultaJefeFarmaciaForm(),
        )

        for formulario in formularios:
            with self.subTest(formulario=formulario.__class__.__name__):
                self.assertTrue(
                    campos_privilegiados.isdisjoint(formulario.fields)
                )
        self.assertIsNotNone(FarmaUserChangeForm._meta.fields)
        self.assertIsNotNone(FarmaGroupChangeForm._meta.fields)
        self.assertNotEqual(FarmaUserChangeForm._meta.fields, "__all__")
        self.assertNotEqual(FarmaGroupChangeForm._meta.fields, "__all__")

    def test_staff_no_superusuario_no_puede_editar_usuarios_ni_permisos_de_grupo(self):
        staff = User.objects.create_user(
            username="staff_sin_admin_privilegiado",
            password="Staff-password-123",
            is_staff=True,
        )
        staff.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="auth",
                codename="change_user",
            ),
            Permission.objects.get(
                content_type__app_label="auth",
                codename="change_group",
            ),
        )
        grupo = Group.objects.create(name="Grupo protegido")
        self.client.force_login(staff)

        for url in (
            reverse("admin:auth_user_change", args=(self.admin.pk,)),
            reverse("admin:auth_group_change", args=(grupo.pk,)),
        ):
            with self.subTest(url=url):
                respuesta = self.client.get(url)
                self.assertEqual(respuesta.status_code, 403)

    def test_superusuario_jefe_qf_va_al_admin_al_iniciar_sesion(self):
        self.admin.username = "jefe_qf"
        self.admin.save(update_fields=("username",))

        respuesta = self.client.post(
            reverse("login"),
            {
                "username": "jefe_qf",
                "password": "Admin-password-123",
                "next": "/",
            },
        )

        self.assertRedirects(
            respuesta,
            reverse("admin:index"),
            fetch_redirect_response=False,
        )

    def test_usuario_sin_grupo_no_puede_iniciar_sesion(self):
        usuario = User.objects.create_user(
            username="usuario",
            password="Usuario-password-123",
        )

        respuesta = self.client.post(
            reverse("login"),
            {
                "username": "usuario",
                "password": "Usuario-password-123",
                "is_staff": "true",
                "is_superuser": "true",
                "groups": str(
                    Group.objects.get_or_create(
                        name=GRUPO_JEFE_FARMACIA
                    )[0].pk
                ),
            },
        )

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Usuario o contraseña incorrectos.")
        self.assertFalse("_auth_user_id" in self.client.session)
        usuario.refresh_from_db()
        self.assertFalse(usuario.is_staff)
        self.assertFalse(usuario.is_superuser)
        self.assertFalse(usuario.groups.exists())

    def test_usuario_staff_sin_rol_farmaceutico_no_puede_iniciar_sesion(self):
        User.objects.create_user(
            username="staff_sin_rol",
            password="Staff-password-123",
            is_staff=True,
        )

        respuesta = self.client.post(
            reverse("login"),
            {
                "username": "staff_sin_rol",
                "password": "Staff-password-123",
            },
        )

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Usuario o contraseña incorrectos.")

    def test_quimico_va_a_su_inicio_de_farmacia_al_iniciar_sesion(self):
        quimico = User.objects.create_user(
            username="quimico",
            password="Quimico-password-123",
            is_active=True,
        )
        quimico.groups.add(Group.objects.get_or_create(name=GRUPO_FARMACEUTICO)[0])

        respuesta = self.client.post(
            reverse("login"),
            {
                "username": "quimico",
                "password": "Quimico-password-123",
            },
        )

        self.assertRedirects(
            respuesta,
            reverse("farmacia:inicio"),
            fetch_redirect_response=False,
        )

    def test_quimico_ve_catalogo_con_precio_y_stock_en_su_inicio(self):
        quimico = User.objects.create_user(
            username="quimico_catalogo",
            password="Quimico-password-123",
            is_active=True,
        )
        quimico.groups.add(Group.objects.get_or_create(name=GRUPO_FARMACEUTICO)[0])
        medicamento = Medicamento.objects.create(
            codigo="MED-QF-001",
            nombre="Producto de farmacia",
            precio=Decimal("2490.00"),
        )
        IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-QF-001",
            cantidad=12,
            fecha_vencimiento=timezone.localdate() + timedelta(days=90),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        self.client.force_login(quimico)

        respuesta = self.client.get(reverse("farmacia:inicio"))

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Producto de farmacia")
        self.assertContains(respuesta, "MED-QF-001")
        self.assertContains(respuesta, "$2.490")
        self.assertContains(respuesta, "Sin receta")
        self.assertContains(respuesta, "Existencias: 12")
        self.assertContains(respuesta, "Ir a Caja")
        self.assertContains(respuesta, "pie-sitio")

    def test_jefe_qf_puede_crear_medicamento_con_receta_desde_admin(self):
        self.client.force_login(self.admin)

        formulario = self.client.get(reverse("admin:farmacia_medicamento_add"))
        self.assertEqual(formulario.status_code, 200)
        self.assertContains(formulario, "Requiere receta")

        respuesta = self.client.post(
            reverse("admin:farmacia_medicamento_add"),
            {
                "nombre": "Medicamento con receta",
                "codigo": "MED-RECETA-001",
                "descripcion": "Requiere receta médica.",
                "precio": "3500.00",
                "requiere_receta": "on",
                "stock_minimo": "5",
                "activo": "on",
                "_save": "Guardar",
            },
        )

        self.assertEqual(respuesta.status_code, 302)
        medicamento = Medicamento.objects.get(codigo="MED-RECETA-001")
        self.assertTrue(medicamento.requiere_receta)

    def test_medicamento_con_receta_se_indica_en_catalogo_y_caja(self):
        quimico = User.objects.create_user(
            username="quimico_recetas",
            password="Quimico-password-123",
            is_active=True,
        )
        quimico.groups.add(Group.objects.get_or_create(name=GRUPO_FARMACEUTICO)[0])
        medicamento = Medicamento.objects.create(
            codigo="MED-RECETA-002",
            nombre="Producto sujeto a receta",
            requiere_receta=True,
        )
        IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-RECETA-002",
            cantidad=3,
            fecha_vencimiento=timezone.localdate() + timedelta(days=90),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        self.client.force_login(quimico)

        inicio = self.client.get(reverse("farmacia:inicio"))
        caja = self.client.get(reverse("farmacia:caja"))

        self.assertContains(inicio, "Con receta")
        self.assertContains(caja, "Con receta")

    def test_caja_permita_agregar_producto_y_muestre_total(self):
        quimico = User.objects.create_user(
            username="quimico_caja",
            password="Quimico-password-123",
            is_active=True,
        )
        quimico.groups.add(Group.objects.get_or_create(name=GRUPO_FARMACEUTICO)[0])
        medicamento = Medicamento.objects.create(
            codigo="MED-CAJA-001",
            nombre="Producto para caja",
            precio=Decimal("1250.00"),
        )
        IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-CAJA-001",
            cantidad=5,
            fecha_vencimiento=timezone.localdate() + timedelta(days=90),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        self.client.force_login(quimico)

        respuesta = self.client.post(
            reverse("farmacia:caja"),
            {
                "accion": "agregar",
                "medicamento_id": medicamento.pk,
                "cantidad": "2",
            },
        )

        self.assertRedirects(
            respuesta,
            reverse("farmacia:caja"),
            fetch_redirect_response=False,
        )
        respuesta = self.client.get(reverse("farmacia:caja"))
        self.assertContains(respuesta, "Producto para caja")
        self.assertContains(respuesta, "$2.500")
        self.assertContains(respuesta, "Confirmar venta y descontar stock")
        self.assertContains(respuesta, "pie-sitio")
        self.assertEqual(
            self.client.session["farmacia_caja_carrito"],
            {str(medicamento.pk): 2},
        )

    def test_caja_registra_pago_y_descuenta_lotes_por_vencimiento(self):
        quimico = User.objects.create_user(
            username="quimico_venta",
            password="Venta-test-123",
            is_active=True,
        )
        quimico.groups.add(Group.objects.get_or_create(name=GRUPO_FARMACEUTICO)[0])
        medicamento = Medicamento.objects.create(
            codigo="MED-VENTA-001",
            nombre="Medicamento para vender",
            precio=Decimal("1250.00"),
        )
        lote_proximo = IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-VENTA-PROXIMO",
            cantidad=2,
            fecha_vencimiento=timezone.localdate() + timedelta(days=30),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        lote_posterior = IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-VENTA-POSTERIOR",
            cantidad=5,
            fecha_vencimiento=timezone.localdate() + timedelta(days=60),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        self.client.force_login(quimico)
        self.client.post(
            reverse("farmacia:caja"),
            {
                "accion": "agregar",
                "medicamento_id": medicamento.pk,
                "cantidad": "4",
            },
        )
        respuesta = self.client.get(reverse("farmacia:caja"))
        checkout_key = respuesta.context["checkout_key"]

        respuesta = self.client.post(
            reverse("farmacia:caja"),
            {
                "accion": "confirmar_venta",
                "checkout_key": checkout_key,
                "medio_pago": SimulacionPago.MedioPago.DEBITO,
                "precio": "0.01",
                "precio_unitario": "0.01",
                "subtotal": "0.01",
                "total": "0.01",
            },
        )

        self.assertRedirects(
            respuesta,
            reverse("farmacia:caja"),
            fetch_redirect_response=False,
        )
        venta = VentaCaja.objects.get()
        pago = SimulacionPago.objects.get(venta_caja=venta)
        self.assertEqual(venta.cajero, quimico)
        self.assertEqual(venta.total, Decimal("5000.00"))
        self.assertEqual(
            list(
                venta.detalles.order_by("pk").values_list(
                    "precio_unitario",
                    flat=True,
                )
            ),
            [Decimal("1250.00"), Decimal("1250.00")],
        )
        self.assertEqual(pago.estado, SimulacionPago.Estado.APROBADO)
        self.assertEqual(pago.medio_pago, SimulacionPago.MedioPago.DEBITO)
        pagos = self.client.get(reverse("farmacia:caja"))
        self.assertContains(pagos, "Pagos simulados recientes")
        self.assertContains(pagos, "Débito")
        self.assertEqual(
            list(
                venta.detalles.order_by("ingreso__fecha_vencimiento").values_list(
                    "ingreso_id",
                    "cantidad",
                )
            ),
            [(lote_proximo.pk, 2), (lote_posterior.pk, 2)],
        )
        lote_proximo.refresh_from_db()
        lote_posterior.refresh_from_db()
        self.assertEqual(lote_proximo.stock_disponible, 0)
        self.assertEqual(lote_posterior.stock_disponible, 3)

        self.client.post(
            reverse("farmacia:caja"),
            {
                "accion": "confirmar_venta",
                "checkout_key": checkout_key,
                "medio_pago": SimulacionPago.MedioPago.DEBITO,
            },
        )
        self.assertEqual(VentaCaja.objects.count(), 1)
        self.assertEqual(SimulacionPago.objects.filter(venta_caja=venta).count(), 1)
        lote_posterior.refresh_from_db()
        self.assertEqual(lote_posterior.stock_disponible, 3)

    def test_caja_rechaza_venta_si_el_carrito_supera_stock_actual(self):
        quimico = User.objects.create_user(
            username="quimico_stock_venta",
            password="Venta-test-123",
            is_active=True,
        )
        quimico.groups.add(Group.objects.get_or_create(name=GRUPO_FARMACEUTICO)[0])
        medicamento = Medicamento.objects.create(
            codigo="MED-VENTA-002",
            nombre="Medicamento con stock limitado",
            precio=Decimal("800.00"),
        )
        ingreso = IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-VENTA-LIMITADO",
            cantidad=1,
            fecha_vencimiento=timezone.localdate() + timedelta(days=60),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        self.client.force_login(quimico)
        respuesta = self.client.get(reverse("farmacia:caja"))
        checkout_key = respuesta.context["checkout_key"]
        session = self.client.session
        session["farmacia_caja_carrito"] = {str(medicamento.pk): 2}
        session.save()

        respuesta = self.client.post(
            reverse("farmacia:caja"),
            {
                "accion": "confirmar_venta",
                "checkout_key": checkout_key,
                "medio_pago": SimulacionPago.MedioPago.EFECTIVO,
            },
            follow=True,
        )

        self.assertContains(respuesta, "Remedio agotado. Stock disponible: 1.")
        self.assertFalse(VentaCaja.objects.exists())
        self.assertFalse(SimulacionPago.objects.exists())
        ingreso.refresh_from_db()
        self.assertEqual(ingreso.stock_disponible, 1)

    def test_caja_responde_400_json_si_falta_receta_aprobada(self):
        quimico = User.objects.create_user(
            username="quimico_receta_caja",
            password="Venta-test-123",
            is_active=True,
        )
        quimico.groups.add(Group.objects.get_or_create(name=GRUPO_FARMACEUTICO)[0])
        medicamento = Medicamento.objects.create(
            codigo="MED-VENTA-RECETA",
            nombre="Medicamento con receta",
            precio=Decimal("1200.00"),
            requiere_receta=True,
        )
        IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-VENTA-RECETA",
            cantidad=3,
            fecha_vencimiento=timezone.localdate() + timedelta(days=60),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        self.client.force_login(quimico)

        respuesta = self.client.post(
            reverse("farmacia:caja"),
            {
                "accion": "agregar",
                "medicamento_id": medicamento.pk,
                "cantidad": "1",
            },
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(respuesta.json()["error"], "prescription_required")
        self.assertEqual(
            respuesta.json()["message"],
            "Se requiere receta médica aprobada para este producto.",
        )
        self.assertEqual(self.client.session.get("farmacia_caja_carrito", {}), {})
        self.assertFalse(VentaCaja.objects.exists())

    def test_caja_rechaza_ids_cantidades_y_json_malformados_sin_500(self):
        quimico = self.crear_quimico("quimico_entrada_malformada")
        medicamento = Medicamento.objects.create(
            codigo="MED-ENTRADA-MALFORMADA",
            nombre="Medicamento para validar entrada",
            precio=Decimal("1000.00"),
        )
        self.client.force_login(quimico)

        for medicamento_id in (
            "no-es-un-id",
            "-1",
            "9" * 30,
            f"0{medicamento.pk}",
        ):
            with self.subTest(medicamento_id=medicamento_id):
                respuesta = self.client.post(
                    reverse("farmacia:caja"),
                    {
                        "accion": "agregar",
                        "medicamento_id": medicamento_id,
                        "cantidad": "1",
                    },
                    HTTP_ACCEPT="application/json",
                )
                self.assertEqual(respuesta.status_code, 400)
                self.assertEqual(
                    respuesta.json()["error"],
                    "invalid_medicine_id",
                )

        for cantidad in ("-1", "0", "1.5", "texto"):
            with self.subTest(cantidad=cantidad):
                respuesta = self.client.post(
                    reverse("farmacia:caja"),
                    {
                        "accion": "agregar",
                        "medicamento_id": str(medicamento.pk),
                        "cantidad": cantidad,
                    },
                    HTTP_ACCEPT="application/json",
                )
                self.assertEqual(respuesta.status_code, 400)
                self.assertEqual(respuesta.json()["error"], "invalid_quantity")

        respuesta = self.client.post(
            reverse("farmacia:caja"),
            data="{",
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(
            respuesta.json()["codigo"],
            "TIPO_DE_SOLICITUD_NO_ADMITIDO",
        )
        self.assertEqual(self.client.session.get("farmacia_caja_carrito", {}), {})
        self.assertFalse(VentaCaja.objects.exists())

    def test_caja_rechaza_parametros_repetidos_y_bloqueos_sqlite(self):
        quimico = self.crear_quimico("quimico_parametros_repetidos")
        self.client.force_login(quimico)

        respuesta = self.client.post(
            reverse("farmacia:caja"),
            "accion=vaciar&accion=confirmar_venta",
            content_type="application/x-www-form-urlencoded",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(respuesta.json()["codigo"], "PARAMETRO_REPETIDO")

        with patch(
            "farmacia.views._registrar_venta_simulada",
            side_effect=OperationalError("database is locked"),
        ):
            respuesta = self.client.post(
                reverse("farmacia:caja"),
                {
                    "accion": "confirmar_venta",
                    "medio_pago": "efectivo",
                    "checkout_key": "checkout-test",
                },
                HTTP_ACCEPT="application/json",
            )

        self.assertEqual(respuesta.status_code, 429)
        self.assertEqual(
            respuesta.json()["codigo"],
            "CONCURRENCIA_EXCESIVA",
        )
        self.assertEqual(respuesta["Retry-After"], "2")

    def test_endpoints_de_accion_y_fotos_rechazan_metodos_no_permitidos(self):
        self.client.force_login(self.admin)

        for nombre in (
            "farmacia:resolver_receta_retenida",
            "farmacia:resolver_excepcion_receta",
        ):
            with self.subTest(endpoint=nombre):
                respuesta = self.client.get(reverse(nombre, args=(999,)))
                self.assertEqual(respuesta.status_code, 405)

        for nombre in (
            "farmacia:foto_receta_retenida",
            "farmacia:foto_consulta_jefe",
        ):
            with self.subTest(endpoint=nombre):
                respuesta = self.client.post(reverse(nombre, args=(999,)))
                self.assertEqual(respuesta.status_code, 405)

    def test_post_de_caja_sin_token_csrf_es_rechazado(self):
        quimico = self.crear_quimico("quimico_sin_csrf")
        client_con_csrf = Client(enforce_csrf_checks=True)
        client_con_csrf.force_login(quimico)

        respuesta = client_con_csrf.post(
            reverse("farmacia:caja"),
            {"accion": "vaciar"},
        )

        self.assertEqual(respuesta.status_code, 403)

    def test_busqueda_caja_con_comillas_es_texto_ordinario(self):
        quimico = self.crear_quimico("quimico_busqueda_comillas")
        self.client.force_login(quimico)

        respuesta = self.client.get(
            reverse("farmacia:caja"),
            {"q": "'texto de búsqueda'; --"},
        )

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "No encontramos productos")

    def test_busqueda_caja_rechaza_texto_excesivamente_largo(self):
        quimico = self.crear_quimico("quimico_busqueda_larga")
        self.client.force_login(quimico)

        respuesta = self.client.get(
            reverse("farmacia:caja"),
            {"q": "x" * 201},
        )

        self.assertEqual(respuesta.status_code, 400)
        self.assertContains(
            respuesta,
            "La búsqueda no puede superar los 200 caracteres.",
            status_code=400,
        )

    def test_caja_rechaza_folio_de_otro_medicamento(self):
        quimico = self.crear_quimico("quimico_folio_otro_medicamento")
        medicamento_recetado = Medicamento.objects.create(
            codigo="MED-FOLIO-ORIGEN",
            nombre="Medicamento origen del folio",
            precio=Decimal("1000.00"),
            requiere_receta=True,
        )
        medicamento_solicitado = Medicamento.objects.create(
            codigo="MED-FOLIO-DESTINO",
            nombre="Medicamento solicitado",
            precio=Decimal("1200.00"),
            requiere_receta=True,
        )
        IngresoInventario.objects.create(
            medicamento=medicamento_solicitado,
            lote="LOTE-FOLIO-DESTINO",
            cantidad=3,
            fecha_vencimiento=timezone.localdate() + timedelta(days=60),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        receta = VentaRecetaRetenida.objects.create(
            cliente_rut="11111111-1",
            medicamento=medicamento_recetado,
            cantidad=2,
            foto_receta=self.crear_imagen_receta(),
            medico_prescriptor="Dra. Ejemplo",
            auditor_qf=quimico,
        )
        jefe = self.crear_jefe("jefe_folio_otro_medicamento")
        receta.aprobar(jefe)
        self.addCleanup(lambda: receta.foto_receta.delete(save=False))
        self.client.force_login(quimico)

        respuesta = self.client.post(
            reverse("farmacia:caja"),
            {
                "accion": "agregar",
                "medicamento_id": medicamento_solicitado.pk,
                "cantidad": "1",
                "receta_id": receta.pk,
            },
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(respuesta.json()["error"], "prescription_required")
        self.assertEqual(self.client.session.get("farmacia_caja_carrito", {}), {})

    def test_caja_rechaza_cantidad_superior_a_la_autorizada_en_receta(self):
        quimico = self.crear_quimico("quimico_cantidad_receta")
        medicamento = Medicamento.objects.create(
            codigo="MED-CANTIDAD-RECETA",
            nombre="Medicamento cantidad autorizada",
            precio=Decimal("1000.00"),
            requiere_receta=True,
        )
        IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-CANTIDAD-RECETA",
            cantidad=4,
            fecha_vencimiento=timezone.localdate() + timedelta(days=60),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        receta = VentaRecetaRetenida.objects.create(
            cliente_rut="11111111-1",
            medicamento=medicamento,
            cantidad=1,
            foto_receta=self.crear_imagen_receta(),
            medico_prescriptor="Dra. Ejemplo",
            auditor_qf=quimico,
        )
        jefe = self.crear_jefe("jefe_cantidad_receta")
        receta.aprobar(jefe)
        self.addCleanup(lambda: receta.foto_receta.delete(save=False))
        self.client.force_login(quimico)

        respuesta = self.client.post(
            reverse("farmacia:caja"),
            {
                "accion": "agregar",
                "medicamento_id": medicamento.pk,
                "cantidad": "2",
                "receta_id": receta.pk,
            },
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(
            respuesta.json()["error"],
            "prescription_quantity_exceeded",
        )
        self.assertEqual(self.client.session.get("farmacia_caja_carrito", {}), {})

    def test_caja_rechaza_receta_si_el_revisor_perdio_el_rol(self):
        quimico = self.crear_quimico("quimico_revisor_sin_rol")
        grupo_jefes = Group.objects.get_or_create(name=GRUPO_JEFE_FARMACIA)[0]
        jefe = User.objects.create_user(
            username="jefe_revisor_sin_rol",
            is_active=True,
        )
        jefe.groups.add(grupo_jefes)
        medicamento = Medicamento.objects.create(
            codigo="MED-REVISOR-SIN-ROL",
            nombre="Medicamento con revisor desautorizado",
            precio=Decimal("1000.00"),
            requiere_receta=True,
        )
        IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-REVISOR-SIN-ROL",
            cantidad=2,
            fecha_vencimiento=timezone.localdate() + timedelta(days=60),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        receta = VentaRecetaRetenida.objects.create(
            cliente_rut="11111111-1",
            medicamento=medicamento,
            cantidad=1,
            foto_receta=self.crear_imagen_receta(),
            medico_prescriptor="Dr. Ejemplo",
            auditor_qf=quimico,
        )
        receta.aprobar(jefe)
        self.addCleanup(lambda: receta.foto_receta.delete(save=False))
        jefe.groups.remove(grupo_jefes)
        self.client.force_login(quimico)

        respuesta = self.client.post(
            reverse("farmacia:caja"),
            {
                "accion": "agregar",
                "medicamento_id": medicamento.pk,
                "cantidad": "1",
                "receta_id": receta.pk,
            },
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(respuesta.json()["error"], "prescription_required")
        self.assertEqual(self.client.session.get("farmacia_caja_carrito", {}), {})

    def test_caja_vende_controlado_con_receta_aprobada_y_la_despacha(self):
        quimico = self.crear_quimico("quimico_receta_aprobada")
        medicamento = Medicamento.objects.create(
            codigo="MED-VENTA-RECETA-APROBADA",
            nombre="Medicamento controlado con stock",
            precio=Decimal("1500.00"),
            requiere_receta=True,
        )
        ingreso = IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-RECETA-APROBADA",
            cantidad=5,
            fecha_vencimiento=timezone.localdate() + timedelta(days=60),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        venta_receta = VentaRecetaRetenida.objects.create(
            cliente_rut="11111111-1",
            medicamento=medicamento,
            cantidad=2,
            foto_receta=self.crear_imagen_receta(),
            medico_prescriptor="Dra. Ejemplo",
            auditor_qf=quimico,
        )
        jefe = self.crear_jefe("jefe_receta_checkout")
        venta_receta.aprobar(jefe)
        self.addCleanup(lambda: venta_receta.foto_receta.delete(save=False))
        self.client.force_login(quimico)

        respuesta = self.client.post(
            reverse("farmacia:caja"),
            {
                "accion": "agregar",
                "medicamento_id": medicamento.pk,
                "cantidad": "2",
                "receta_id": venta_receta.pk,
            },
        )
        self.assertEqual(respuesta.status_code, 302)
        pagina_caja = self.client.get(reverse("farmacia:caja"))
        checkout_key = pagina_caja.context["checkout_key"]

        respuesta = self.client.post(
            reverse("farmacia:caja"),
            {
                "accion": "confirmar_venta",
                "checkout_key": checkout_key,
                "medio_pago": SimulacionPago.MedioPago.EFECTIVO,
            },
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(respuesta.status_code, 302)
        venta_caja = VentaCaja.objects.get()
        ingreso.refresh_from_db()
        venta_receta.refresh_from_db()
        self.assertEqual(ingreso.stock_disponible, 3)
        self.assertEqual(venta_receta.estado, VentaRecetaRetenida.Estado.DISPENSADA)
        self.assertEqual(venta_receta.venta_caja, venta_caja)
        self.assertEqual(venta_caja.detalles.get().cantidad, 2)
        self.assertEqual(venta_caja.detalles.get().receta, venta_receta)

        respuesta = self.client.post(
            reverse("farmacia:caja"),
            {
                "accion": "agregar",
                "medicamento_id": medicamento.pk,
                "cantidad": "1",
                "receta_id": venta_receta.pk,
            },
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(respuesta.json()["error"], "prescription_required")
        self.assertEqual(self.client.session.get("farmacia_caja_carrito", {}), {})

        session = self.client.session
        session["farmacia_caja_carrito"] = {str(medicamento.pk): 1}
        session["farmacia_caja_recetas"] = {
            str(medicamento.pk): venta_receta.pk
        }
        session.save()
        checkout_key = self.client.get(reverse("farmacia:caja")).context[
            "checkout_key"
        ]
        doble_dispensacion = self.client.post(
            reverse("farmacia:caja"),
            {
                "accion": "confirmar_venta",
                "checkout_key": checkout_key,
                "medio_pago": SimulacionPago.MedioPago.EFECTIVO,
            },
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(doble_dispensacion.status_code, 400)
        self.assertEqual(
            doble_dispensacion.json()["codigo"],
            "RECETA_YA_UTILIZADA",
        )
        self.assertEqual(VentaCaja.objects.count(), 1)

        with patch(
            "farmacia.views._registrar_venta_simulada",
            side_effect=IntegrityError("unique constraint failed"),
        ):
            error_integridad = self.client.post(
                reverse("farmacia:caja"),
                {
                    "accion": "confirmar_venta",
                    "checkout_key": checkout_key,
                    "medio_pago": SimulacionPago.MedioPago.EFECTIVO,
                },
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(error_integridad.status_code, 400)
        self.assertEqual(
            error_integridad.json()["codigo"],
            "RECETA_YA_UTILIZADA",
        )

    def test_base_de_datos_impide_asociar_una_receta_a_dos_ventas(self):
        quimico = self.crear_quimico("quimico_receta_unica")
        medicamento = Medicamento.objects.create(
            codigo="MED-RECETA-UNICA-DETALLE",
            nombre="Medicamento para receta única",
            precio=Decimal("1000.00"),
            requiere_receta=True,
        )
        ingreso = IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-RECETA-UNICA-DETALLE",
            cantidad=5,
            fecha_vencimiento=timezone.localdate() + timedelta(days=60),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        receta = VentaRecetaRetenida.objects.create(
            cliente_rut="11111111-1",
            medicamento=medicamento,
            cantidad=1,
            foto_receta=self.crear_imagen_receta(),
            medico_prescriptor="Dra. Ejemplo",
            auditor_qf=quimico,
        )
        self.addCleanup(lambda: receta.foto_receta.delete(save=False))
        venta_original = VentaCaja.objects.create(
            cajero=quimico,
            total=Decimal("1000.00"),
        )
        VentaRecetaRetenida.objects.filter(pk=receta.pk).update(
            estado=VentaRecetaRetenida.Estado.DISPENSADA,
            venta_caja=venta_original,
        )
        DetalleVentaCaja.objects.create(
            venta=venta_original,
            ingreso=ingreso,
            receta=receta,
            cantidad=1,
            precio_unitario=Decimal("1000.00"),
        )
        otra_venta = VentaCaja.objects.create(
            cajero=quimico,
            total=Decimal("1000.00"),
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DetalleVentaCaja.objects.create(
                    venta=otra_venta,
                    ingreso=ingreso,
                    receta=receta,
                    cantidad=1,
                    precio_unitario=Decimal("1000.00"),
                )

    def test_caja_responde_400_json_si_stock_es_insuficiente(self):
        quimico = self.crear_quimico("quimico_receta_stock_insuficiente")
        jefe = User.objects.create_user(username="jefe_receta_stock", is_active=True)
        jefe.groups.add(Group.objects.get_or_create(name=GRUPO_JEFE_FARMACIA)[0])
        medicamento = Medicamento.objects.create(
            codigo="MED-VENTA-RECETA-STOCK",
            nombre="Medicamento controlado con poco stock",
            precio=Decimal("900.00"),
            requiere_receta=True,
        )
        ingreso = IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-RECETA-STOCK",
            cantidad=1,
            fecha_vencimiento=timezone.localdate() + timedelta(days=60),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        receta = VentaRecetaRetenida.objects.create(
            cliente_rut="11111111-1",
            medicamento=medicamento,
            cantidad=2,
            foto_receta=self.crear_imagen_receta(),
            medico_prescriptor="Dr. Ejemplo",
            auditor_qf=quimico,
            estado=VentaRecetaRetenida.Estado.APROBADA,
            jefe_farmacia=jefe,
            revisado_por=jefe,
            fecha_resolucion=timezone.now(),
        )
        self.addCleanup(lambda: receta.foto_receta.delete(save=False))
        self.client.force_login(quimico)

        respuesta = self.client.post(
            reverse("farmacia:caja"),
            {
                "accion": "agregar",
                "medicamento_id": medicamento.pk,
                "cantidad": "2",
                "receta_id": receta.pk,
            },
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(respuesta.json()["error"], "insufficient_stock")
        self.assertEqual(
            respuesta.json()["message"],
            "Remedio agotado. Stock disponible: 1.",
        )
        ingreso.refresh_from_db()
        receta.refresh_from_db()
        self.assertEqual(ingreso.stock_disponible, 1)
        self.assertEqual(receta.estado, VentaRecetaRetenida.Estado.APROBADA)
        self.assertFalse(VentaCaja.objects.exists())

    def test_checkout_falla_si_el_stock_cambia_y_no_dispensa_la_receta(self):
        quimico = self.crear_quimico("quimico_receta_checkout_stock")
        jefe = User.objects.create_user(username="jefe_checkout_stock", is_active=True)
        jefe.groups.add(Group.objects.get_or_create(name=GRUPO_JEFE_FARMACIA)[0])
        medicamento = Medicamento.objects.create(
            codigo="MED-RECETA-CHECKOUT-STOCK",
            nombre="Medicamento controlado de prueba",
            precio=Decimal("700.00"),
            requiere_receta=True,
        )
        ingreso = IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-RECETA-CHECKOUT-STOCK",
            cantidad=2,
            fecha_vencimiento=timezone.localdate() + timedelta(days=60),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        receta = VentaRecetaRetenida.objects.create(
            cliente_rut="11111111-1",
            medicamento=medicamento,
            cantidad=2,
            foto_receta=self.crear_imagen_receta(),
            medico_prescriptor="Dr. Ejemplo",
            auditor_qf=quimico,
            estado=VentaRecetaRetenida.Estado.APROBADA,
            jefe_farmacia=jefe,
            revisado_por=jefe,
            fecha_resolucion=timezone.now(),
        )
        self.addCleanup(lambda: receta.foto_receta.delete(save=False))
        self.client.force_login(quimico)
        self.client.post(
            reverse("farmacia:caja"),
            {
                "accion": "agregar",
                "medicamento_id": medicamento.pk,
                "cantidad": "2",
                "receta_id": receta.pk,
            },
        )
        pagina_caja = self.client.get(reverse("farmacia:caja"))
        checkout_key = pagina_caja.context["checkout_key"]
        IngresoInventario.objects.filter(pk=ingreso.pk).update(stock_disponible=1)

        respuesta = self.client.post(
            reverse("farmacia:caja"),
            {
                "accion": "confirmar_venta",
                "checkout_key": checkout_key,
                "medio_pago": SimulacionPago.MedioPago.EFECTIVO,
            },
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(
            respuesta.json()["message"],
            "Remedio agotado. Stock disponible: 1.",
        )
        ingreso.refresh_from_db()
        receta.refresh_from_db()
        self.assertEqual(ingreso.stock_disponible, 1)
        self.assertEqual(receta.estado, VentaRecetaRetenida.Estado.APROBADA)
        self.assertIsNone(receta.venta_caja_id)
        self.assertFalse(VentaCaja.objects.exists())

    def test_auditor_envia_receta_y_el_maximo_ordinario_es_seis(self):
        quimico = self.crear_quimico("auditor_receta")
        medicamento = Medicamento.objects.create(
            codigo="MED-RECETA-FORM-001",
            nombre="Medicamento de prueba",
            requiere_receta=True,
        )
        self.client.force_login(quimico)

        respuesta = self.client.post(
            reverse("farmacia:recetas_retenidas"),
            {
                "cliente_rut": "11111111-1",
                "medicamento": medicamento.pk,
                "cantidad": "7",
                "medico_prescriptor": "Dra. Ejemplo",
                "foto_receta": self.crear_imagen_receta(),
            },
        )

        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(respuesta.context["form"].errors["motivo_excepcion"])
        self.assertFalse(VentaRecetaRetenida.objects.exists())

        respuesta = self.client.post(
            reverse("farmacia:recetas_retenidas"),
            {
                "cliente_rut": "11111111-1",
                "medicamento": medicamento.pk,
                "cantidad": "6",
                "medico_prescriptor": "Dra. Ejemplo",
                "foto_receta": self.crear_imagen_receta(),
                "auditor_qf": self.admin.pk,
                "jefe_farmacia": self.admin.pk,
                "revisado_por": self.admin.pk,
                "estado": VentaRecetaRetenida.Estado.APROBADA,
                "is_staff": "true",
                "is_superuser": "true",
            },
        )

        self.assertRedirects(
            respuesta,
            reverse("farmacia:recetas_retenidas"),
            fetch_redirect_response=False,
        )
        venta = VentaRecetaRetenida.objects.get()
        self.addCleanup(lambda: venta.foto_receta.delete(save=False))
        self.assertEqual(venta.auditor_qf, quimico)
        self.assertEqual(venta.estado, VentaRecetaRetenida.Estado.PENDIENTE_REVISION)
        self.assertIsNone(venta.jefe_farmacia)
        self.assertIsNone(venta.revisado_por)
        quimico.refresh_from_db()
        self.assertFalse(quimico.is_staff)
        self.assertFalse(quimico.is_superuser)
        self.assertFalse(venta.autorizaciones_excepcionales.exists())

    def test_receta_retenida_limita_pendientes_y_aplica_cooldown(self):
        quimico = self.crear_quimico("auditor_limite_solicitudes")
        medicamento = Medicamento.objects.create(
            codigo="MED-RECETA-LIMITE-SOLICITUDES",
            nombre="Medicamento para límite de solicitudes",
            requiere_receta=True,
        )
        self.client.force_login(quimico)
        datos = {
            "cliente_rut": "11111111-1",
            "medicamento": medicamento.pk,
            "cantidad": "1",
            "medico_prescriptor": "Dra. Ejemplo",
            "foto_receta": self.crear_imagen_receta(),
        }

        respuesta = self.client.post(
            reverse("farmacia:recetas_retenidas"),
            datos,
            follow=True,
        )
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(
            VentaRecetaRetenida.objects.filter(auditor_qf=quimico).count(),
            1,
        )
        receta_inicial = VentaRecetaRetenida.objects.get(auditor_qf=quimico)
        self.addCleanup(lambda: receta_inicial.foto_receta.delete(save=False))

        respuesta = self.client.post(
            reverse("farmacia:recetas_retenidas"),
            {},
            follow=True,
        )
        self.assertContains(
            respuesta,
            "Debes esperar 2 minutos antes de enviar otra solicitud.",
        )
        self.assertEqual(
            VentaRecetaRetenida.objects.filter(auditor_qf=quimico).count(),
            1,
        )

        cache.delete(f"farmacia:cooldown:receta-retenida:{quimico.pk}")
        for _ in range(2):
            receta_pendiente = VentaRecetaRetenida.objects.create(
                cliente_rut="11111111-1",
                medicamento=medicamento,
                cantidad=1,
                foto_receta=self.crear_imagen_receta(),
                medico_prescriptor="Dra. Ejemplo",
                auditor_qf=quimico,
            )
            self.addCleanup(
                lambda receta=receta_pendiente: receta.foto_receta.delete(
                    save=False
                )
            )

        respuesta = self.client.post(
            reverse("farmacia:recetas_retenidas"),
            {},
            follow=True,
        )
        self.assertContains(
            respuesta,
            "Has alcanzado el límite de 3 solicitudes pendientes. "
            "Espera la respuesta del Jefe de Farmacia.",
        )
        self.assertEqual(
            VentaRecetaRetenida.objects.filter(auditor_qf=quimico).count(),
            3,
        )

    def test_formulario_receta_rechaza_cantidades_fuera_de_rango(self):
        quimico = self.crear_quimico("auditor_cantidad_fuera_rango")
        medicamento = Medicamento.objects.create(
            codigo="MED-RECETA-CANTIDAD-RANGO",
            nombre="Medicamento para validar cantidad",
            requiere_receta=True,
        )
        self.client.force_login(quimico)

        for cantidad in ("-5", "0", "1.5", "32768", "9" * 100):
            with self.subTest(cantidad=cantidad):
                respuesta = self.client.post(
                    reverse("farmacia:recetas_retenidas"),
                    {
                        "cliente_rut": "11111111-1",
                        "medicamento": medicamento.pk,
                        "cantidad": cantidad,
                        "medico_prescriptor": "Dra. Ejemplo",
                        "foto_receta": self.crear_imagen_receta(),
                    },
                )
                self.assertEqual(respuesta.status_code, 200)
                self.assertTrue(respuesta.context["form"].errors["cantidad"])
                self.assertFalse(VentaRecetaRetenida.objects.exists())

    def test_imagen_receta_rechaza_extension_no_permitida_y_archivo_mayor_a_5mb(self):
        quimico = self.crear_quimico("auditor_imagen_limites")
        medicamento = Medicamento.objects.create(
            codigo="MED-RECETA-IMAGEN",
            nombre="Medicamento para validar imagen",
            requiere_receta=True,
        )
        self.client.force_login(quimico)

        respuesta = self.client.post(
            reverse("farmacia:recetas_retenidas"),
            {
                "cliente_rut": "11111111-1",
                "medicamento": medicamento.pk,
                "cantidad": "1",
                "medico_prescriptor": "Dra. Ejemplo",
                "foto_receta": SimpleUploadedFile(
                    "receta.webp",
                    self.crear_imagen_receta().read(),
                    content_type="image/webp",
                ),
            },
        )
        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(respuesta.context["form"].errors["foto_receta"])
        self.assertFalse(VentaRecetaRetenida.objects.exists())

        archivo_grande = SimpleUploadedFile(
            "receta.png",
            b"x" * (MAX_RECETA_UPLOAD_BYTES + 1),
            content_type="image/png",
        )
        with self.assertRaisesMessage(
            ValidationError,
            "La imagen de la receta no puede superar los 5 MB.",
        ):
            validar_archivo_receta(archivo_grande)

    def test_receta_rechaza_contenido_que_no_es_imagen_con_extension_permitida(self):
        quimico = self.crear_quimico("auditor_imagen_corrupta")
        medicamento = Medicamento.objects.create(
            codigo="MED-RECETA-IMAGEN-CORRUPTA",
            nombre="Medicamento para validar contenido de imagen",
            requiere_receta=True,
        )
        self.client.force_login(quimico)

        respuesta = self.client.post(
            reverse("farmacia:recetas_retenidas"),
            {
                "cliente_rut": "11111111-1",
                "medicamento": medicamento.pk,
                "cantidad": "1",
                "medico_prescriptor": "Dra. Ejemplo",
                "foto_receta": SimpleUploadedFile(
                    "receta.png",
                    b"contenido que no es una imagen",
                    content_type="image/png",
                ),
            },
        )

        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(respuesta.context["form"].errors["foto_receta"])
        self.assertFalse(VentaRecetaRetenida.objects.exists())

    def test_receta_rechaza_imagen_de_mas_de_cinco_megapixeles(self):
        from farmacia.receta_forms import RecetaImageField

        self.assertEqual(Image.MAX_IMAGE_PIXELS, MAX_RECETA_IMAGE_PIXELS)
        archivo = BytesIO()
        Image.new("RGB", (2500, 2100), color="white").save(
            archivo,
            format="PNG",
        )

        with self.assertRaisesMessage(
            ValidationError,
            "La imagen excede la resolución máxima permitida de 5 megapíxeles.",
        ):
            RecetaImageField().clean(
                SimpleUploadedFile(
                    "pixel-bomb.png",
                    archivo.getvalue(),
                    content_type="image/png",
                )
            )

    def test_formulario_normaliza_unicode_y_rechaza_caracteres_de_control(self):
        from farmacia.receta_forms import VentaRecetaRetenidaForm

        quimico = self.crear_quimico("auditor_unicode_rut")
        medicamento = Medicamento.objects.create(
            codigo="MED-UNICODE-RUT",
            nombre="Medicamento de prueba",
            requiere_receta=True,
        )
        self.client.force_login(quimico)

        form = VentaRecetaRetenidaForm(
            data={
                "cliente_rut": "１１１１１１１１-1",
                "medicamento": medicamento.pk,
                "cantidad": "1",
                "medico_prescriptor": " Dra. Ejemplo ",
                "motivo_excepcion": " Justificación%00 válida ",
            },
            files={"foto_receta": self.crear_imagen_receta()},
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["cliente_rut"], "11111111-1")
        self.assertEqual(form.cleaned_data["medico_prescriptor"], "Dra. Ejemplo")
        self.assertEqual(
            form.cleaned_data["motivo_excepcion"],
            "Justificación válida",
        )
        formulario_con_controles = VentaRecetaRetenidaForm(
            data={
                "cliente_rut": "11111111-\x001",
                "medicamento": medicamento.pk,
                "cantidad": "1",
                "medico_prescriptor": "Dr.\u200b Ejemplo",
            },
            files={"foto_receta": self.crear_imagen_receta()},
        )
        self.assertFalse(formulario_con_controles.is_valid())
        self.assertIn("cliente_rut", formulario_con_controles.errors)
        self.assertIn("medico_prescriptor", formulario_con_controles.errors)

        formulario_solo_espacios = VentaRecetaRetenidaForm(
            data={
                "cliente_rut": "11111111-1",
                "medicamento": medicamento.pk,
                "cantidad": "1",
                "medico_prescriptor": " " * 10_000,
            },
            files={"foto_receta": self.crear_imagen_receta()},
        )
        self.assertFalse(formulario_solo_espacios.is_valid())
        self.assertIn(
            "medico_prescriptor",
            formulario_solo_espacios.errors,
        )
        formulario_truncado = VentaRecetaRetenidaForm(
            data={
                "cliente_rut": "11111111-1",
                "medicamento": medicamento.pk,
                "cantidad": "1",
                "medico_prescriptor": "D" * 201,
            },
            files={"foto_receta": self.crear_imagen_receta()},
        )
        self.assertFalse(formulario_truncado.is_valid())
        self.assertIn("medico_prescriptor", formulario_truncado.errors)

        medicamento_sin_normalizar = Medicamento(
            codigo="MED-NORMALIZAR-NOMBRE",
            nombre="  Ａspi\u200brina\x00  ",
        )
        medicamento_sin_normalizar.full_clean()
        self.assertEqual(medicamento_sin_normalizar.nombre, "Aspirina")

    def test_receta_rechaza_svg_y_parametros_repetidos(self):
        quimico = self.crear_quimico("auditor_svg_y_repetidos")
        medicamento = Medicamento.objects.create(
            codigo="MED-RECETA-SVG",
            nombre="Medicamento para bloquear SVG",
            requiere_receta=True,
        )
        self.client.force_login(quimico)

        respuesta = self.client.post(
            reverse("farmacia:recetas_retenidas"),
            {
                "cliente_rut": "11111111-1",
                "medicamento": medicamento.pk,
                "cantidad": "1",
                "medico_prescriptor": "Dra. Ejemplo",
                "foto_receta": SimpleUploadedFile(
                    "receta.svg",
                    b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
                    content_type="image/svg+xml",
                ),
            },
        )
        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(respuesta.context["form"].errors["foto_receta"])
        self.assertFalse(VentaRecetaRetenida.objects.exists())

        respuesta = self.client.post(
            reverse("farmacia:recetas_retenidas"),
            (
                "cliente_rut=11111111-1&"
                f"medicamento={medicamento.pk}&cantidad=1&cantidad=2"
            ),
            content_type="application/x-www-form-urlencoded",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(respuesta.json()["codigo"], "PARAMETRO_REPETIDO")
        self.assertFalse(VentaRecetaRetenida.objects.exists())

    def test_nombre_de_archivo_de_receta_no_puede_salir_de_su_directorio(self):
        from farmacia.storage import PrivatePrescriptionStorage

        quimico = self.crear_quimico("auditor_nombre_archivo")
        medicamento = Medicamento.objects.create(
            codigo="MED-NOMBRE-ARCHIVO",
            nombre="Medicamento para validar nombre de archivo",
            requiere_receta=True,
        )
        imagen = self.crear_imagen_receta()
        contenido = imagen.read()
        self.client.force_login(quimico)

        respuesta = self.client.post(
            reverse("farmacia:recetas_retenidas"),
            {
                "cliente_rut": "11111111-1",
                "medicamento": medicamento.pk,
                "cantidad": "1",
                "medico_prescriptor": "Dra. Ejemplo",
                "foto_receta": SimpleUploadedFile(
                    "../../fuera-del-directorio.png",
                    contenido,
                    content_type="image/png",
                ),
            },
        )

        self.assertEqual(respuesta.status_code, 302)
        receta = VentaRecetaRetenida.objects.get()
        self.addCleanup(lambda: receta.foto_receta.delete(save=False))
        self.assertTrue(receta.foto_receta.name.startswith("recetas_retenidas/"))
        self.assertNotIn("..", receta.foto_receta.name)
        self.assertRegex(
            receta.foto_receta.name,
            r"^recetas_retenidas/\d{4}/\d{2}/[0-9a-f]{32}\.png$",
        )
        nombre_normalizado = PrivatePrescriptionStorage().get_valid_name(
            "receta%00\x00Ａ.png"
        )
        self.assertEqual(nombre_normalizado, "recetaA.png")
        nombre_almacenado = PrivatePrescriptionStorage().get_available_name(
            "recetas_retenidas/2026/11/nombre-original.png"
        )
        self.assertRegex(
            nombre_almacenado,
            r"^recetas_retenidas[\\/]2026[\\/]11[\\/][0-9a-f]{32}\.png$",
        )

    def test_jefe_debe_resolver_excepcion_antes_de_aprobar_mas_de_seis(self):
        quimico = self.crear_quimico("auditor_excepcion")
        medicamento = Medicamento.objects.create(
            codigo="MED-RECETA-FORM-002",
            nombre="Medicamento de prueba",
            requiere_receta=True,
        )
        self.client.force_login(quimico)
        respuesta = self.client.post(
            reverse("farmacia:recetas_retenidas"),
            {
                "cliente_rut": "11111111-1",
                "medicamento": medicamento.pk,
                "cantidad": "8",
                "medico_prescriptor": "Dra. Ejemplo",
                "motivo_excepcion": "La receta indica ocho unidades para el tratamiento.",
                "foto_receta": self.crear_imagen_receta(),
            },
        )
        self.assertEqual(respuesta.status_code, 302)
        venta = VentaRecetaRetenida.objects.get()
        self.addCleanup(lambda: venta.foto_receta.delete(save=False))
        autorizacion = AutorizacionExcepcional.objects.get(venta=venta)

        jefe = User.objects.create_user(username="jefe_recetas", is_active=True)
        jefe.groups.add(Group.objects.get_or_create(name=GRUPO_JEFE_FARMACIA)[0])
        self.client.force_login(jefe)
        aprobar_venta = self.client.post(
            reverse("farmacia:resolver_receta_retenida", args=(venta.pk,)),
            {"decision": "aprobar"},
            follow=True,
        )
        self.assertContains(aprobar_venta, "Se requiere aprobar primero la excepción")
        venta.refresh_from_db()
        self.assertEqual(venta.estado, VentaRecetaRetenida.Estado.PENDIENTE_REVISION)

        respuesta = self.client.post(
            reverse("farmacia:resolver_excepcion_receta", args=(autorizacion.pk,)),
            {"decision": "aprobar", "respuesta": ""},
        )
        self.assertEqual(respuesta.status_code, 302)
        autorizacion.refresh_from_db()
        self.assertEqual(autorizacion.estado, AutorizacionExcepcional.Estado.APROBADA)

        respuesta = self.client.post(
            reverse("farmacia:resolver_receta_retenida", args=(venta.pk,)),
            {"decision": "aprobar"},
        )
        self.assertEqual(respuesta.status_code, 302)
        venta.refresh_from_db()
        self.assertEqual(venta.estado, VentaRecetaRetenida.Estado.APROBADA)
        self.assertEqual(venta.jefe_farmacia, jefe)
        self.assertIsNotNone(venta.fecha_resolucion)

    def test_rechazo_de_receta_requiere_motivo_y_solo_jefe_puede_resolver(self):
        quimico = self.crear_quimico("auditor_rechazo")
        medicamento = Medicamento.objects.create(
            codigo="MED-RECETA-RECHAZO",
            nombre="Medicamento de prueba",
            requiere_receta=True,
        )
        venta = VentaRecetaRetenida.objects.create(
            cliente_rut="11111111-1",
            medicamento=medicamento,
            cantidad=1,
            foto_receta=self.crear_imagen_receta(),
            medico_prescriptor="Dr. Ejemplo",
            auditor_qf=quimico,
        )
        self.addCleanup(lambda: venta.foto_receta.delete(save=False))
        self.client.force_login(quimico)
        with self.assertRaises(PermissionDenied):
            venta.aprobar(quimico)

        listado = self.client.get(reverse("farmacia:recetas_retenidas"))
        self.assertEqual(listado.status_code, 200)
        self.assertNotContains(listado, "Aprobar mi receta")

        for decision, datos in (
            ("aprobar", {"decision": "aprobar"}),
            (
                "rechazar",
                {"decision": "rechazar", "observacion": "Motivo"},
            ),
        ):
            with self.subTest(decision=decision):
                no_autorizado = self.client.post(
                    reverse(
                        "farmacia:resolver_receta_retenida",
                        args=(venta.pk,),
                    ),
                    datos,
                )
                self.assertEqual(no_autorizado.status_code, 403)
        venta.refresh_from_db()
        self.assertEqual(
            venta.estado,
            VentaRecetaRetenida.Estado.PENDIENTE_REVISION,
        )

        jefe = User.objects.create_user(username="jefe_rechazo", is_active=True)
        jefe.groups.add(Group.objects.get_or_create(name=GRUPO_JEFE_FARMACIA)[0])
        self.client.force_login(jefe)
        sin_motivo = self.client.post(
            reverse("farmacia:resolver_receta_retenida", args=(venta.pk,)),
            {"decision": "rechazar", "observacion": ""},
            follow=True,
        )
        self.assertContains(sin_motivo, "motivo obligatorio")
        venta.refresh_from_db()
        self.assertEqual(venta.estado, VentaRecetaRetenida.Estado.PENDIENTE_REVISION)

        respuesta = self.client.post(
            reverse("farmacia:resolver_receta_retenida", args=(venta.pk,)),
            {"decision": "rechazar", "observacion": "La imagen no es legible."},
        )
        self.assertEqual(respuesta.status_code, 302)
        venta.refresh_from_db()
        self.assertEqual(venta.estado, VentaRecetaRetenida.Estado.RECHAZADA)
        self.assertEqual(venta.observacion_resolucion, "La imagen no es legible.")
        self.assertEqual(venta.jefe_farmacia, jefe)

    def test_observacion_de_receta_se_escapa_al_renderizarse(self):
        quimico = self.crear_quimico("auditor_texto_escapado")
        medicamento = Medicamento.objects.create(
            codigo="MED-TEXTO-ESCAPADO",
            nombre="Medicamento para validar texto",
            requiere_receta=True,
        )
        texto = "<b>comentario de prueba</b>"
        receta = VentaRecetaRetenida.objects.create(
            cliente_rut="11111111-1",
            medicamento=medicamento,
            cantidad=1,
            foto_receta=self.crear_imagen_receta(),
            medico_prescriptor="Dr. Ejemplo",
            auditor_qf=quimico,
            estado=VentaRecetaRetenida.Estado.RECHAZADA,
            jefe_farmacia=self.admin,
            revisado_por=self.admin,
            observacion_resolucion=texto,
            fecha_resolucion=timezone.now(),
        )
        self.addCleanup(lambda: receta.foto_receta.delete(save=False))
        self.client.force_login(self.admin)

        respuesta = self.client.get(reverse("farmacia:recetas_retenidas"))

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "&lt;b&gt;comentario de prueba&lt;/b&gt;")
        self.assertNotContains(respuesta, texto, html=False)

    def test_auditor_solo_ve_sus_recetas_y_su_imagen(self):
        titular = self.crear_quimico("auditor_titular")
        otro_auditor = self.crear_quimico("auditor_otro")
        medicamento = Medicamento.objects.create(
            codigo="MED-RECETA-PRIVADA",
            nombre="Receta privada de prueba",
            requiere_receta=True,
        )
        venta = VentaRecetaRetenida.objects.create(
            cliente_rut="11111111-1",
            medicamento=medicamento,
            cantidad=1,
            foto_receta=self.crear_imagen_receta(),
            medico_prescriptor="Dr. Ejemplo",
            auditor_qf=titular,
        )
        self.addCleanup(lambda: venta.foto_receta.delete(save=False))
        self.client.force_login(otro_auditor)

        historial = self.client.get(reverse("farmacia:recetas_retenidas"))
        foto = self.client.get(
            reverse("farmacia:foto_receta_retenida", args=(venta.pk,))
        )

        self.assertEqual(historial.status_code, 200)
        self.assertNotContains(historial, "11111111-1")
        self.assertEqual(foto.status_code, 403)

    def test_operador_pos_no_puede_ver_imagen_ni_aprobar_receta_ajena(self):
        titular = self.crear_quimico("auditor_titular_operador_pos")
        medicamento = Medicamento.objects.create(
            codigo="MED-RECETA-OPERADOR-POS",
            nombre="Receta privada para operador POS",
            requiere_receta=True,
        )
        receta = VentaRecetaRetenida.objects.create(
            cliente_rut="11111111-1",
            medicamento=medicamento,
            cantidad=1,
            foto_receta=self.crear_imagen_receta(),
            medico_prescriptor="Dr. Ejemplo",
            auditor_qf=titular,
        )
        self.addCleanup(lambda: receta.foto_receta.delete(save=False))
        operador = User.objects.create_user(
            username="operador_pos_sin_rol",
            password="Operador-password-123",
            is_active=True,
            is_staff=True,
        )
        operador.groups.add(Group.objects.get_or_create(name="Operador_POS")[0])
        self.client.force_login(operador)

        foto = self.client.get(
            reverse("farmacia:foto_receta_retenida", args=(receta.pk,))
        )
        aprobacion = self.client.post(
            reverse("farmacia:resolver_receta_retenida", args=(receta.pk,)),
            {"decision": "aprobar"},
        )

        self.assertEqual(foto.status_code, 403)
        self.assertEqual(aprobacion.status_code, 403)
        receta.refresh_from_db()
        self.assertEqual(
            receta.estado,
            VentaRecetaRetenida.Estado.PENDIENTE_REVISION,
        )

    def crear_imagen_receta(self):
        archivo = BytesIO()
        Image.new("RGB", (4, 4), color="white").save(archivo, format="PNG")
        return SimpleUploadedFile(
            "receta.png",
            archivo.getvalue(),
            content_type="image/png",
        )

    def crear_quimico(self, username="quimico"):
        quimico = User.objects.create_user(
            username=username,
            password="Quimico-password-123",
            is_active=True,
        )
        quimico.groups.add(Group.objects.get_or_create(name=GRUPO_FARMACEUTICO)[0])
        return quimico

    def crear_jefe(self, username="jefe_farmacia"):
        jefe = User.objects.create_user(username=username, is_active=True)
        jefe.groups.add(Group.objects.get_or_create(name=GRUPO_JEFE_FARMACIA)[0])
        return jefe

    def test_quimico_envia_consulta_con_foto_y_solo_remitente_o_jefe_ve_la_foto(self):
        quimico = self.crear_quimico()
        self.client.force_login(quimico)

        respuesta = self.client.post(
            reverse("farmacia:consultas_jefe"),
            {
                "asunto": "Duda sobre indicación",
                "pregunta": "¿Podría confirmar la dosis indicada?",
                "foto_receta": self.crear_imagen_receta(),
                "solicitante": self.admin.pk,
                "jefe_farmacia": self.admin.pk,
                "estado": ConsultaJefeFarmacia.Estado.RESPONDIDA,
                "respuesta": "Respuesta inyectada",
            },
        )

        self.assertRedirects(
            respuesta,
            reverse("farmacia:consultas_jefe"),
            fetch_redirect_response=False,
        )
        consulta = ConsultaJefeFarmacia.objects.get()
        self.addCleanup(lambda: consulta.foto_receta.delete(save=False))
        self.assertEqual(consulta.solicitante, quimico)
        self.assertEqual(consulta.estado, ConsultaJefeFarmacia.Estado.PENDIENTE)
        self.assertRegex(
            consulta.foto_receta.name,
            r"^consultas_jefe/\d{4}/\d{2}/[0-9a-f]{32}\.png$",
        )
        self.assertIsNone(consulta.jefe_farmacia)
        self.assertEqual(consulta.respuesta, "")

        foto = self.client.get(
            reverse("farmacia:foto_consulta_jefe", args=(consulta.pk,))
        )
        self.assertEqual(foto.status_code, 200)
        self.assertEqual(foto["Content-Type"], "image/png")
        self.assertEqual(foto["X-Content-Type-Options"], "nosniff")
        self.assertEqual(foto["Cache-Control"], "private, no-store")
        foto.close()

        otro_quimico = self.crear_quimico("otro_quimico")
        self.client.force_login(otro_quimico)
        foto_no_autorizada = self.client.get(
            reverse("farmacia:foto_consulta_jefe", args=(consulta.pk,))
        )
        self.assertEqual(foto_no_autorizada.status_code, 403)

        jefe = User.objects.create_user(
            username="jefe_farmacia",
            is_active=True,
        )
        jefe.groups.add(Group.objects.get_or_create(name=GRUPO_JEFE_FARMACIA)[0])
        self.client.force_login(jefe)
        foto_jefe = self.client.get(
            reverse("farmacia:foto_consulta_jefe", args=(consulta.pk,))
        )
        self.assertEqual(foto_jefe.status_code, 200)
        foto_jefe.close()

    def test_consulta_jefe_limita_pendientes_y_aplica_cooldown(self):
        quimico = self.crear_quimico("quimico_limite_solicitudes")
        self.client.force_login(quimico)
        datos = {
            "asunto": "Consulta de prueba",
            "pregunta": "Necesito confirmar una indicación.",
            "foto_receta": self.crear_imagen_receta(),
        }

        respuesta = self.client.post(
            reverse("farmacia:consultas_jefe"),
            datos,
            follow=True,
        )
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(
            ConsultaJefeFarmacia.objects.filter(solicitante=quimico).count(),
            1,
        )
        consulta_inicial = ConsultaJefeFarmacia.objects.get(solicitante=quimico)
        self.addCleanup(lambda: consulta_inicial.foto_receta.delete(save=False))

        respuesta = self.client.post(
            reverse("farmacia:consultas_jefe"),
            {},
            follow=True,
        )
        self.assertContains(
            respuesta,
            "Debes esperar 2 minutos antes de enviar otra solicitud.",
        )
        self.assertEqual(
            ConsultaJefeFarmacia.objects.filter(solicitante=quimico).count(),
            1,
        )

        cache.delete(f"farmacia:cooldown:consulta-jefe:{quimico.pk}")
        for indice in range(2):
            consulta_pendiente = ConsultaJefeFarmacia.objects.create(
                solicitante=quimico,
                asunto=f"Consulta pendiente {indice}",
                pregunta="Consulta de prueba.",
                foto_receta=self.crear_imagen_receta(),
            )
            self.addCleanup(
                lambda consulta=consulta_pendiente: consulta.foto_receta.delete(
                    save=False
                )
            )

        respuesta = self.client.post(
            reverse("farmacia:consultas_jefe"),
            {},
            follow=True,
        )
        self.assertContains(
            respuesta,
            "Has alcanzado el límite de 3 solicitudes pendientes. "
            "Espera la respuesta del Jefe de Farmacia.",
        )
        self.assertEqual(
            ConsultaJefeFarmacia.objects.filter(solicitante=quimico).count(),
            3,
        )

    def test_imagen_de_consulta_se_recodifica_sin_metadatos_exif(self):
        quimico = self.crear_quimico("quimico_imagen_exif")
        self.client.force_login(quimico)
        archivo = BytesIO()
        exif = Image.Exif()
        exif[270] = "metadato privado de prueba"
        Image.new("RGB", (8, 8), color="white").save(
            archivo,
            format="JPEG",
            exif=exif.tobytes(),
        )

        respuesta = self.client.post(
            reverse("farmacia:consultas_jefe"),
            {
                "asunto": "Consulta para verificar metadatos",
                "pregunta": "Comprobar limpieza de EXIF.",
                "foto_receta": SimpleUploadedFile(
                    "receta.jpg",
                    archivo.getvalue(),
                    content_type="image/jpeg",
                ),
            },
        )

        self.assertEqual(respuesta.status_code, 302)
        consulta = ConsultaJefeFarmacia.objects.get()
        self.addCleanup(lambda: consulta.foto_receta.delete(save=False))
        with consulta.foto_receta.open("rb") as imagen_guardada:
            with Image.open(imagen_guardada) as imagen:
                self.assertEqual(imagen.format, "JPEG")
                self.assertEqual(dict(imagen.getexif()), {})
                self.assertNotIn("exif", imagen.info)

    def test_jefe_responde_consulta_solo_una_vez(self):
        quimico = self.crear_quimico()
        consulta = ConsultaJefeFarmacia.objects.create(
            solicitante=quimico,
            asunto="Consulta de prueba",
            pregunta="¿Cuál es la indicación?",
            foto_receta=self.crear_imagen_receta(),
        )
        self.addCleanup(lambda: consulta.foto_receta.delete(save=False))
        jefe = User.objects.create_user(username="jefe_farmacia", is_active=True)
        jefe.groups.add(Group.objects.get_or_create(name="Jefe_QF")[0])
        self.client.force_login(jefe)

        respuesta = self.client.post(
            reverse("farmacia:consultas_jefe"),
            {"consulta_id": consulta.pk, "respuesta": "  Dosis confirmada.  "},
        )

        self.assertRedirects(
            respuesta,
            reverse("farmacia:consultas_jefe"),
            fetch_redirect_response=False,
        )
        consulta.refresh_from_db()
        self.assertEqual(consulta.estado, ConsultaJefeFarmacia.Estado.RESPONDIDA)
        self.assertEqual(consulta.respuesta, "Dosis confirmada.")
        self.assertEqual(consulta.jefe_farmacia, jefe)
        self.assertIsNotNone(consulta.fecha_respuesta)

        respuesta = self.client.post(
            reverse("farmacia:consultas_jefe"),
            {"consulta_id": consulta.pk, "respuesta": "Otra respuesta."},
            follow=True,
        )
        self.assertContains(respuesta, "ya fue respondida")
        consulta.refresh_from_db()
        self.assertEqual(consulta.respuesta, "Dosis confirmada.")

    def test_eliminar_consulta_requiere_password_y_conserva_registro_si_falla(self):
        quimico = self.crear_quimico("quimico_consulta_password_incorrecto")
        consulta = ConsultaJefeFarmacia.objects.create(
            solicitante=quimico,
            asunto="Consulta protegida",
            pregunta="Consulta que no debe eliminarse con clave incorrecta.",
            foto_receta=self.crear_imagen_receta(),
        )
        self.addCleanup(lambda: consulta.foto_receta.delete(save=False))
        jefe = self.crear_jefe("jefe_consulta_password_incorrecto")
        jefe.set_password("Jefe-password-123")
        jefe.save(update_fields=("password",))
        self.client.force_login(jefe)

        respuesta = self.client.post(
            reverse("farmacia:eliminar_consulta_jefe", args=(consulta.pk,)),
            {"password_confirmacion": "Clave-incorrecta"},
            follow=True,
        )

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "La contraseña no es correcta.")
        self.assertTrue(ConsultaJefeFarmacia.objects.filter(pk=consulta.pk).exists())

    def test_eliminar_consulta_valida_rol_password_y_limpia_foto(self):
        quimico = self.crear_quimico("quimico_consulta_eliminar")
        consulta = ConsultaJefeFarmacia.objects.create(
            solicitante=quimico,
            asunto="Consulta eliminable",
            pregunta="Consulta creada para verificar la eliminación.",
            foto_receta=self.crear_imagen_receta(),
        )
        nombre_imagen = consulta.foto_receta.name
        almacen_imagen = consulta.foto_receta.storage
        self.addCleanup(lambda: almacen_imagen.delete(nombre_imagen))
        self.assertTrue(almacen_imagen.exists(nombre_imagen))
        jefe = self.crear_jefe("jefe_consulta_eliminar")
        jefe.set_password("Jefe-password-123")
        jefe.save(update_fields=("password",))
        self.client.force_login(jefe)

        with self.captureOnCommitCallbacks(execute=True):
            respuesta = self.client.post(
                reverse("farmacia:eliminar_consulta_jefe", args=(consulta.pk,)),
                {"password_confirmacion": "Jefe-password-123"},
            )

        self.assertEqual(respuesta.status_code, 302)
        self.assertFalse(ConsultaJefeFarmacia.objects.filter(pk=consulta.pk).exists())
        self.assertFalse(almacen_imagen.exists(nombre_imagen))

    def test_eliminar_consulta_rechaza_metodos_y_roles_no_autorizados(self):
        quimico = self.crear_quimico("quimico_sin_permiso_eliminar")
        cliente = User.objects.create_user(
            username="cliente_sin_permiso_eliminar",
            password="Cliente-password-123",
            is_active=True,
        )
        consulta = ConsultaJefeFarmacia.objects.create(
            solicitante=quimico,
            asunto="Consulta no autorizada",
            pregunta="Un químico no puede eliminar esta consulta.",
            foto_receta=self.crear_imagen_receta(),
        )
        self.addCleanup(lambda: consulta.foto_receta.delete(save=False))
        jefe = self.crear_jefe("jefe_metodo_eliminar")
        jefe.set_password("Jefe-password-123")
        jefe.save(update_fields=("password",))

        for usuario in (quimico, cliente):
            self.client.force_login(usuario)
            for metodo in ("get", "post"):
                with self.subTest(usuario=usuario.username, metodo=metodo):
                    respuesta = getattr(self.client, metodo)(
                        reverse(
                            "farmacia:eliminar_consulta_jefe",
                            args=(consulta.pk,),
                        ),
                        {"password_confirmacion": "Jefe-password-123"}
                        if metodo == "post"
                        else None,
                    )
                    self.assertEqual(respuesta.status_code, 403)

        self.client.force_login(jefe)
        respuesta_jefe_get = self.client.get(
            reverse("farmacia:eliminar_consulta_jefe", args=(consulta.pk,))
        )

        self.assertEqual(respuesta_jefe_get.status_code, 405)
        self.assertTrue(ConsultaJefeFarmacia.objects.filter(pk=consulta.pk).exists())

    def test_eliminar_consulta_limita_a_cinco_intentos_por_minuto(self):
        quimico = self.crear_quimico("quimico_consulta_rate_limit")
        consulta = ConsultaJefeFarmacia.objects.create(
            solicitante=quimico,
            asunto="Consulta con límite",
            pregunta="Consulta para probar límite de clave.",
            foto_receta=self.crear_imagen_receta(),
        )
        self.addCleanup(lambda: consulta.foto_receta.delete(save=False))
        jefe = self.crear_jefe("jefe_consulta_rate_limit")
        jefe.set_password("Jefe-password-123")
        jefe.save(update_fields=("password",))
        self.client.force_login(jefe)
        url = reverse("farmacia:eliminar_consulta_jefe", args=(consulta.pk,))

        for intento in range(5):
            with self.subTest(intento=intento + 1):
                respuesta = self.client.post(
                    url,
                    {"password_confirmacion": "Clave-incorrecta"},
                )
                self.assertEqual(respuesta.status_code, 302)

        respuesta = self.client.post(
            url,
            {"password_confirmacion": "Clave-incorrecta"},
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(respuesta.status_code, 429)
        self.assertEqual(respuesta["Retry-After"], "60")
        self.assertEqual(respuesta.json()["error"], "rate_limited")
        self.assertTrue(ConsultaJefeFarmacia.objects.filter(pk=consulta.pk).exists())

    def test_eliminar_consulta_maneja_protected_error_sin_traza(self):
        quimico = self.crear_quimico("quimico_consulta_protected_error")
        consulta = ConsultaJefeFarmacia.objects.create(
            solicitante=quimico,
            asunto="Consulta protegida",
            pregunta="La eliminación simula una FK protegida.",
            foto_receta=self.crear_imagen_receta(),
        )
        self.addCleanup(lambda: consulta.foto_receta.delete(save=False))
        jefe = self.crear_jefe("jefe_consulta_protected_error")
        jefe.set_password("Jefe-password-123")
        jefe.save(update_fields=("password",))
        self.client.force_login(jefe)

        with patch(
            "farmacia.views.ConsultaJefeFarmacia.delete",
            side_effect=ProtectedError("protected", []),
        ):
            respuesta = self.client.post(
                reverse("farmacia:eliminar_consulta_jefe", args=(consulta.pk,)),
                {"password_confirmacion": "Jefe-password-123"},
                follow=True,
            )

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(
            respuesta,
            "No se puede eliminar la consulta porque tiene registros asociados.",
        )
        self.assertNotContains(respuesta, "Traceback")
        self.assertTrue(ConsultaJefeFarmacia.objects.filter(pk=consulta.pk).exists())

    def test_plantilla_consulta_escapa_texto_y_incluye_csrf_en_eliminacion(self):
        quimico = self.crear_quimico("quimico_consulta_xss")
        consulta = ConsultaJefeFarmacia.objects.create(
            solicitante=quimico,
            asunto="<script>alert(1)</script>",
            pregunta="<img src=x onerror=alert(1)>",
            foto_receta=self.crear_imagen_receta(),
        )
        self.addCleanup(lambda: consulta.foto_receta.delete(save=False))
        jefe = self.crear_jefe("jefe_consulta_xss")
        self.client.force_login(jefe)

        respuesta = self.client.get(reverse("farmacia:consultas_jefe"))

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "name=\"csrfmiddlewaretoken\"")
        self.assertContains(respuesta, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertContains(respuesta, "&lt;img src=x onerror=alert(1)&gt;")
        self.assertNotContains(respuesta, "<script>alert(1)</script>")
        self.assertNotContains(respuesta, "<img src=x onerror=alert(1)>")

    def test_formulario_consulta_rechaza_archivo_que_no_es_imagen(self):
        quimico = self.crear_quimico()
        self.client.force_login(quimico)
        respuesta = self.client.post(
            reverse("farmacia:consultas_jefe"),
            {
                "asunto": "Duda sobre receta",
                "pregunta": "Consulta de prueba",
                "foto_receta": SimpleUploadedFile(
                    "receta.png",
                    b"esto no es una imagen",
                    content_type="image/png",
                ),
            },
        )

        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(respuesta.context["form"].errors["foto_receta"])
        self.assertFalse(ConsultaJefeFarmacia.objects.exists())

    def test_formulario_consulta_aplica_limites_de_extension_y_tamano(self):
        quimico = self.crear_quimico("quimico_consulta_limites")
        self.client.force_login(quimico)
        archivo_webp = self.crear_imagen_receta()
        contenido_png = archivo_webp.read()

        respuesta = self.client.post(
            reverse("farmacia:consultas_jefe"),
            {
                "asunto": "Consulta de prueba",
                "pregunta": "Verificar extensión.",
                "foto_receta": SimpleUploadedFile(
                    "receta.webp",
                    contenido_png,
                    content_type="image/webp",
                ),
            },
        )
        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(respuesta.context["form"].errors["foto_receta"])
        self.assertFalse(ConsultaJefeFarmacia.objects.exists())

        respuesta = self.client.post(
            reverse("farmacia:consultas_jefe"),
            {
                "asunto": "Consulta de prueba",
                "pregunta": "Verificar tamaño.",
                "foto_receta": SimpleUploadedFile(
                    "receta.png",
                    b"x" * (MAX_RECETA_UPLOAD_BYTES + 1),
                    content_type="image/png",
                ),
            },
        )
        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(respuesta.context["form"].errors["foto_receta"])
        self.assertFalse(ConsultaJefeFarmacia.objects.exists())

    def test_usuario_sin_grupo_no_puede_abrir_consultas(self):
        usuario = User.objects.create_user(
            username="usuario_sin_rol",
            password="Usuario-password-123",
            is_active=True,
        )
        self.client.force_login(usuario)

        respuesta = self.client.get(reverse("farmacia:consultas_jefe"))

        self.assertEqual(respuesta.status_code, 403)

    def test_caja_rechaza_cantidad_superior_al_stock_vendible(self):
        quimico = User.objects.create_user(
            username="quimico_stock",
            password="Quimico-password-123",
            is_active=True,
        )
        quimico.groups.add(Group.objects.get_or_create(name=GRUPO_FARMACEUTICO)[0])
        medicamento = Medicamento.objects.create(
            codigo="MED-CAJA-002",
            nombre="Producto con poco stock",
            precio=Decimal("500.00"),
        )
        IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-CAJA-002",
            cantidad=1,
            fecha_vencimiento=timezone.localdate() + timedelta(days=90),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        self.client.force_login(quimico)

        self.client.post(
            reverse("farmacia:caja"),
            {
                "accion": "agregar",
                "medicamento_id": medicamento.pk,
                "cantidad": "2",
            },
        )

        self.assertEqual(
            self.client.session.get("farmacia_caja_carrito", {}),
            {},
        )

    def test_personal_sin_privilegios_no_puede_ver_solicitudes(self):
        usuario_staff = User.objects.create_user(
            username="staff",
            email="staff@example.com",
            password="Staff-password-123",
            is_staff=True,
        )
        self.client.force_login(usuario_staff)

        respuesta = self.client.get(
            reverse("admin:farmacia_solicitudcuenta_changelist")
        )

        self.assertEqual(respuesta.status_code, 403)

    def test_panel_admin_muestra_resumen_de_farmacia(self):
        self.client.force_login(self.admin)

        respuesta = self.client.get(reverse("admin:index"))

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "farmacia/admin.css?v=5")
        self.assertContains(respuesta, "Resumen de inventario")
        self.assertContains(respuesta, "Abrir Caja")
        self.assertContains(respuesta, "Consultas al Jefe de Farmacia")
        self.assertContains(
            respuesta,
            reverse("farmacia:consultas_jefe"),
        )
        self.assertContains(respuesta, reverse("farmacia:caja"))
        self.assertContains(respuesta, "Registrar o revisar ingresos")
        self.assertContains(respuesta, "Solicitudes de qu")
        self.assertNotContains(respuesta, "pie-sitio")

    def test_jefe_no_ve_accesos_restringidos_a_solicitudes_de_cuenta(self):
        jefe = User.objects.create_user(
            username="jefe_farmacia",
            password="Jefe-password-123",
            is_staff=True,
        )
        jefe.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="farmacia",
                codename="view_medicamento",
            )
        )
        self.client.force_login(jefe)

        respuesta = self.client.get(reverse("admin:index"))

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Control de farmacia")
        self.assertNotContains(
            respuesta,
            reverse("admin:farmacia_solicitudcuenta_changelist"),
        )

    def test_listas_principales_del_admin_abren_sin_error(self):
        self.client.force_login(self.admin)
        nombres = (
            "farmacia_medicamento_changelist",
            "farmacia_ingresoinventario_changelist",
            "farmacia_merma_changelist",
            "farmacia_ventarecetaretenida_changelist",
            "farmacia_simulacionpago_changelist",
            "farmacia_ventacaja_changelist",
            "farmacia_detalleventacaja_changelist",
            "farmacia_autorizacionexcepcional_changelist",
            "farmacia_solicitudcuenta_changelist",
            "auth_user_changelist",
            "auth_group_changelist",
        )

        for nombre in nombres:
            with self.subTest(pantalla=nombre):
                respuesta = self.client.get(reverse(f"admin:{nombre}"))
                self.assertEqual(respuesta.status_code, 200)

    def test_permisos_de_usuario_se_muestran_con_nombres_claros(self):
        tipo_medicamento = ContentType.objects.get_for_model(Medicamento)
        Permission.objects.get_or_create(
            content_type=tipo_medicamento,
            codename="delete_medicamento",
            defaults={"name": "Can delete medicamento"},
        )
        self.client.force_login(self.admin)

        respuesta = self.client.get(
            reverse("admin:auth_user_change", args=(self.admin.pk,))
        )

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Eliminar medicamento")
        self.assertContains(respuesta, "Permisos asignados directamente")
        self.assertNotContains(respuesta, "Can delete medicamento")

    def test_formulario_para_crear_usuario_sigue_disponible(self):
        self.client.force_login(self.admin)

        respuesta = self.client.get(reverse("admin:auth_user_add"))

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Crear cuenta de usuario")

    def test_permisos_de_grupo_se_muestran_con_nombres_claros(self):
        tipo_medicamento = ContentType.objects.get_for_model(Medicamento)
        Permission.objects.get_or_create(
            content_type=tipo_medicamento,
            codename="delete_medicamento",
            defaults={"name": "Can delete medicamento"},
        )
        grupo = Group.objects.create(name="Inventario")
        self.client.force_login(self.admin)

        respuesta = self.client.get(
            reverse("admin:auth_group_change", args=(grupo.pk,))
        )

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Eliminar medicamento")
        self.assertContains(respuesta, "Permisos incluidos en este grupo")

    def test_navbar_compartida_muestra_gestion_de_inventario_a_superusuario(self):
        self.client.force_login(self.admin)
        url_inventario = reverse("admin:farmacia_ingresoinventario_changelist")
        rutas = (
            "farmacia:inicio",
            "farmacia:caja",
            "farmacia:recetas_retenidas",
            "farmacia:consultas_jefe",
        )

        for ruta in rutas:
            with self.subTest(ruta=ruta):
                respuesta = self.client.get(reverse(ruta))
                self.assertEqual(respuesta.status_code, 200)
                self.assertContains(respuesta, url_inventario)
                self.assertContains(respuesta, "Punto de Venta")
                self.assertContains(respuesta, "Gesti&oacute;n de Inventario")

    def test_navbar_no_muestra_gestion_de_inventario_sin_permiso(self):
        quimico = self.crear_quimico("quimico_sin_permiso_inventario")
        self.client.force_login(quimico)

        respuesta = self.client.get(reverse("farmacia:inicio"))

        self.assertEqual(respuesta.status_code, 200)
        self.assertNotContains(respuesta, "Gestión de Inventario")

    def test_jefe_puede_ingresar_al_admin_y_gestionar_proveedores(self):
        jefe = User.objects.create_user(
            username="jefe_gestion_proveedores",
            is_active=True,
        )
        jefe.groups.add(Group.objects.get_or_create(name="Jefe_QF")[0])
        jefe.set_password("Jefe-password-123")
        jefe.save(update_fields=("password",))

        respuesta_login = self.client.post(
            reverse("admin:login"),
            {
                "username": jefe.username,
                "password": "Jefe-password-123",
                "next": reverse("admin:index"),
            },
        )

        self.assertRedirects(
            respuesta_login,
            reverse("admin:index"),
            fetch_redirect_response=False,
        )
        respuesta = self.client.post(
            reverse("admin:farmacia_proveedor_add"),
            {
                "nombre": "Distribuidora del Sur",
                "rut": "12345678-5",
                "activo": "on",
                "_save": "Guardar",
            },
        )

        self.assertEqual(respuesta.status_code, 302)
        proveedor = Proveedor.objects.get(nombre="Distribuidora del Sur")
        self.assertEqual(proveedor.rut, "12345678-5")

        respuesta_edicion = self.client.post(
            reverse("admin:farmacia_proveedor_change", args=(proveedor.pk,)),
            {
                "nombre": "Distribuidora del Sur Actualizada",
                "rut": "12345678-5",
                "activo": "on",
                "_save": "Guardar",
            },
        )

        self.assertEqual(respuesta_edicion.status_code, 302)
        proveedor.refresh_from_db()
        self.assertEqual(proveedor.nombre, "Distribuidora del Sur Actualizada")

    def test_personal_staff_fuera_del_rol_jefe_no_puede_agregar_proveedores(self):
        usuario_staff = User.objects.create_user(
            username="staff_sin_rol_proveedor",
            password="Staff-password-123",
            is_active=True,
            is_staff=True,
        )
        self.client.force_login(usuario_staff)

        respuesta = self.client.post(
            reverse("admin:farmacia_proveedor_add"),
            {
                "nombre": "Proveedor no autorizado",
                "rut": "",
                "activo": "on",
                "_save": "Guardar",
            },
        )

        self.assertEqual(respuesta.status_code, 403)
        self.assertFalse(
            Proveedor.objects.filter(nombre="Proveedor no autorizado").exists()
        )

        respuesta_edicion = self.client.post(
            reverse("admin:farmacia_proveedor_change", args=(self.proveedor.pk,)),
            {
                "nombre": "Proveedor editado sin permiso",
                "rut": "",
                "activo": "on",
                "_save": "Guardar",
            },
        )

        self.assertEqual(respuesta_edicion.status_code, 403)
        self.proveedor.refresh_from_db()
        self.assertEqual(self.proveedor.nombre, "Proveedor de prueba")

    def test_admin_proveedor_muestra_error_controlado_ante_protected_error(self):
        self.client.force_login(self.admin)

        with patch(
            "farmacia.admin.ProveedorAdmin.save_model",
            side_effect=ProtectedError("protected", []),
        ):
            respuesta = self.client.post(
                reverse("admin:farmacia_proveedor_add"),
                {
                    "nombre": "Proveedor con error simulado",
                    "rut": "",
                    "activo": "on",
                    "_save": "Guardar",
                },
                follow=True,
            )

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(
            respuesta,
            "No se pudo guardar el proveedor por una restricción de integridad.",
        )
        self.assertNotContains(respuesta, "Traceback")
        self.assertFalse(
            Proveedor.objects.filter(nombre="Proveedor con error simulado").exists()
        )

    def test_admin_escapa_nombre_de_proveedor_antes_de_renderizarlo(self):
        Proveedor.objects.create(nombre="<script>alert(1)</script>")
        self.client.force_login(self.admin)

        respuesta = self.client.get(
            reverse("admin:farmacia_proveedor_changelist")
        )

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(respuesta, "<script>alert(1)</script>")

    def test_administrador_puede_registrar_ingreso_desde_el_panel(self):
        medicamento = Medicamento.objects.create(
            codigo="MED-ADMIN-001",
            nombre="Medicamento para ingreso",
        )
        self.client.force_login(self.admin)

        respuesta = self.client.post(
            reverse("admin:farmacia_ingresoinventario_add"),
            {
                "medicamento": medicamento.pk,
                "lote": "LOTE-ADMIN-01",
                "cantidad": 20,
                "fecha_vencimiento": (
                    timezone.localdate() + timedelta(days=180)
                ).isoformat(),
                "proveedor": self.proveedor.pk,
                "_save": "Guardar",
            },
        )

        self.assertEqual(respuesta.status_code, 302)
        ingreso = IngresoInventario.objects.get(lote="LOTE-ADMIN-01")
        self.assertEqual(ingreso.registrado_por, self.admin)

    def test_administrador_puede_registrar_merma_desde_el_panel(self):
        medicamento = Medicamento.objects.create(
            codigo="MED-ADMIN-002",
            nombre="Medicamento para merma",
        )
        ingreso = IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-ADMIN-02",
            cantidad=10,
            fecha_vencimiento=timezone.localdate() + timedelta(days=180),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        self.client.force_login(self.admin)

        respuesta = self.client.post(
            reverse("admin:farmacia_merma_add"),
            {
                "ingreso": ingreso.pk,
                "cantidad": 2,
                "motivo": Merma.Motivo.DANADO,
                "observacion": "Envase dañado al recibirlo",
                "_save": "Guardar",
            },
        )

        self.assertEqual(respuesta.status_code, 302)
        merma = Merma.objects.get(ingreso=ingreso)
        self.assertEqual(merma.registrado_por, self.admin)
        ingreso.refresh_from_db()
        self.assertEqual(ingreso.stock_disponible, 8)

    def test_merma_devuelve_429_si_sqlite_bloquea_el_guardado(self):
        medicamento = Medicamento.objects.create(
            codigo="MED-ADMIN-BLOQUEO",
            nombre="Medicamento para probar bloqueo de merma",
        )
        ingreso = IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-ADMIN-BLOQUEO",
            cantidad=10,
            fecha_vencimiento=timezone.localdate() + timedelta(days=180),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        self.client.force_login(self.admin)

        with patch(
            "farmacia.admin.MermaAdmin.save_model",
            side_effect=OperationalError("database is locked"),
        ):
            respuesta = self.client.post(
                reverse("admin:farmacia_merma_add"),
                {
                    "ingreso": ingreso.pk,
                    "cantidad": 2,
                    "motivo": Merma.Motivo.DANADO,
                    "observacion": "Envase dañado durante transporte",
                    "_save": "Guardar",
                },
                HTTP_ACCEPT="application/json",
            )

        self.assertEqual(respuesta.status_code, 429)
        self.assertEqual(
            respuesta.json()["codigo"],
            "CONCURRENCIA_EXCESIVA",
        )
        self.assertEqual(respuesta["Retry-After"], "2")
        self.assertFalse(Merma.objects.exists())

    def test_merma_requiere_observacion_en_formulario_y_modelo(self):
        medicamento = Medicamento.objects.create(
            codigo="MED-MERMA-OBS",
            nombre="Medicamento para validar merma",
        )
        ingreso = IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-MERMA-OBS",
            cantidad=5,
            fecha_vencimiento=timezone.localdate() + timedelta(days=180),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        self.client.force_login(self.admin)

        respuesta = self.client.post(
            reverse("admin:farmacia_merma_add"),
            {
                "ingreso": ingreso.pk,
                "cantidad": 1,
                "motivo": Merma.Motivo.VENCIDO,
                "observacion": "   ",
                "_save": "Guardar",
            },
        )

        self.assertEqual(respuesta.status_code, 200)
        self.assertFalse(Merma.objects.exists())
        ingreso.refresh_from_db()
        self.assertEqual(ingreso.stock_disponible, 5)
        with self.assertRaises(ValidationError):
            Merma.objects.create(
                ingreso=ingreso,
                cantidad=1,
                motivo=Merma.Motivo.VENCIDO,
                registrado_por=self.admin,
            )

    def test_ruta_inexistente_devuelve_json_personalizado_con_404(self):
        with override_settings(DEBUG=False):
            respuesta = self.client.get(
                "/ruta-que-no-existe/",
                HTTP_ACCEPT="application/json",
            )

        self.assertEqual(respuesta.status_code, 404)
        self.assertEqual(respuesta.json()["error"], "not_found")
        self.assertEqual(
            respuesta.json()["message"],
            "La ruta solicitada no existe.",
        )

    def test_ruta_inexistente_renderiza_404_amigable_sin_traceback(self):
        with override_settings(DEBUG=False):
            respuesta = self.client.get("/ruta-html-no-existe/")

        self.assertEqual(respuesta.status_code, 404)
        self.assertContains(
            respuesta,
            "Página desconocida",
            status_code=404,
        )
        self.assertNotContains(respuesta, "Error 404", status_code=404)
        self.assertContains(
            respuesta,
            "No encontramos una página disponible",
            status_code=404,
        )
        self.assertContains(
            respuesta,
            'aria-label="Navegación principal"',
            status_code=404,
        )
        self.assertContains(
            respuesta,
            "Volver al Panel Principal",
            status_code=404,
        )
        self.assertNotContains(respuesta, "Traceback", status_code=404)

    def test_vistas_de_recetas_y_consultas_devuelven_404_para_ids_inexistentes(self):
        jefe = self.crear_jefe("jefe_ids_inexistentes")
        self.client.force_login(jefe)

        respuestas = (
            self.client.get(
                reverse("farmacia:foto_receta_retenida", args=(999999,))
            ),
            self.client.get(
                reverse("farmacia:foto_consulta_jefe", args=(999999,))
            ),
            self.client.post(
                reverse("farmacia:resolver_receta_retenida", args=(999999,)),
                {"decision": "aprobar"},
            ),
            self.client.post(
                reverse("farmacia:resolver_excepcion_receta", args=(999999,)),
                {"decision": "aprobar"},
            ),
            self.client.post(
                reverse("farmacia:consultas_jefe"),
                {"consulta_id": "no-es-un-id", "respuesta": "Respuesta"},
            ),
        )

        for respuesta in respuestas:
            with self.subTest(status=respuesta.status_code):
                self.assertEqual(respuesta.status_code, 404)

    def test_error_500_devuelve_json_seguro(self):
        from config.error_handlers import handler500

        request = RequestFactory().get("/fallo/", HTTP_ACCEPT="application/json")
        respuesta = handler500(request)

        self.assertEqual(respuesta.status_code, 500)
        self.assertEqual(
            json.loads(respuesta.content),
            {
                "error": "internal_server_error",
                "message": "Ocurrió un error inesperado. Inténtalo más tarde.",
            },
        )

    def test_error_500_real_con_debug_false_no_expone_detalles(self):
        client = Client(raise_request_exception=False)
        client.force_login(self.admin)

        with override_settings(DEBUG=False):
            with patch(
                "farmacia.views._es_personal_farmacia",
                side_effect=RuntimeError("TRACE_SENTINEL"),
            ):
                respuesta = client.get(
                    reverse("farmacia:inicio"),
                    HTTP_ACCEPT="application/json",
                )
                respuesta_html = client.get(reverse("farmacia:inicio"))

        self.assertEqual(respuesta.status_code, 500)
        self.assertEqual(
            json.loads(respuesta.content),
            {
                "error": "internal_server_error",
                "message": "Ocurrió un error inesperado. Inténtalo más tarde.",
            },
        )
        self.assertNotIn(b"TRACE_SENTINEL", respuesta.content)
        self.assertEqual(respuesta_html.status_code, 500)
        self.assertContains(
            respuesta_html,
            "Error 500 - Error interno del servidor",
            status_code=500,
        )
        self.assertContains(
            respuesta_html,
            "Volver al Panel Principal",
            status_code=500,
        )
        self.assertContains(
            respuesta_html,
            'aria-label="Navegación principal"',
            status_code=500,
        )
        self.assertNotIn(b"TRACE_SENTINEL", respuesta_html.content)

    def test_stock_calcula_ingresos_menos_mermas_y_limita_merma(self):
        medicamento = Medicamento.objects.create(
            codigo="MED-001",
            nombre="Medicamento de prueba",
            stock_minimo=2,
        )
        ingreso = IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-001",
            cantidad=10,
            fecha_vencimiento=timezone.localdate() + timedelta(days=180),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        Merma.objects.create(
            ingreso=ingreso,
            cantidad=3,
            motivo=Merma.Motivo.DANADO,
            observacion="Caja dañada durante la recepción.",
            registrado_por=self.admin,
        )

        self.assertEqual(medicamento.stock_actual, 7)
        self.assertEqual(ingreso.stock_disponible, 7)

        with self.assertRaises(ValidationError):
            Merma.objects.create(
                ingreso=ingreso,
                cantidad=8,
                motivo=Merma.Motivo.PERDIDA,
                registrado_por=self.admin,
            )

        self.assertEqual(ingreso.mermas.count(), 1)
        self.assertEqual(ingreso.mermas.count(), 1)

    def test_ingreso_no_acepta_lote_vencido(self):
        medicamento = Medicamento.objects.create(
            codigo="MED-INGRESO-VENCIDO",
            nombre="Medicamento vencido",
        )
        ingreso = IngresoInventario(
            medicamento=medicamento,
            lote="LOTE-INGRESO-VENCIDO",
            cantidad=1,
            fecha_vencimiento=timezone.localdate() - timedelta(days=1),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )

        with self.assertRaises(ValidationError):
            ingreso.save()

        self.assertFalse(
            IngresoInventario.objects.filter(lote="LOTE-INGRESO-VENCIDO").exists()
        )

    def test_enteros_fuera_de_rango_no_crean_ni_modifican_stock(self):
        medicamento = Medicamento.objects.create(
            codigo="MED-CANTIDAD-FUERA-RANGO",
            nombre="Medicamento para validar límites",
        )
        cantidad_gigante = 10**100

        with self.assertRaises(ValidationError):
            IngresoInventario.objects.create(
                medicamento=medicamento,
                lote="LOTE-CANTIDAD-GIGANTE",
                cantidad=cantidad_gigante,
                fecha_vencimiento=timezone.localdate() + timedelta(days=60),
                proveedor=self.proveedor,
                registrado_por=self.admin,
            )
        self.assertFalse(
            IngresoInventario.objects.filter(lote="LOTE-CANTIDAD-GIGANTE").exists()
        )

        ingreso = IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-MERMA-CANTIDAD-GIGANTE",
            cantidad=5,
            fecha_vencimiento=timezone.localdate() + timedelta(days=60),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        with self.assertRaises(ValidationError):
            Merma.objects.create(
                ingreso=ingreso,
                cantidad=cantidad_gigante,
                motivo=Merma.Motivo.DANADO,
                observacion="Cantidad inválida para prueba de límite.",
                registrado_por=self.admin,
            )
        ingreso.refresh_from_db()
        self.assertEqual(ingreso.stock_disponible, 5)
        self.assertFalse(ingreso.mermas.exists())

    def test_modelo_medicamento_valida_limites_de_stock_y_precio(self):
        medicamento = Medicamento(
            codigo="MED-STOCK-MINIMO-GIGANTE",
            nombre="Medicamento con stock mínimo inválido",
            stock_minimo=10**100,
        )
        with self.assertRaises(ValidationError) as error:
            medicamento.full_clean()
        self.assertIn("stock_minimo", error.exception.message_dict)

        medicamento = Medicamento(
            codigo="MED-PRECIO-GIGANTE",
            nombre="Medicamento con precio inválido",
            precio=Decimal("10000000000.00"),
        )
        with self.assertRaises(ValidationError) as error:
            medicamento.full_clean()
        self.assertIn("precio", error.exception.message_dict)

    def test_lotes_vencidos_no_aparecen_como_stock_disponible_en_admin(self):
        medicamento = Medicamento.objects.create(
            codigo="MED-VENCIDO-001",
            nombre="Producto con lote vencido",
            stock_minimo=2,
        )
        ingreso_vencido = IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-VENCIDO-01",
            cantidad=10,
            fecha_vencimiento=timezone.localdate() + timedelta(days=1),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        IngresoInventario.objects.filter(pk=ingreso_vencido.pk).update(
            fecha_vencimiento=timezone.localdate() - timedelta(days=1)
        )
        IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-VIGENTE-01",
            cantidad=3,
            fecha_vencimiento=timezone.localdate() + timedelta(days=30),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        self.client.force_login(self.admin)

        inicio = self.client.get(reverse("admin:index"))
        lista = self.client.get(reverse("admin:farmacia_medicamento_changelist"))

        self.assertEqual(medicamento.stock_actual, 3)
        self.assertEqual(inicio.context["resumen_farmacia"]["stock_bajo"], 0)
        medicamento_en_lista = lista.context["cl"].result_list[0]
        self.assertEqual(medicamento_en_lista._stock_actual, 3)

    def test_solo_lotes_vencidos_cuentan_como_stock_bajo_en_admin(self):
        medicamento = Medicamento.objects.create(
            codigo="MED-VENCIDO-002",
            nombre="Producto vencido sin stock vigente",
            stock_minimo=2,
        )
        ingreso_vencido = IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-VENCIDO-02",
            cantidad=10,
            fecha_vencimiento=timezone.localdate() + timedelta(days=1),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        IngresoInventario.objects.filter(pk=ingreso_vencido.pk).update(
            fecha_vencimiento=timezone.localdate() - timedelta(days=1)
        )
        self.client.force_login(self.admin)

        inicio = self.client.get(reverse("admin:index"))

        self.assertEqual(inicio.context["resumen_farmacia"]["stock_bajo"], 1)

    def test_ajuste_lote_actualiza_stock_por_delta_y_registra_auditoria(self):
        jefe = self.crear_jefe("jefe_ajuste_lote")
        jefe.set_password("Jefe-password-123")
        jefe.save(update_fields=("password",))
        medicamento = Medicamento.objects.create(
            codigo="MED-AJUSTE-LOTE",
            nombre="Medicamento para ajuste de lote",
        )
        vencimiento_inicial = timezone.localdate() + timedelta(days=90)
        ingreso = IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-AJUSTE-01",
            cantidad=10,
            fecha_vencimiento=vencimiento_inicial,
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        IngresoInventario.objects.filter(pk=ingreso.pk).update(
            stock_disponible=8
        )
        self.client.force_login(jefe)
        datos_ajuste = {
            "cantidad": "8",
            "fecha_vencimiento": (
                timezone.localdate() + timedelta(days=120)
            ).isoformat(),
            "motivo": "Se corrige el conteo del ingreso.",
            "password_confirmacion": "Clave-erronea",
        }

        respuesta = self.client.post(
            reverse("farmacia:ajustar_stock_lote", args=(ingreso.pk,)),
            datos_ajuste,
        )

        self.assertEqual(respuesta.status_code, 400)
        self.assertContains(
            respuesta,
            "Contraseña de confirmación incorrecta.",
            status_code=400,
        )
        ingreso.refresh_from_db()
        self.assertEqual(ingreso.cantidad, 10)
        self.assertEqual(ingreso.stock_disponible, 8)
        self.assertFalse(AuditoriaInventario.objects.exists())

        respuesta = self.client.post(
            reverse("farmacia:ajustar_stock_lote", args=(ingreso.pk,)),
            {
                "cantidad": "8",
                "fecha_vencimiento": datos_ajuste["fecha_vencimiento"],
                "motivo": "Se corrige el conteo del ingreso.",
                "password_confirmacion": "Jefe-password-123",
            },
        )

        self.assertRedirects(
            respuesta,
            reverse("admin:farmacia_ingresoinventario_changelist"),
            fetch_redirect_response=False,
        )
        ingreso.refresh_from_db()
        auditoria = AuditoriaInventario.objects.get()
        self.assertEqual(ingreso.cantidad, 8)
        self.assertEqual(ingreso.stock_disponible, 6)
        self.assertEqual(
            auditoria.tipo_ajuste,
            AuditoriaInventario.TipoAjuste.REDUCCION_STOCK,
        )
        self.assertEqual(auditoria.cantidad_anterior, 8)
        self.assertEqual(auditoria.cantidad_nueva, 6)
        self.assertIn("conteo del ingreso", auditoria.motivo)
        self.assertIn("Cantidad recibida corregida de 10 a 8", auditoria.motivo)

        fecha_corregida = timezone.localdate() + timedelta(days=150)
        respuesta = self.client.post(
            reverse("farmacia:ajustar_stock_lote", args=(ingreso.pk,)),
            {
                "cantidad": "8",
                "fecha_vencimiento": fecha_corregida.isoformat(),
                "motivo": "Se corrige la fecha según el envase.",
                "password_confirmacion": "Jefe-password-123",
            },
        )

        self.assertEqual(respuesta.status_code, 302)
        ingreso.refresh_from_db()
        self.assertEqual(ingreso.stock_disponible, 6)
        self.assertEqual(ingreso.fecha_vencimiento, fecha_corregida)
        self.assertEqual(AuditoriaInventario.objects.count(), 2)
        self.assertEqual(
            AuditoriaInventario.objects.order_by("-pk").first().tipo_ajuste,
            AuditoriaInventario.TipoAjuste.CORRECCION_INGRESO,
        )

        respuesta = self.client.post(
            reverse("farmacia:ajustar_stock_lote", args=(ingreso.pk,)),
            {
                "cantidad": "2",
                "fecha_vencimiento": fecha_corregida.isoformat(),
                "motivo": "Se corrige el conteo a dos unidades recibidas.",
                "password_confirmacion": "Jefe-password-123",
            },
        )

        self.assertEqual(respuesta.status_code, 302)
        ingreso.refresh_from_db()
        self.assertEqual(ingreso.stock_disponible, 0)
        self.assertEqual(AuditoriaInventario.objects.count(), 3)

    def test_ajuste_lote_rechaza_roles_no_autorizados_y_stock_negativo(self):
        quimico = self.crear_quimico("quimico_sin_ajuste")
        medicamento = Medicamento.objects.create(
            codigo="MED-AJUSTE-DENEGADO",
            nombre="Medicamento para rechazar ajuste",
        )
        ingreso = IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-AJUSTE-DENEGADO",
            cantidad=10,
            fecha_vencimiento=timezone.localdate() + timedelta(days=90),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        self.client.force_login(quimico)

        for metodo in ("get", "post"):
            respuesta = getattr(self.client, metodo)(
                reverse("farmacia:ajustar_stock_lote", args=(ingreso.pk,)),
                {
                    "cantidad": "1",
                    "fecha_vencimiento": timezone.localdate().isoformat(),
                    "motivo": "Intento sin permisos.",
                    "password_confirmacion": "Jefe-password-123",
                },
            )
            self.assertEqual(respuesta.status_code, 403)

        jefe = self.crear_jefe("jefe_ajuste_negativo")
        jefe.set_password("Jefe-password-123")
        jefe.save(update_fields=("password",))
        IngresoInventario.objects.filter(pk=ingreso.pk).update(
            stock_disponible=4
        )
        self.client.force_login(jefe)
        respuesta = self.client.post(
            reverse("farmacia:ajustar_stock_lote", args=(ingreso.pk,)),
            {
                "cantidad": "5",
                "fecha_vencimiento": (
                    timezone.localdate() + timedelta(days=90)
                ).isoformat(),
                "motivo": "     ",
                "password_confirmacion": "Jefe-password-123",
            },
        )
        self.assertEqual(respuesta.status_code, 400)
        self.assertFalse(AuditoriaInventario.objects.exists())

        respuesta = self.client.post(
            reverse("farmacia:ajustar_stock_lote", args=(ingreso.pk,)),
            {
                "cantidad": "5",
                "fecha_vencimiento": (
                    timezone.localdate() + timedelta(days=90)
                ).isoformat(),
                "motivo": "El ajuste produciría stock negativo.",
                "password_confirmacion": "Jefe-password-123",
            },
        )

        self.assertEqual(respuesta.status_code, 400)
        ingreso.refresh_from_db()
        self.assertEqual(ingreso.cantidad, 10)
        self.assertEqual(ingreso.stock_disponible, 4)
        self.assertFalse(AuditoriaInventario.objects.exists())

    def test_reversion_merma_reintegra_stock_sin_borrar_y_no_se_repite(self):
        jefe = self.crear_jefe("jefe_revertir_merma")
        jefe.set_password("Jefe-password-123")
        jefe.save(update_fields=("password",))
        medicamento = Medicamento.objects.create(
            codigo="MED-REVERTIR-MERMA",
            nombre="Medicamento para reversión",
        )
        ingreso = IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-REVERTIR-01",
            cantidad=10,
            fecha_vencimiento=timezone.localdate() + timedelta(days=90),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        merma = Merma.objects.create(
            ingreso=ingreso,
            cantidad=3,
            motivo=Merma.Motivo.DANADO,
            observacion="Caja dañada durante la recepción.",
            registrado_por=self.admin,
        )
        self.client.force_login(jefe)

        respuesta = self.client.post(
            reverse("farmacia:revertir_merma", args=(merma.pk,)),
            {
                "motivo": "La merma se registró por error.",
                "password_confirmacion": "Clave-erronea",
            },
        )
        self.assertEqual(respuesta.status_code, 400)
        self.assertContains(
            respuesta,
            "Contraseña de confirmación incorrecta.",
            status_code=400,
        )
        ingreso.refresh_from_db()
        merma.refresh_from_db()
        self.assertEqual(ingreso.stock_disponible, 7)
        self.assertFalse(merma.revertida)
        self.assertFalse(AuditoriaInventario.objects.exists())

        respuesta = self.client.post(
            reverse("farmacia:revertir_merma", args=(merma.pk,)),
            {
                "motivo": "La merma se registró por error.",
                "password_confirmacion": "Jefe-password-123",
            },
        )

        self.assertRedirects(
            respuesta,
            reverse("admin:farmacia_merma_changelist"),
            fetch_redirect_response=False,
        )
        ingreso.refresh_from_db()
        merma.refresh_from_db()
        auditoria = AuditoriaInventario.objects.get()
        self.assertEqual(ingreso.stock_disponible, 10)
        self.assertTrue(merma.revertida)
        self.assertEqual(merma.ingreso_id, ingreso.pk)
        self.assertEqual(ingreso.mermas_totales, 0)
        self.assertEqual(auditoria.merma_id, merma.pk)
        self.assertEqual(
            auditoria.tipo_ajuste,
            AuditoriaInventario.TipoAjuste.REVERSION_MERMA,
        )
        self.assertEqual(auditoria.cantidad_anterior, 7)
        self.assertEqual(auditoria.cantidad_nueva, 10)

        respuesta = self.client.post(
            reverse("farmacia:revertir_merma", args=(merma.pk,)),
            {
                "motivo": "Segundo intento de reversión.",
                "password_confirmacion": "Jefe-password-123",
            },
        )

        self.assertEqual(respuesta.status_code, 400)
        ingreso.refresh_from_db()
        self.assertEqual(ingreso.stock_disponible, 10)
        self.assertEqual(AuditoriaInventario.objects.count(), 1)

    def test_ajustes_inventario_limita_a_cinco_intentos_por_minuto(self):
        jefe = self.crear_jefe("jefe_rate_limit_inventario")
        jefe.set_password("Jefe-password-123")
        jefe.save(update_fields=("password",))
        medicamento = Medicamento.objects.create(
            codigo="MED-RATE-LIMIT-AJUSTE",
            nombre="Medicamento para límite de ajustes",
        )
        ingreso = IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-RATE-LIMIT-AJUSTE",
            cantidad=5,
            fecha_vencimiento=timezone.localdate() + timedelta(days=90),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        self.client.force_login(jefe)
        url = reverse("farmacia:ajustar_stock_lote", args=(ingreso.pk,))
        datos = {
            "cantidad": "4",
            "fecha_vencimiento": (
                timezone.localdate() + timedelta(days=90)
            ).isoformat(),
            "motivo": "Intento de ajuste con contraseña incorrecta.",
            "password_confirmacion": "Clave-erronea",
        }

        for intento in range(1, 6):
            with self.subTest(intento=intento):
                respuesta = self.client.post(url, datos)
                self.assertEqual(respuesta.status_code, 400)

        respuesta = self.client.post(
            url,
            datos,
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(respuesta.status_code, 429)
        self.assertEqual(respuesta["Retry-After"], "60")
        ingreso.refresh_from_db()
        self.assertEqual(ingreso.cantidad, 5)
        self.assertEqual(ingreso.stock_disponible, 5)
        self.assertFalse(AuditoriaInventario.objects.exists())

    def test_admin_muestra_dialogos_csrf_y_auditoria_solo_lectura(self):
        medicamento = Medicamento.objects.create(
            codigo="MED-ADMIN-AJUSTE",
            nombre="Medicamento visible en administración",
        )
        ingreso = IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-ADMIN-AJUSTE",
            cantidad=5,
            fecha_vencimiento=timezone.localdate() + timedelta(days=90),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        Merma.objects.create(
            ingreso=ingreso,
            cantidad=1,
            motivo=Merma.Motivo.DANADO,
            observacion="Envase con daño visible.",
            registrado_por=self.admin,
        )
        AuditoriaInventario.objects.create(
            usuario=self.admin,
            lote=ingreso,
            tipo_ajuste=AuditoriaInventario.TipoAjuste.CORRECCION_INGRESO,
            cantidad_anterior=4,
            cantidad_nueva=5,
            motivo="Corrección de prueba.",
        )
        self.client.force_login(self.admin)

        inventario = self.client.get(
            reverse("admin:farmacia_ingresoinventario_changelist")
        )
        mermas = self.client.get(reverse("admin:farmacia_merma_changelist"))
        auditoria = self.client.get(
            reverse("admin:farmacia_auditoriainventario_changelist")
        )

        self.assertEqual(inventario.status_code, 200)
        self.assertContains(inventario, "Corregir lote / stock")
        self.assertContains(inventario, "ajustar-lote-dialog")
        self.assertContains(inventario, 'name="csrfmiddlewaretoken"')
        self.assertContains(inventario, 'name="password_confirmacion"')
        self.assertEqual(mermas.status_code, 200)
        self.assertContains(mermas, "Anular / corregir merma")
        self.assertContains(mermas, "revertir-merma-dialog")
        self.assertContains(mermas, 'name="csrfmiddlewaretoken"')
        self.assertContains(mermas, 'name="password_confirmacion"')
        self.assertEqual(auditoria.status_code, 200)
        self.assertContains(auditoria, "Corrección de ingreso")
        self.assertNotContains(auditoria, "Add auditoría de inventario")

    def test_jefe_farmacia_ve_acciones_de_ajuste_y_reversion_en_admin(self):
        jefe = self.crear_jefe("jefe_botones_inventario")
        medicamento = Medicamento.objects.create(
            codigo="MED-BOTONES-JEFE",
            nombre="Medicamento para botones del jefe",
        )
        ingreso = IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-BOTONES-JEFE",
            cantidad=5,
            fecha_vencimiento=timezone.localdate() + timedelta(days=90),
            proveedor=self.proveedor,
            registrado_por=self.admin,
        )
        Merma.objects.create(
            ingreso=ingreso,
            cantidad=1,
            motivo=Merma.Motivo.DANADO,
            observacion="Caja dañada.",
            registrado_por=self.admin,
        )
        self.client.force_login(jefe)

        inventario = self.client.get(
            reverse("admin:farmacia_ingresoinventario_changelist")
        )
        mermas = self.client.get(reverse("admin:farmacia_merma_changelist"))

        self.assertEqual(inventario.status_code, 200)
        self.assertContains(inventario, "Corregir lote / stock")
        self.assertEqual(mermas.status_code, 200)
        self.assertContains(mermas, "Anular / corregir merma")

    def test_jefe_farmacia_ve_el_acceso_a_gestion_de_inventario(self):
        jefe = self.crear_jefe("jefe_nav_inventario")
        self.client.force_login(jefe)

        respuesta = self.client.get(reverse("farmacia:inicio"))

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(
            respuesta,
            reverse("admin:farmacia_ingresoinventario_changelist"),
        )
        self.assertContains(respuesta, "Gesti&oacute;n de Inventario")

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_password_reset_envia_correo_html_y_texto_con_enlace_valido(self):
        usuario = User.objects.create_user(
            username="usuario_password_reset",
            email="reset@example.com",
            password="Original-password-123",
            is_active=True,
        )

        respuesta = self.client.post(
            reverse("farmacia:password_reset"),
            {"email": usuario.email},
        )

        self.assertRedirects(
            respuesta,
            reverse("farmacia:password_reset_done"),
        )
        self.assertEqual(len(mail.outbox), 1)
        correo = mail.outbox[0]
        self.assertEqual(correo.from_email, "no-reply@farmasistem.local")
        self.assertEqual(len(correo.alternatives), 1)
        html = correo.alternatives[0][0]
        uid = urlsafe_base64_encode(force_bytes(usuario.pk))
        token = default_token_generator.make_token(usuario)
        enlace = reverse(
            "farmacia:password_reset_confirm",
            kwargs={"uidb64": uid, "token": token},
        )
        enlace_absoluto = f"http://testserver{enlace}"
        self.assertIn(enlace_absoluto, correo.body)
        self.assertIn(enlace_absoluto, html)

        confirmacion = self.client.get(enlace, follow=True)
        self.assertEqual(confirmacion.status_code, 200)
        self.assertContains(confirmacion, "csrfmiddlewaretoken")
        clave_intentos = f"failed_login_{usuario.username}"
        cache.set(
            clave_intentos,
            settings.FARMACIA_LOGIN_MAX_FAILED_ATTEMPTS,
            timeout=settings.FARMACIA_LOGIN_LOCKOUT_SECONDS,
        )
        guardado = self.client.post(
            confirmacion.redirect_chain[-1][0],
            {
                "new_password1": "Nueva-segura-456!",
                "new_password2": "Nueva-segura-456!",
            },
        )
        self.assertRedirects(
            guardado,
            reverse("farmacia:password_reset_complete"),
        )
        usuario.refresh_from_db()
        self.assertTrue(usuario.check_password("Nueva-segura-456!"))
        self.assertIsNone(cache.get(clave_intentos))

    def test_password_reset_paginas_comparten_base_y_navegacion_publica(self):
        uid = urlsafe_base64_encode(force_bytes(self.admin.pk))
        rutas = (
            reverse("farmacia:password_reset"),
            reverse("farmacia:password_reset_done"),
            reverse(
                "farmacia:password_reset_confirm",
                kwargs={"uidb64": uid, "token": "token-invalido"},
            ),
            reverse("farmacia:password_reset_complete"),
        )

        for ruta in rutas:
            with self.subTest(ruta=ruta):
                respuesta = self.client.get(ruta)
                self.assertEqual(respuesta.status_code, 200)
                self.assertContains(
                    respuesta,
                    'aria-label="Navegación principal"',
                )
                self.assertContains(respuesta, "Iniciar sesión")

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_password_reset_no_revela_si_el_correo_no_existe(self):
        respuesta = self.client.post(
            reverse("farmacia:password_reset"),
            {"email": "no-existe@example.com"},
        )

        self.assertRedirects(
            respuesta,
            reverse("farmacia:password_reset_done"),
        )
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        ALLOWED_HOSTS=["testserver"],
    )
    def test_password_reset_rechaza_host_no_permitido(self):
        usuario = User.objects.create_user(
            username="usuario_host_reset",
            email="host-reset@example.com",
            password="Original-password-123",
            is_active=True,
        )

        respuesta = self.client.post(
            reverse("farmacia:password_reset"),
            {"email": usuario.email},
            HTTP_HOST="host-no-autorizado.invalid",
        )

        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        PASSWORD_RESET_TIMEOUT=1,
    )
    def test_password_reset_rechaza_token_expirado(self):
        usuario = User.objects.create_user(
            username="usuario_token_expirado",
            email="token-expirado@example.com",
            password="Original-password-123",
            is_active=True,
        )
        instante_token = default_token_generator._now()
        with patch.object(
            default_token_generator,
            "_now",
            return_value=instante_token,
        ):
            token = default_token_generator.make_token(usuario)
        uid = urlsafe_base64_encode(force_bytes(usuario.pk))
        enlace = reverse(
            "farmacia:password_reset_confirm",
            kwargs={"uidb64": uid, "token": token},
        )

        with patch.object(
            default_token_generator,
            "_now",
            return_value=instante_token + timedelta(seconds=2),
        ):
            respuesta = self.client.get(enlace)

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Este enlace no es válido o ya fue utilizado.")
        usuario.refresh_from_db()
        self.assertTrue(usuario.check_password("Original-password-123"))
