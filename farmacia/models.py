import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


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
        ]

    def __str__(self):
        return f"Pago simulado de venta #{self.venta_id}: {self.get_estado_display()}"


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
        limit_choices_to={"groups__name": "Jefe de Farmacia"},
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
        if self.jefe_farmacia_id and not self.jefe_farmacia.groups.filter(
            name="Jefe de Farmacia"
        ).exists():
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
