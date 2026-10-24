from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse

from farmacia.models import (
    GRUPO_CLIENTE,
    GRUPO_FARMACEUTICO,
    IngresoInventario,
    Medicamento,
    Merma,
    SolicitudCuenta,
)

User = get_user_model()


class RegistroUsuarioTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="jefe",
            email="jefe@example.com",
            password="Admin-password-123",
        )
        self.datos_registro = {
            "username": "nuevo_usuario",
            "first_name": "Nuevo",
            "last_name": "Usuario",
            "email": "nuevo@example.com",
            "rol_solicitado": SolicitudCuenta.RolSolicitado.FARMACEUTICO,
            "password1": "Cuenta-segura-123",
            "password2": "Cuenta-segura-123",
        }

    def test_registro_crea_cuenta_pendiente_inactiva(self):
        respuesta = self.client.post(
            reverse("registro"),
            self.datos_registro,
        )

        self.assertRedirects(
            respuesta,
            reverse("login"),
            fetch_redirect_response=False,
        )
        usuario = User.objects.get(username="nuevo_usuario")
        solicitud = usuario.solicitud_cuenta
        self.assertFalse(usuario.is_active)
        self.assertEqual(solicitud.estado, SolicitudCuenta.Estado.PENDIENTE)
        self.assertFalse(
            self.client.login(username="nuevo_usuario", password="Cuenta-segura-123")
        )

    def test_cliente_se_registra_activo_sin_aprobacion(self):
        self.datos_registro["rol_solicitado"] = SolicitudCuenta.RolSolicitado.CLIENTE

        respuesta = self.client.post(reverse("registro"), self.datos_registro)

        self.assertRedirects(
            respuesta,
            reverse("login"),
            fetch_redirect_response=False,
        )
        cliente = User.objects.get(username="nuevo_usuario")
        self.assertTrue(cliente.is_active)
        self.assertFalse(SolicitudCuenta.objects.filter(usuario=cliente).exists())
        self.assertTrue(cliente.groups.filter(name=GRUPO_CLIENTE).exists())
        self.assertTrue(
            self.client.login(username="nuevo_usuario", password="Cuenta-segura-123")
        )

    def test_pagina_de_login_incluye_registro_y_ambos_roles_publicos(self):
        respuesta = self.client.get(reverse("login"))

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Registrarse")
        self.assertContains(respuesta, 'value="farmaceutico"')
        self.assertContains(respuesta, 'value="cliente"')
        self.assertNotContains(respuesta, 'value="jefe"')

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

    def test_cliente_va_al_catalogo_al_iniciar_sesion(self):
        cliente = User.objects.create_user(
            username="cliente",
            password="Cliente-password-123",
        )
        cliente.is_active = True
        cliente.save(update_fields=("is_active",))

        respuesta = self.client.post(
            reverse("login"),
            {
                "username": "cliente",
                "password": "Cliente-password-123",
            },
        )

        self.assertRedirects(
            respuesta,
            reverse("catalogo"),
            fetch_redirect_response=False,
        )

    def test_administrador_aprueba_y_asigna_grupo_solicitado(self):
        self.client.post(reverse("registro"), self.datos_registro)
        solicitud = SolicitudCuenta.objects.get(usuario__username="nuevo_usuario")

        solicitud.aprobar(self.admin)

        usuario = solicitud.usuario
        self.assertTrue(usuario.is_active)
        self.assertEqual(solicitud.estado, SolicitudCuenta.Estado.APROBADA)
        self.assertTrue(usuario.groups.filter(name=GRUPO_FARMACEUTICO).exists())
        self.assertTrue(
            self.client.login(username="nuevo_usuario", password="Cuenta-segura-123")
        )

    def test_solicitante_no_puede_pedir_rol_administrador(self):
        datos = {**self.datos_registro, "rol_solicitado": "jefe"}

        respuesta = self.client.post(
            reverse("registro"),
            datos,
        )

        self.assertEqual(respuesta.status_code, 400)
        self.assertFalse(User.objects.filter(username="nuevo_usuario").exists())

    def test_usuario_no_administrador_no_puede_aprobar_solicitudes(self):
        self.client.post(reverse("registro"), self.datos_registro)
        solicitud = SolicitudCuenta.objects.get(usuario__username="nuevo_usuario")
        usuario_normal = User.objects.create_user(
            username="otro",
            email="otro@example.com",
            password="Otra-password-123",
        )

        with self.assertRaises(PermissionDenied):
            solicitud.aprobar(usuario_normal)

        self.assertEqual(solicitud.estado, SolicitudCuenta.Estado.PENDIENTE)
        self.assertFalse(solicitud.usuario.is_active)

    def test_cliente_nuevo_no_requiere_que_admin_apruebe_solicitud(self):
        self.datos_registro["rol_solicitado"] = SolicitudCuenta.RolSolicitado.CLIENTE
        self.client.post(reverse("registro"), self.datos_registro)
        cliente = User.objects.get(username="nuevo_usuario")

        grupos = set(cliente.groups.values_list("name", flat=True))
        self.assertEqual(grupos, {"Cliente"})
        self.assertFalse(cliente.is_staff)
        self.assertFalse(cliente.is_superuser)

    def test_solicitud_pendiente_se_puede_rechazar(self):
        self.client.post(reverse("registro"), self.datos_registro)
        solicitud = SolicitudCuenta.objects.get(usuario__username="nuevo_usuario")

        solicitud.rechazar(self.admin)

        self.assertEqual(solicitud.estado, SolicitudCuenta.Estado.RECHAZADA)
        self.assertFalse(solicitud.usuario.is_active)
        self.assertIsNotNone(solicitud.fecha_revision)
        self.assertFalse(
            self.client.login(username="nuevo_usuario", password="Cuenta-segura-123")
        )

    def test_administrador_aprueba_solicitud_desde_panel_admin(self):
        self.client.post(reverse("registro"), self.datos_registro)
        solicitud = SolicitudCuenta.objects.get(usuario__username="nuevo_usuario")
        self.client.force_login(self.admin)

        respuesta = self.client.post(
            reverse("admin:farmacia_solicitudcuenta_changelist"),
            {
                "action": "aprobar_solicitudes",
                "_selected_action": [solicitud.pk],
                "index": 0,
            },
        )

        self.assertEqual(respuesta.status_code, 302)
        solicitud.refresh_from_db()
        self.assertEqual(solicitud.estado, SolicitudCuenta.Estado.APROBADA)
        self.assertTrue(solicitud.usuario.is_active)

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
        self.assertContains(respuesta, "farmacia/admin.css")
        self.assertContains(respuesta, "Resumen de inventario")
        self.assertContains(respuesta, "Registrar o revisar ingresos")
        self.assertContains(respuesta, "Solicitudes de qu")

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
                "fecha_vencimiento": (date.today() + timedelta(days=180)).isoformat(),
                "proveedor": "Proveedor de prueba",
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
            fecha_vencimiento=date.today() + timedelta(days=180),
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
        self.assertEqual(ingreso.stock_disponible, 8)

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
            fecha_vencimiento=date.today() + timedelta(days=180),
            proveedor="Proveedor de prueba",
            registrado_por=self.admin,
        )
        Merma.objects.create(
            ingreso=ingreso,
            cantidad=3,
            motivo=Merma.Motivo.DANADO,
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
