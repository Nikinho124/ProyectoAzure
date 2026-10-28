from io import BytesIO
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from PIL import Image

from farmacia.models import (
    GRUPO_CLIENTE,
    GRUPO_FARMACEUTICO,
    IngresoInventario,
    Medicamento,
    Merma,
    SolicitudCuenta,
    SolicitudRevisionReceta,
    SimulacionPago,
    VentaCaja,
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
        self.assertContains(respuesta, "pie-sitio")
        self.assertContains(respuesta, "Tel&eacute;fono")
        self.assertContains(respuesta, 'value="farmaceutico"')
        self.assertContains(respuesta, 'value="cliente"')
        self.assertNotContains(respuesta, 'value="jefe"')

    def test_catalogo_publico_incluye_pie_y_estilos_del_sitio(self):
        respuesta = self.client.get(reverse("catalogo"))

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "pie-sitio")
        self.assertContains(respuesta, "Horarios de atenci")
        self.assertContains(respuesta, "farmacia/sitio.css?v=1")

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
            fecha_vencimiento=date.today() + timedelta(days=90),
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

    def test_cliente_no_puede_entrar_a_caja_ni_inicio_de_farmacia(self):
        cliente = User.objects.create_user(
            username="cliente_privado",
            password="Cliente-password-123",
            is_active=True,
        )
        cliente.groups.add(Group.objects.get_or_create(name=GRUPO_CLIENTE)[0])
        self.client.force_login(cliente)

        inicio = self.client.get(reverse("farmacia:inicio"))
        caja = self.client.get(reverse("farmacia:caja"))

        self.assertEqual(inicio.status_code, 403)
        self.assertEqual(caja.status_code, 403)

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
            fecha_vencimiento=date.today() + timedelta(days=90),
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
            fecha_vencimiento=date.today() + timedelta(days=90),
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
            fecha_vencimiento=date.today() + timedelta(days=30),
            registrado_por=self.admin,
        )
        lote_posterior = IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-VENTA-POSTERIOR",
            cantidad=5,
            fecha_vencimiento=date.today() + timedelta(days=60),
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
            fecha_vencimiento=date.today() + timedelta(days=60),
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

        self.assertContains(respuesta, "Stock insuficiente")
        self.assertFalse(VentaCaja.objects.exists())
        self.assertFalse(SimulacionPago.objects.exists())
        ingreso.refresh_from_db()
        self.assertEqual(ingreso.stock_disponible, 1)

    def test_caja_no_vende_medicamento_con_receta_sin_validacion(self):
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
            fecha_vencimiento=date.today() + timedelta(days=60),
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
            follow=True,
        )

        self.assertContains(respuesta, "requieren el flujo de validación")
        self.assertEqual(self.client.session.get("farmacia_caja_carrito", {}), {})
        self.assertFalse(VentaCaja.objects.exists())

    def crear_imagen_receta(self):
        archivo = BytesIO()
        Image.new("RGB", (4, 4), color="white").save(archivo, format="PNG")
        return SimpleUploadedFile(
            "receta.png",
            archivo.getvalue(),
            content_type="image/png",
        )

    def datos_solicitud_receta(self):
        return {
            "cliente_nombre": "Cliente de prueba",
            "cliente_rut": "11.111.111-1",
            "medicamento": "Medicamento recetado",
            "cantidad": "1",
            "medico_prescriptor": "Dra. Ejemplo",
            "foto_receta": self.crear_imagen_receta(),
        }

    def test_visitante_puede_enviar_receta_y_solo_personal_autorizado_verla(self):
        respuesta = self.client.post(
            reverse("farmacia:solicitar_receta"),
            self.datos_solicitud_receta(),
        )

        self.assertRedirects(
            respuesta,
            reverse("farmacia:solicitar_receta"),
            fetch_redirect_response=False,
        )
        solicitud = SolicitudRevisionReceta.objects.get()
        self.addCleanup(lambda: solicitud.foto_receta.delete(save=False))
        self.assertIsNone(solicitud.cliente)
        self.assertEqual(solicitud.estado, SolicitudRevisionReceta.Estado.PENDIENTE)
        self.assertIn("private_recetas", solicitud.foto_receta.path)

        foto_no_autorizada = self.client.get(
            reverse("farmacia:foto_receta", args=(solicitud.pk,))
        )
        self.assertEqual(foto_no_autorizada.status_code, 302)

        quimico = User.objects.create_user(
            username="quimico_revision",
            password="Revision-test-123",
            is_active=True,
        )
        quimico.groups.add(Group.objects.get_or_create(name=GRUPO_FARMACEUTICO)[0])
        self.client.force_login(quimico)

        bandeja = self.client.get(reverse("farmacia:revisar_recetas"))
        self.assertContains(bandeja, "Cliente de prueba")
        self.assertContains(bandeja, "Medicamento recetado")
        self.assertContains(bandeja, "Aprobar revisión")
        foto = self.client.get(
            reverse("farmacia:foto_receta", args=(solicitud.pk,))
        )
        self.assertEqual(foto.status_code, 200)
        self.assertEqual(foto["Content-Type"], "image/png")
        self.assertEqual(foto["X-Content-Type-Options"], "nosniff")
        self.assertEqual(foto["Cache-Control"], "private, no-store")
        foto.close()

        respuesta = self.client.post(
            reverse("farmacia:resolver_receta", args=(solicitud.pk,)),
            {"decision": "aprobar", "respuesta": "Receta legible."},
            follow=True,
        )

        self.assertContains(respuesta, f"La solicitud #{solicitud.pk} quedó aprobada")
        solicitud.refresh_from_db()
        self.assertEqual(solicitud.estado, SolicitudRevisionReceta.Estado.APROBADA)
        self.assertEqual(solicitud.revisada_por, quimico)
        self.assertEqual(solicitud.respuesta, "Receta legible.")
        self.assertIsNotNone(solicitud.fecha_revision)
        self.assertContains(respuesta, "Solicitudes revisadas")

    def test_cliente_con_cuenta_puede_enviar_receta_asociada_a_su_usuario(self):
        cliente = User.objects.create_user(
            username="cliente_receta",
            password="Cliente-test-123",
            is_active=True,
        )
        cliente.groups.add(Group.objects.get_or_create(name=GRUPO_CLIENTE)[0])
        self.client.force_login(cliente)

        respuesta = self.client.post(
            reverse("farmacia:solicitar_receta"),
            self.datos_solicitud_receta(),
        )

        self.assertRedirects(
            respuesta,
            reverse("farmacia:solicitar_receta"),
            fetch_redirect_response=False,
        )
        solicitud = SolicitudRevisionReceta.objects.get()
        self.addCleanup(lambda: solicitud.foto_receta.delete(save=False))
        self.assertEqual(solicitud.cliente, cliente)

    def test_usuario_que_no_es_cliente_no_puede_enviar_solicitud_de_receta(self):
        usuario = User.objects.create_user(
            username="usuario_sin_rol",
            password="Usuario-test-123",
            is_active=True,
        )
        self.client.force_login(usuario)

        respuesta = self.client.get(reverse("farmacia:solicitar_receta"))

        self.assertEqual(respuesta.status_code, 403)

    def test_usuario_no_farmaceutico_no_puede_revisar_solicitudes(self):
        cliente = User.objects.create_user(
            username="cliente_sin_revision",
            password="Cliente-test-123",
            is_active=True,
        )
        cliente.groups.add(Group.objects.get_or_create(name=GRUPO_CLIENTE)[0])
        self.client.force_login(cliente)

        respuesta = self.client.get(reverse("farmacia:revisar_recetas"))

        self.assertEqual(respuesta.status_code, 403)

    def test_quimico_puede_rechazar_solicitud_y_no_revisarla_dos_veces(self):
        respuesta = self.client.post(
            reverse("farmacia:solicitar_receta"),
            self.datos_solicitud_receta(),
        )
        self.assertEqual(respuesta.status_code, 302)
        solicitud = SolicitudRevisionReceta.objects.get()
        self.addCleanup(lambda: solicitud.foto_receta.delete(save=False))

        quimico = User.objects.create_user(
            username="quimico_rechazo",
            password="Revision-test-123",
            is_active=True,
        )
        quimico.groups.add(Group.objects.get_or_create(name=GRUPO_FARMACEUTICO)[0])
        self.client.force_login(quimico)

        respuesta = self.client.post(
            reverse("farmacia:resolver_receta", args=(solicitud.pk,)),
            {"decision": "rechazar", "respuesta": "La foto está borrosa."},
            follow=True,
        )

        self.assertContains(respuesta, "quedó rechazada")
        solicitud.refresh_from_db()
        self.assertEqual(solicitud.estado, SolicitudRevisionReceta.Estado.RECHAZADA)
        self.assertEqual(solicitud.respuesta, "La foto está borrosa.")

        respuesta = self.client.post(
            reverse("farmacia:resolver_receta", args=(solicitud.pk,)),
            {"decision": "aprobar"},
            follow=True,
        )

        self.assertContains(respuesta, "Esta solicitud ya fue revisada.")
        solicitud.refresh_from_db()
        self.assertEqual(solicitud.estado, SolicitudRevisionReceta.Estado.RECHAZADA)

    def test_formulario_rechaza_archivo_que_no_es_imagen(self):
        datos = self.datos_solicitud_receta()
        datos["foto_receta"] = SimpleUploadedFile(
            "receta.png",
            b"esto no es una imagen",
            content_type="image/png",
        )

        respuesta = self.client.post(
            reverse("farmacia:solicitar_receta"),
            datos,
        )

        self.assertEqual(respuesta.status_code, 400)
        self.assertTrue(respuesta.context["form"].errors["foto_receta"])
        self.assertFalse(SolicitudRevisionReceta.objects.exists())

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
            fecha_vencimiento=date.today() + timedelta(days=90),
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

        revision_url = reverse(
            "admin:farmacia_solicitudcuenta_revisar",
            args=(solicitud.pk,),
        )
        pagina_revision = self.client.get(revision_url)
        self.assertEqual(pagina_revision.status_code, 200)
        self.assertContains(pagina_revision, "farmacia/admin.css?v=3")
        self.assertContains(pagina_revision, "Aprobar y activar cuenta")

        respuesta = self.client.post(
            reverse(
                "admin:farmacia_solicitudcuenta_resolver",
                args=(solicitud.pk,),
            ),
            {
                "decision": "aprobar",
            },
        )

        self.assertEqual(respuesta.status_code, 302)
        solicitud.refresh_from_db()
        self.assertEqual(solicitud.estado, SolicitudCuenta.Estado.APROBADA)
        self.assertTrue(solicitud.usuario.is_active)

    def test_admin_muestra_enlace_directo_para_revisar_quimico(self):
        self.client.post(reverse("registro"), self.datos_registro)
        solicitud = SolicitudCuenta.objects.get(usuario__username="nuevo_usuario")
        self.client.force_login(self.admin)

        respuesta = self.client.get(
            reverse("admin:farmacia_solicitudcuenta_changelist")
        )

        self.assertContains(respuesta, "Revisar solicitud")
        self.assertContains(
            respuesta,
            reverse(
                "admin:farmacia_solicitudcuenta_revisar",
                args=(solicitud.pk,),
            ),
        )

    def test_personal_sin_privilegios_no_puede_abrir_revision_directa(self):
        self.client.post(reverse("registro"), self.datos_registro)
        solicitud = SolicitudCuenta.objects.get(usuario__username="nuevo_usuario")
        usuario_staff = User.objects.create_user(
            username="staff_revision",
            email="staff-revision@example.com",
            password="Staff-password-123",
            is_staff=True,
        )
        self.client.force_login(usuario_staff)

        respuesta = self.client.get(
            reverse(
                "admin:farmacia_solicitudcuenta_revisar",
                args=(solicitud.pk,),
            )
        )

        self.assertEqual(respuesta.status_code, 403)

    def test_resolucion_de_solicitud_solo_acepta_post(self):
        self.client.post(reverse("registro"), self.datos_registro)
        solicitud = SolicitudCuenta.objects.get(usuario__username="nuevo_usuario")
        self.client.force_login(self.admin)

        respuesta = self.client.get(
            reverse(
                "admin:farmacia_solicitudcuenta_resolver",
                args=(solicitud.pk,),
            )
        )

        self.assertEqual(respuesta.status_code, 405)
        solicitud.refresh_from_db()
        self.assertEqual(solicitud.estado, SolicitudCuenta.Estado.PENDIENTE)

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
        self.assertContains(respuesta, "farmacia/admin.css?v=3")
        self.assertContains(respuesta, "Resumen de inventario")
        self.assertContains(respuesta, "Abrir Caja")
        self.assertContains(respuesta, reverse("farmacia:caja"))
        self.assertContains(respuesta, "Registrar o revisar ingresos")
        self.assertContains(respuesta, "Solicitudes de qu")
        self.assertNotContains(respuesta, "pie-sitio")

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
        ingreso.refresh_from_db()
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
        self.assertEqual(ingreso.mermas.count(), 1)
    def test_lotes_vencidos_no_aparecen_como_stock_disponible_en_admin(self):
        medicamento = Medicamento.objects.create(
            codigo="MED-VENCIDO-001",
            nombre="Producto con lote vencido",
            stock_minimo=2,
        )
        IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-VENCIDO-01",
            cantidad=10,
            fecha_vencimiento=date.today() - timedelta(days=1),
            registrado_por=self.admin,
        )
        IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-VIGENTE-01",
            cantidad=3,
            fecha_vencimiento=date.today() + timedelta(days=30),
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
        IngresoInventario.objects.create(
            medicamento=medicamento,
            lote="LOTE-VENCIDO-02",
            cantidad=10,
            fecha_vencimiento=date.today() - timedelta(days=1),
            registrado_por=self.admin,
        )
        self.client.force_login(self.admin)

        inicio = self.client.get(reverse("admin:index"))

        self.assertEqual(inicio.context["resumen_farmacia"]["stock_bajo"], 1)
