import re
import uuid

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from farmacia.storage import PrivatePrescriptionStorage


GRUPO_FARMACEUTICO = "Auditor_QF"
GRUPO_CLIENTE = "Cliente"
GRUPO_JEFE_FARMACIA = "Jefe de Farmacia"


def validar_rut_chileno(value):
    rut = re.sub(r"[.\-\s]", "", value).upper()
    if not re.fullmatch(r"\d{1,8}[0-9K]", rut):
        raise ValidationError("Ingrese un RUT chileno válido.")

    cuerpo, digito_verificador = rut[:-1], rut[-1]
    suma = sum(
        int(digito) * factor
        for digito, factor in zip(reversed(cuerpo), (2, 3, 4, 5, 6, 7) * 2)
    )
    resultado = 11 - (suma % 11)
    esperado = "0" if resultado == 11 else "K" if resultado == 10 else str(resultado)
    if digito_verificador != esperado:
        raise ValidationError("El dígito verificador del RUT no es válido.")


class VentaRecetaRetenida(models.Model):
    class Estado(models.TextChoices):
        PENDIENTE_REVISION = "pendiente_revision", "Pendiente de revisión"
        APROBADA = "aprobada", "Aprobada"
        RECHAZADA = "rechazada", "Rechazada"
        BLOQUEADA = "bloqueada", "Bloqueada"

    cliente_rut = models.CharField(
        "RUT del cliente",
        max_length=12,
        validators=[validar_rut_chileno],
    )
    medicamento = models.CharField("Medicamento", max_length=200)
    cantidad = models.PositiveSmallIntegerField(
        "Cantidad",
        validators=[MinValueValidator(1)],
    )
    foto_receta = models.ImageField(
        "Foto de la receta",
        upload_to="recetas_retenidas/%Y/%m/",
    )
    medico_prescriptor = models.CharField("Médico prescriptor", max_length=200)
    auditor_qf = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="ventas_recetas_retenidas_auditadas",
        limit_choices_to={"groups__name": "Auditor_QF"},
        verbose_name="Auditor QF",
    )
    estado = models.CharField(
        "Estado",
        max_length=24,
        choices=Estado.choices,
        default=Estado.PENDIENTE_REVISION,
    )
    fecha_registro = models.DateTimeField("Fecha de registro", auto_now_add=True)
    eliminado_en = models.DateTimeField(
        "Eliminado en",
        null=True,
        blank=True,
        help_text="Se usa para el borrado lógico; el registro no se elimina físicamente.",
    )

    class Meta:
        verbose_name = "venta de receta retenida"
        verbose_name_plural = "ventas de recetas retenidas"
        ordering = ("-fecha_registro",)
        constraints = [
            models.CheckConstraint(
                check=Q(cantidad__gt=0),
                name="venta_receta_cantidad_positiva",
            ),
        ]

    def clean(self):
        super().clean()
        if self.auditor_qf_id and not self.auditor_qf.groups.filter(
            name="Auditor_QF"
        ).exists():
            raise ValidationError(
                {"auditor_qf": "El usuario debe pertenecer al grupo Auditor_QF."}
            )

    def eliminar_logicamente(self):
        if self.eliminado_en is None:
            self.eliminado_en = timezone.now()
            self.save(update_fields=("eliminado_en",))

    def __str__(self):
        return f"Venta #{self.pk} - {self.cliente_rut}"


class SolicitudRevisionReceta(models.Model):
    class Estado(models.TextChoices):
        PENDIENTE = "pendiente", "Pendiente de revisión"
        APROBADA = "aprobada", "Aprobada"
        RECHAZADA = "rechazada", "Rechazada"

    cliente = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="solicitudes_revision_receta",
        null=True,
        blank=True,
        verbose_name="Cuenta del cliente",
    )
    cliente_nombre = models.CharField("Nombre del cliente", max_length=150)
    cliente_rut = models.CharField(
        "RUT del cliente",
        max_length=12,
        validators=[validar_rut_chileno],
    )
    medicamento = models.CharField("Medicamento solicitado", max_length=200)
    cantidad = models.PositiveSmallIntegerField(
        "Cantidad solicitada",
        validators=[MinValueValidator(1)],
    )
    medico_prescriptor = models.CharField("Médico prescriptor", max_length=200)
    foto_receta = models.ImageField(
        "Foto de la receta",
        upload_to="solicitudes_receta/%Y/%m/",
        storage=PrivatePrescriptionStorage(),
    )
    estado = models.CharField(
        "Estado",
        max_length=10,
        choices=Estado.choices,
        default=Estado.PENDIENTE,
    )
    revisada_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="solicitudes_receta_revisadas",
        null=True,
        blank=True,
        verbose_name="Químico farmacéutico que revisa",
    )
    respuesta = models.TextField("Observación de la revisión", blank=True)
    fecha_solicitud = models.DateTimeField("Fecha de solicitud", auto_now_add=True)
    fecha_revision = models.DateTimeField(
        "Fecha de revisión",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "solicitud de revisión de receta"
        verbose_name_plural = "solicitudes de revisión de recetas"
        ordering = ("-fecha_solicitud",)
        constraints = [
            models.CheckConstraint(
                check=Q(cantidad__gt=0),
                name="solicitud_receta_cantidad_positiva",
            ),
            models.CheckConstraint(
                check=(
                    Q(
                        estado="pendiente",
                        revisada_por__isnull=True,
                        fecha_revision__isnull=True,
                    )
                    | Q(
                        estado__in=("aprobada", "rechazada"),
                        revisada_por__isnull=False,
                        fecha_revision__isnull=False,
                    )
                ),
                name="solicitud_receta_revision_consistente",
            ),
        ]

    def resolver(self, revisor, aprobada, respuesta=""):
        if not (
            revisor.is_superuser
            or revisor.groups.filter(name=GRUPO_FARMACEUTICO).exists()
        ):
            raise PermissionDenied(
                "Solo un químico farmacéutico puede revisar solicitudes de recetas."
            )

        with transaction.atomic():
            solicitud = type(self).objects.select_for_update().get(pk=self.pk)
            if solicitud.estado != self.Estado.PENDIENTE:
                raise ValidationError("Esta solicitud ya fue revisada.")
            solicitud.estado = (
                self.Estado.APROBADA if aprobada else self.Estado.RECHAZADA
            )
            solicitud.revisada_por = revisor
            solicitud.respuesta = respuesta.strip()
            solicitud.fecha_revision = timezone.now()
            solicitud.save(
                update_fields=(
                    "estado",
                    "revisada_por",
                    "respuesta",
                    "fecha_revision",
                )
            )

        self.estado = solicitud.estado
        self.revisada_por = solicitud.revisada_por
        self.respuesta = solicitud.respuesta
        self.fecha_revision = solicitud.fecha_revision

    def __str__(self):
        return f"Solicitud de receta #{self.pk} - {self.get_estado_display()}"


class SimulacionPago(models.Model):
    class MedioPago(models.TextChoices):
        EFECTIVO = "efectivo", "Efectivo"
        DEBITO = "debito", "Débito"
        CREDITO = "credito", "Crédito"
        TRANSFERENCIA = "transferencia", "Transferencia"

    class Estado(models.TextChoices):
        PENDIENTE = "pendiente", "Pendiente"
        APROBADO = "aprobado", "Aprobado"
        RECHAZADO = "rechazado", "Rechazado"

    venta = models.ForeignKey(
        VentaRecetaRetenida,
        on_delete=models.PROTECT,
        related_name="simulaciones_pago",
        verbose_name="Venta",
        null=True,
        blank=True,
    )
    venta_caja = models.ForeignKey(
        "VentaCaja",
        on_delete=models.PROTECT,
        related_name="simulaciones_pago",
        verbose_name="Venta de caja",
        null=True,
        blank=True,
    )
    monto = models.DecimalField(
        "Monto simulado",
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(0.01)],
    )
    medio_pago = models.CharField(
        "Medio de pago",
        max_length=16,
        choices=MedioPago.choices,
    )
    estado = models.CharField(
        "Resultado de la simulación",
        max_length=10,
        choices=Estado.choices,
        default=Estado.PENDIENTE,
    )
    referencia_simulada = models.CharField(
        "Referencia simulada",
        max_length=100,
        blank=True,
    )
    fecha_simulacion = models.DateTimeField("Fecha de simulación", auto_now_add=True)

    class Meta:
        verbose_name = "simulación de pago"
        verbose_name_plural = "simulaciones de pago"
        ordering = ("-fecha_simulacion",)
        constraints = [
            models.CheckConstraint(
                check=Q(monto__gt=0),
                name="simulacion_pago_monto_positivo",
            ),
            models.CheckConstraint(
                check=(
                    Q(venta__isnull=False, venta_caja__isnull=True)
                    | Q(venta__isnull=True, venta_caja__isnull=False)
                ),
                name="simulacion_pago_una_venta",
            ),
        ]

    def __str__(self):
        venta = self.venta_caja or self.venta
        return f"Pago simulado de venta #{venta.pk}: {self.get_estado_display()}"


class VentaCaja(models.Model):
    class Estado(models.TextChoices):
        COMPLETADA = "completada", "Completada"

    idempotency_key = models.UUIDField(
        "Identificador de operación",
        default=uuid.uuid4,
        unique=True,
        editable=False,
    )
    cajero = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="ventas_caja_realizadas",
        verbose_name="Cajero",
    )
    total = models.DecimalField(
        "Total",
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(0.01)],
    )
    estado = models.CharField(
        "Estado",
        max_length=12,
        choices=Estado.choices,
        default=Estado.COMPLETADA,
    )
    fecha_venta = models.DateTimeField("Fecha de venta", auto_now_add=True)

    class Meta:
        verbose_name = "venta de caja"
        verbose_name_plural = "ventas de caja"
        ordering = ("-fecha_venta",)
        constraints = [
            models.CheckConstraint(
                check=Q(total__gt=0),
                name="venta_caja_total_positivo",
            ),
        ]

    def __str__(self):
        return f"Venta #{self.pk} — {self.get_estado_display()}"


class DetalleVentaCaja(models.Model):
    venta = models.ForeignKey(
        VentaCaja,
        on_delete=models.PROTECT,
        related_name="detalles",
        verbose_name="Venta",
    )
    ingreso = models.ForeignKey(
        "IngresoInventario",
        on_delete=models.PROTECT,
        related_name="detalles_venta",
        verbose_name="Lote descontado",
    )
    cantidad = models.PositiveIntegerField(
        "Cantidad vendida",
        validators=[MinValueValidator(1)],
    )
    precio_unitario = models.DecimalField(
        "Precio unitario al vender",
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(0)],
    )

    class Meta:
        verbose_name = "detalle de venta"
        verbose_name_plural = "detalles de venta"
        constraints = [
            models.CheckConstraint(
                check=Q(cantidad__gt=0),
                name="detalle_venta_cantidad_positiva",
            ),
            models.CheckConstraint(
                check=Q(precio_unitario__gte=0),
                name="detalle_venta_precio_no_negativo",
            ),
        ]

    @property
    def subtotal(self):
        return self.cantidad * self.precio_unitario

    def __str__(self):
        return f"{self.ingreso.medicamento} × {self.cantidad}"


class AutorizacionExcepcional(models.Model):
    class Tipo(models.TextChoices):
        RECETA_ILEGIBLE = "receta_ilegible", "Receta poco legible"
        TOPE_MEDICAMENTOS = "tope_medicamentos", "Excepción al tope de medicamentos"
        OTRA = "otra", "Otra excepción"

    class Estado(models.TextChoices):
        PENDIENTE = "pendiente", "Pendiente"
        APROBADA = "aprobada", "Aprobada"
        RECHAZADA = "rechazada", "Rechazada"

    venta = models.ForeignKey(
        VentaRecetaRetenida,
        on_delete=models.PROTECT,
        related_name="autorizaciones_excepcionales",
        verbose_name="Venta",
    )
    tipo = models.CharField("Tipo de excepción", max_length=24, choices=Tipo.choices)
    motivo = models.TextField("Motivo de la solicitud")
    solicitada_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="solicitudes_excepcionales_recetas",
        limit_choices_to={"groups__name": "Auditor_QF"},
        verbose_name="Solicitada por (Auditor QF)",
    )
    estado = models.CharField(
        "Estado",
        max_length=10,
        choices=Estado.choices,
        default=Estado.PENDIENTE,
    )
    jefe_farmacia = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="autorizaciones_excepcionales_recetas",
        limit_choices_to=(
            Q(groups__name=GRUPO_JEFE_FARMACIA) | Q(is_superuser=True)
        ),
        null=True,
        blank=True,
        verbose_name="Jefe de Farmacia que resuelve",
    )
    respuesta_jefe = models.TextField("Respuesta del Jefe de Farmacia", blank=True)
    fecha_solicitud = models.DateTimeField("Fecha de solicitud", auto_now_add=True)
    fecha_resolucion = models.DateTimeField(
        "Fecha de resolución",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "autorización excepcional"
        verbose_name_plural = "autorizaciones excepcionales"
        ordering = ("-fecha_solicitud",)
        constraints = [
            models.CheckConstraint(
                check=(
                    Q(
                        estado="pendiente",
                        jefe_farmacia__isnull=True,
                        fecha_resolucion__isnull=True,
                    )
                    | Q(
                        estado__in=("aprobada", "rechazada"),
                        jefe_farmacia__isnull=False,
                        fecha_resolucion__isnull=False,
                    )
                ),
                name="autorizacion_resolucion_consistente",
            ),
        ]

    def clean(self):
        super().clean()
        errores = {}
        if self.solicitada_por_id and not self.solicitada_por.groups.filter(
            name="Auditor_QF"
        ).exists():
            errores["solicitada_por"] = (
                "El usuario solicitante debe pertenecer al grupo Auditor_QF."
            )
        if self.jefe_farmacia_id and not (
            self.jefe_farmacia.is_superuser
            or self.jefe_farmacia.groups.filter(name=GRUPO_JEFE_FARMACIA).exists()
        ):
            errores["jefe_farmacia"] = (
                "El usuario debe pertenecer al grupo Jefe de Farmacia."
            )
        if self.estado != self.Estado.PENDIENTE and not self.jefe_farmacia_id:
            errores["jefe_farmacia"] = (
                "La resolución requiere un usuario del grupo Jefe de Farmacia."
            )
        if self.estado == self.Estado.PENDIENTE and self.fecha_resolucion:
            errores["fecha_resolucion"] = (
                "Una solicitud pendiente no puede tener fecha de resolución."
            )
        if errores:
            raise ValidationError(errores)

    def __str__(self):
        return f"Excepción #{self.pk} - venta #{self.venta_id} ({self.get_estado_display()})"


class SolicitudCuenta(models.Model):
    class RolSolicitado(models.TextChoices):
        FARMACEUTICO = "farmaceutico", "Químico en farmacia"
        CLIENTE = "cliente", "Cliente"

    class Estado(models.TextChoices):
        PENDIENTE = "pendiente", "Pendiente"
        APROBADA = "aprobada", "Aprobada"
        RECHAZADA = "rechazada", "Rechazada"

    usuario = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="solicitud_cuenta",
        verbose_name="Usuario",
    )
    rol_solicitado = models.CharField(
        "Rol solicitado",
        max_length=20,
        choices=RolSolicitado.choices,
    )
    estado = models.CharField(
        "Estado",
        max_length=10,
        choices=Estado.choices,
        default=Estado.PENDIENTE,
    )
    fecha_solicitud = models.DateTimeField("Fecha de solicitud", auto_now_add=True)
    revisada_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="solicitudes_cuenta_revisadas",
        verbose_name="Revisada por",
        null=True,
        blank=True,
    )
    fecha_revision = models.DateTimeField(
        "Fecha de revisión",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "solicitud de cuenta"
        verbose_name_plural = "solicitudes de cuentas"
        ordering = ("fecha_solicitud",)

    def aprobar(self, revisor):
        if not revisor.is_superuser:
            raise PermissionDenied("Solo el administrador principal puede aprobar cuentas.")
        if self.estado != self.Estado.PENDIENTE:
            raise ValidationError("Solo se pueden aprobar solicitudes pendientes.")

        grupos_por_rol = {
            self.RolSolicitado.FARMACEUTICO: GRUPO_FARMACEUTICO,
            self.RolSolicitado.CLIENTE: GRUPO_CLIENTE,
        }
        nombre_grupo = grupos_por_rol.get(self.rol_solicitado)
        if nombre_grupo is None:
            raise ValidationError("El rol solicitado no es válido.")

        with transaction.atomic():
            grupo, _ = Group.objects.get_or_create(name=nombre_grupo)
            self.usuario.groups.add(grupo)
            self.usuario.is_active = True
            self.usuario.save(update_fields=("is_active",))
            self.estado = self.Estado.APROBADA
            self.revisada_por = revisor
            self.fecha_revision = timezone.now()
            self.save(
                update_fields=("estado", "revisada_por", "fecha_revision")
            )

    def rechazar(self, revisor):
        if not revisor.is_superuser:
            raise PermissionDenied("Solo el administrador principal puede rechazar cuentas.")
        if self.estado != self.Estado.PENDIENTE:
            raise ValidationError("Solo se pueden rechazar solicitudes pendientes.")

        with transaction.atomic():
            self.usuario.is_active = False
            self.usuario.save(update_fields=("is_active",))
            self.estado = self.Estado.RECHAZADA
            self.revisada_por = revisor
            self.fecha_revision = timezone.now()
            self.save(update_fields=("estado", "revisada_por", "fecha_revision"))

    def __str__(self):
        return f"{self.usuario} — {self.get_rol_solicitado_display()} ({self.get_estado_display()})"


class Medicamento(models.Model):
    codigo = models.CharField("Código de producto", max_length=50, unique=True)
    nombre = models.CharField("Nombre del medicamento", max_length=200)
    descripcion = models.TextField("Descripción", blank=True)
    precio = models.DecimalField(
        "Precio de venta",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    requiere_receta = models.BooleanField(
        "Requiere receta",
        default=False,
        help_text="Activa esta opción si la dispensación requiere receta médica.",
    )
    stock_minimo = models.PositiveIntegerField(
        "Stock mínimo",
        default=5,
        help_text="Se mostrará una alerta cuando el stock disponible llegue a este valor.",
    )
    activo = models.BooleanField("Activo en el catálogo", default=True)

    class Meta:
        verbose_name = "medicamento"
        verbose_name_plural = "medicamentos"
        ordering = ("nombre",)
        constraints = [
            models.CheckConstraint(
                check=Q(stock_minimo__gte=0),
                name="medicamento_stock_minimo_no_negativo",
            ),
            models.CheckConstraint(
                check=Q(precio__isnull=True) | Q(precio__gte=0),
                name="medicamento_precio_no_negativo",
            ),
        ]

    @property
    def stock_actual(self):
        lotes_vigentes = self.ingresos.filter(
            fecha_vencimiento__gte=timezone.localdate()
        )
        return lotes_vigentes.aggregate(total=Sum("stock_disponible"))["total"] or 0

    def __str__(self):
        return f"{self.nombre} ({self.codigo})"


class IngresoInventario(models.Model):
    medicamento = models.ForeignKey(
        Medicamento,
        on_delete=models.PROTECT,
        related_name="ingresos",
        verbose_name="Medicamento",
    )
    lote = models.CharField("Número de lote", max_length=100)
    cantidad = models.PositiveIntegerField(
        "Cantidad recibida",
        validators=[MinValueValidator(1)],
    )
    stock_disponible = models.PositiveIntegerField(
        "Stock disponible",
        default=0,
        editable=False,
    )
    fecha_vencimiento = models.DateField("Fecha de vencimiento")
    proveedor = models.CharField("Proveedor", max_length=200, blank=True)
    fecha_ingreso = models.DateTimeField("Fecha de ingreso", auto_now_add=True)
    registrado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="ingresos_inventario_registrados",
        verbose_name="Registrado por",
    )

    class Meta:
        verbose_name = "ingreso de inventario"
        verbose_name_plural = "ingresos de inventario"
        ordering = ("fecha_vencimiento", "medicamento__nombre")
        constraints = [
            models.CheckConstraint(
                check=Q(cantidad__gt=0),
                name="ingreso_inventario_cantidad_positiva",
            ),
            models.CheckConstraint(
                check=Q(stock_disponible__gte=0),
                name="ingreso_stock_no_negativo",
            ),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding:
            self.stock_disponible = self.cantidad
        else:
            original = type(self).objects.only("cantidad").get(pk=self.pk)
            if self.cantidad != original.cantidad:
                raise ValidationError(
                    {"cantidad": "No se puede modificar la cantidad de un ingreso guardado."}
                )
        super().save(*args, **kwargs)

    @property
    def mermas_totales(self):
        return self.mermas.aggregate(total=Sum("cantidad"))["total"] or 0

    def __str__(self):
        return f"{self.medicamento} — Lote {self.lote}"


class Merma(models.Model):
    class Motivo(models.TextChoices):
        VENCIDO = "vencido", "Producto vencido"
        DANADO = "danado", "Producto dañado"
        PERDIDA = "perdida", "Pérdida o extravío"
        OTRO = "otro", "Otro"

    ingreso = models.ForeignKey(
        IngresoInventario,
        on_delete=models.PROTECT,
        related_name="mermas",
        verbose_name="Lote de inventario",
    )
    cantidad = models.PositiveIntegerField(
        "Cantidad mermada",
        validators=[MinValueValidator(1)],
    )
    motivo = models.CharField("Motivo", max_length=12, choices=Motivo.choices)
    observacion = models.TextField("Observación", blank=True)
    fecha_registro = models.DateTimeField("Fecha de registro", auto_now_add=True)
    registrado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="mermas_inventario_registradas",
        verbose_name="Registrado por",
    )

    class Meta:
        verbose_name = "merma"
        verbose_name_plural = "mermas"
        ordering = ("-fecha_registro",)
        constraints = [
            models.CheckConstraint(
                check=Q(cantidad__gt=0),
                name="merma_cantidad_positiva",
            ),
        ]

    def clean(self):
        super().clean()
        if not self.ingreso_id or not self.cantidad:
            return

        if self.cantidad > self.ingreso.stock_disponible:
            raise ValidationError(
                {
                    "cantidad": (
                        "La cantidad supera el stock disponible del lote "
                        f"({self.ingreso.stock_disponible} unidades)."
                    )
                }
            )

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                raise ValidationError("Las mermas guardadas no se pueden modificar.")
            ingreso_original = self.ingreso
            self.ingreso = IngresoInventario.objects.select_for_update().get(
                pk=self.ingreso_id
            )
            self.full_clean()
            actualizado = IngresoInventario.objects.filter(
                pk=self.ingreso_id,
                stock_disponible__gte=self.cantidad,
            ).update(stock_disponible=F("stock_disponible") - self.cantidad)
            if not actualizado:
                raise ValidationError(
                    {"cantidad": "El stock disponible cambió; revisa la cantidad."}
                )
            ingreso_original.stock_disponible = (
                self.ingreso.stock_disponible - self.cantidad
            )
            super().save(*args, **kwargs)

    def __str__(self):
        return f"Merma de {self.cantidad} unidades — {self.ingreso}"
