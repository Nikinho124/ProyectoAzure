import re
import unicodedata
import uuid
from datetime import date
from os.path import splitext
from warnings import catch_warnings, simplefilter

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import IntegrityError, models, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from farmacia.storage import PrivatePrescriptionStorage


GRUPO_FARMACEUTICO = "Auditor_QF"
GRUPO_JEFE_FARMACIA = "Jefe de Farmacia"
GRUPOS_JEFE_FARMACIA = (GRUPO_JEFE_FARMACIA, "Jefe_QF")
MAX_UNIDADES_POR_RECETA = 6
MAX_POSITIVE_INTEGER = 2_147_483_647
MAX_POSITIVE_SMALL_INTEGER = 32_767
MAX_MOTIVO_AJUSTE_LENGTH = 2000
MAX_RECETA_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_RECETA_IMAGE_PIXELS = settings.RECETA_MAX_IMAGE_PIXELS
EXTENSIONES_RECETA_PERMITIDAS = {".jpg", ".jpeg", ".png"}
Image.MAX_IMAGE_PIXELS = MAX_RECETA_IMAGE_PIXELS


def _ruta_imagen_receta(directorio, filename):
    extension = splitext(filename)[1].lower()
    if extension in {".jpg", ".jpeg"}:
        extension = ".jpg"
    elif extension != ".png":
        raise ValidationError(
            "La imagen debe ser JPG o PNG para poder guardarse."
        )
    fecha = timezone.localdate()
    return (
        f"{directorio}/{fecha:%Y/%m}/"
        f"{uuid.uuid4().hex}{extension}"
    )


def receta_retenida_upload_to(instance, filename):
    return _ruta_imagen_receta("recetas_retenidas", filename)


def consulta_receta_upload_to(instance, filename):
    return _ruta_imagen_receta("consultas_jefe", filename)


def normalizar_texto_seguro(valor):
    if valor is None:
        return ""
    if not isinstance(valor, str):
        return valor
    valor = valor.replace("\x00", "")
    valor = re.sub(r"%00", "", valor, flags=re.IGNORECASE)
    valor = unicodedata.normalize("NFKC", valor)
    return "".join(
        caracter
        for caracter in valor
        if caracter in "\t\n\r"
        or unicodedata.category(caracter) not in {"Cc", "Cf", "Cs"}
    )


class SanitizaTextoMixin:
    campos_texto_sanitizables = ()

    def clean_fields(self, exclude=None):
        excluidos = set(exclude or ())
        for campo in self.campos_texto_sanitizables:
            if campo not in excluidos:
                setattr(
                    self,
                    campo,
                    normalizar_texto_seguro(getattr(self, campo)),
                )
        super().clean_fields(exclude=exclude)


def validar_archivo_receta(archivo):
    if archivo.size > MAX_RECETA_UPLOAD_BYTES:
        raise ValidationError("La imagen de la receta no puede superar los 5 MB.")
    extension = splitext(archivo.name)[1].lower()
    if extension not in EXTENSIONES_RECETA_PERMITIDAS:
        raise ValidationError(
            "La imagen de la receta debe tener extensión .jpg, .jpeg o .png."
        )
    try:
        archivo.seek(0)
        with catch_warnings():
            simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(archivo) as imagen:
                formato = imagen.format
                if formato not in {"JPEG", "PNG"}:
                    raise ValidationError("El archivo debe ser una imagen JPG o PNG.")
                if (
                    extension in {".jpg", ".jpeg"} and formato != "JPEG"
                ) or (extension == ".png" and formato != "PNG"):
                    raise ValidationError(
                        "La extensión del archivo no coincide con su formato real."
                    )
                if imagen.width * imagen.height > MAX_RECETA_IMAGE_PIXELS:
                    raise ValidationError(
                        "La imagen excede la resolución máxima permitida de "
                        "5 megapíxeles."
                    )
                imagen.verify()
    except ValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ValidationError(
            "La imagen excede la resolución máxima permitida de 5 megapíxeles."
        ) from error
    except (
        OSError,
        TypeError,
        UnidentifiedImageError,
        ValueError,
    ) as error:
        raise ValidationError("El archivo debe ser una imagen JPG o PNG válida.") from error
    finally:
        archivo.seek(0)


def validar_rut_chileno(value):
    value = normalizar_texto_seguro(value)
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


class VentaRecetaRetenida(SanitizaTextoMixin, models.Model):
    campos_texto_sanitizables = (
        "cliente_rut",
        "medico_prescriptor",
        "observacion_resolucion",
    )

    class Estado(models.TextChoices):
        PENDIENTE_REVISION = "pendiente_revision", "Pendiente de revisión"
        APROBADA = "aprobada", "Aprobada"
        RECHAZADA = "rechazada", "Rechazada"
        BLOQUEADA = "bloqueada", "Bloqueada"
        DISPENSADA = "dispensada", "Dispensada"

    cliente_rut = models.CharField(
        "RUT del cliente",
        max_length=12,
        validators=[validar_rut_chileno],
    )
    medicamento = models.ForeignKey(
        "Medicamento",
        on_delete=models.PROTECT,
        related_name="recetas_retenidas",
        limit_choices_to={"activo": True, "requiere_receta": True},
        null=True,
        blank=True,
        verbose_name="Medicamento",
    )
    medicamento_legado = models.CharField(
        "Nombre histórico del medicamento",
        max_length=200,
        blank=True,
        editable=False,
    )
    cantidad = models.PositiveSmallIntegerField(
        "Cantidad",
        validators=[
            MinValueValidator(1),
            MaxValueValidator(MAX_POSITIVE_SMALL_INTEGER),
        ],
    )
    foto_receta = models.ImageField(
        "Foto de la receta",
        upload_to=receta_retenida_upload_to,
        storage=PrivatePrescriptionStorage(),
        validators=[validar_archivo_receta],
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
    jefe_farmacia = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="ventas_recetas_retenidas_resueltas",
        null=True,
        blank=True,
        verbose_name="Jefe de Farmacia que revisó",
    )
    revisado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recetas_retenidas_revisadas",
        null=True,
        blank=True,
        verbose_name="Revisado por",
    )
    venta_caja = models.ForeignKey(
        "VentaCaja",
        on_delete=models.PROTECT,
        related_name="recetas_retenidas",
        null=True,
        blank=True,
        verbose_name="Venta de caja",
    )
    observacion_resolucion = models.CharField(
        "Observación de la resolución",
        max_length=2000,
        blank=True,
    )
    fecha_registro = models.DateTimeField("Fecha de registro", auto_now_add=True)
    fecha_resolucion = models.DateTimeField(
        "Fecha de resolución",
        null=True,
        blank=True,
    )
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
        self.cliente_rut = normalizar_texto_seguro(self.cliente_rut).strip()
        self.medico_prescriptor = normalizar_texto_seguro(
            self.medico_prescriptor
        ).strip()
        self.observacion_resolucion = normalizar_texto_seguro(
            self.observacion_resolucion
        ).strip()
        if self._state.adding and not self.medicamento_id:
            raise ValidationError(
                {"medicamento": "Selecciona un medicamento del catálogo."}
            )
        if self.medicamento_id and (
            not self.medicamento.activo or not self.medicamento.requiere_receta
        ):
            raise ValidationError(
                {"medicamento": "La receta debe corresponder a un medicamento activo que la requiera."}
            )
        if self.auditor_qf_id and not self.auditor_qf.groups.filter(
            name=GRUPO_FARMACEUTICO
        ).exists():
            raise ValidationError(
                {"auditor_qf": "El usuario debe pertenecer al grupo Auditor_QF."}
            )
        if self.jefe_farmacia_id and not (
            self.jefe_farmacia.is_superuser
            or self.jefe_farmacia.groups.filter(
                name__in=GRUPOS_JEFE_FARMACIA
            ).exists()
        ):
            raise ValidationError(
                {"jefe_farmacia": "El usuario debe pertenecer al grupo Jefe de Farmacia."}
            )
        if self.revisado_por_id and not (
            self.revisado_por.is_superuser
            or self.revisado_por.groups.filter(
                name__in=GRUPOS_JEFE_FARMACIA
            ).exists()
        ):
            raise ValidationError(
                {"revisado_por": "Solo el Jefe de Farmacia puede revisar recetas."}
            )

    def save(self, *args, **kwargs):
        if self._state.adding:
            self.full_clean()
        super().save(*args, **kwargs)

    def eliminar_logicamente(self):
        if self.eliminado_en is None:
            self.eliminado_en = timezone.now()
            self.save(update_fields=("eliminado_en",))

    def aprobar(self, revisor):
        es_jefe = revisor.is_superuser or revisor.groups.filter(
            name__in=GRUPOS_JEFE_FARMACIA
        ).exists()
        if not es_jefe:
            raise PermissionDenied(
                "Solo el Jefe de Farmacia puede aprobar una receta."
            )

        with transaction.atomic():
            venta = type(self).objects.select_for_update().get(pk=self.pk)
            if venta.estado != self.Estado.PENDIENTE_REVISION:
                raise ValidationError("Esta receta ya fue revisada.")
            if venta.cantidad > MAX_UNIDADES_POR_RECETA and not (
                venta.autorizaciones_excepcionales.filter(
                    tipo=AutorizacionExcepcional.Tipo.TOPE_MEDICAMENTOS,
                    estado=AutorizacionExcepcional.Estado.APROBADA,
                ).exists()
            ):
                raise ValidationError(
                    "La cantidad supera el máximo de 6 unidades. "
                    "Se requiere aprobar primero la excepción."
                )

            fecha = timezone.now()
            type(self).objects.filter(
                pk=venta.pk,
                estado=self.Estado.PENDIENTE_REVISION,
            ).update(
                estado=self.Estado.APROBADA,
                jefe_farmacia=revisor if es_jefe else None,
                revisado_por=revisor,
                fecha_resolucion=fecha,
            )
            self.estado = self.Estado.APROBADA
            self.jefe_farmacia = revisor if es_jefe else None
            self.revisado_por = revisor
            self.fecha_resolucion = fecha

    def rechazar(self, jefe, observacion):
        if not (
            jefe.is_superuser
            or jefe.groups.filter(name__in=GRUPOS_JEFE_FARMACIA).exists()
        ):
            raise PermissionDenied(
                "Solo el Jefe de Farmacia puede rechazar una receta."
            )
        observacion = normalizar_texto_seguro(observacion or "").strip()
        if not observacion:
            raise ValidationError(
                {"observacion_resolucion": "Indica el motivo del rechazo."}
            )
        if len(observacion) > 2000:
            raise ValidationError(
                {
                    "observacion_resolucion": (
                        "La observación no puede superar los 2000 caracteres."
                    )
                }
            )

        with transaction.atomic():
            venta = type(self).objects.select_for_update().get(pk=self.pk)
            if venta.estado != self.Estado.PENDIENTE_REVISION:
                raise ValidationError("Esta receta ya fue revisada.")

            fecha = timezone.now()
            type(self).objects.filter(
                pk=venta.pk,
                estado=self.Estado.PENDIENTE_REVISION,
            ).update(
                estado=self.Estado.RECHAZADA,
                jefe_farmacia=jefe,
                revisado_por=jefe,
                observacion_resolucion=observacion,
                fecha_resolucion=fecha,
            )
            venta.autorizaciones_excepcionales.filter(
                estado=AutorizacionExcepcional.Estado.PENDIENTE
            ).update(
                estado=AutorizacionExcepcional.Estado.RECHAZADA,
                jefe_farmacia=jefe,
                respuesta_jefe=observacion,
                fecha_resolucion=fecha,
            )
            self.estado = self.Estado.RECHAZADA
            self.jefe_farmacia = jefe
            self.revisado_por = jefe
            self.observacion_resolucion = observacion
            self.fecha_resolucion = fecha

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
    receta = models.ForeignKey(
        VentaRecetaRetenida,
        on_delete=models.PROTECT,
        related_name="detalles_dispensacion",
        null=True,
        blank=True,
        verbose_name="Receta dispensada",
    )
    cantidad = models.PositiveIntegerField(
        "Cantidad vendida",
        validators=[
            MinValueValidator(1),
            MaxValueValidator(MAX_POSITIVE_INTEGER),
        ],
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
            models.UniqueConstraint(
                fields=("receta",),
                name="detalle_venta_receta_unica",
            ),
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


class AutorizacionExcepcional(SanitizaTextoMixin, models.Model):
    campos_texto_sanitizables = ("motivo", "respuesta_jefe")

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
    motivo = models.TextField("Motivo de la solicitud", max_length=2000)
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
            Q(groups__name__in=GRUPOS_JEFE_FARMACIA) | Q(is_superuser=True)
        ),
        null=True,
        blank=True,
        verbose_name="Jefe de Farmacia que resuelve",
    )
    respuesta_jefe = models.TextField(
        "Respuesta del Jefe de Farmacia",
        max_length=2000,
        blank=True,
    )
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
        self.motivo = normalizar_texto_seguro(self.motivo).strip()
        self.respuesta_jefe = normalizar_texto_seguro(
            self.respuesta_jefe
        ).strip()
        self.motivo = (self.motivo or "").strip()
        if not self.motivo:
            errores["motivo"] = "Describe por qué se solicita la excepción."
        if self.solicitada_por_id and not self.solicitada_por.groups.filter(
            name="Auditor_QF"
        ).exists():
            errores["solicitada_por"] = (
                "El usuario solicitante debe pertenecer al grupo Auditor_QF."
            )
        if self.jefe_farmacia_id and not (
            self.jefe_farmacia.is_superuser
            or self.jefe_farmacia.groups.filter(
                name__in=GRUPOS_JEFE_FARMACIA
            ).exists()
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

    def resolver(self, jefe, aprobado, respuesta=""):
        if not (
            jefe.is_superuser
            or jefe.groups.filter(name__in=GRUPOS_JEFE_FARMACIA).exists()
        ):
            raise PermissionDenied(
                "Solo el Jefe de Farmacia puede resolver excepciones."
            )
        respuesta = normalizar_texto_seguro(respuesta or "").strip()
        if len(respuesta) > 2000:
            raise ValidationError(
                {"respuesta_jefe": "La respuesta no puede superar los 2000 caracteres."}
            )
        if not aprobado and not respuesta:
            raise ValidationError(
                {"respuesta_jefe": "Indica el motivo por el que rechazas la excepción."}
            )

        with transaction.atomic():
            venta = VentaRecetaRetenida.objects.select_for_update().get(
                pk=self.venta_id
            )
            autorizacion = type(self).objects.select_for_update().get(pk=self.pk)
            if autorizacion.estado != self.Estado.PENDIENTE:
                raise ValidationError("Esta excepción ya fue resuelta.")
            if venta.estado != VentaRecetaRetenida.Estado.PENDIENTE_REVISION:
                raise ValidationError("La receta ya no está pendiente de revisión.")

            estado = self.Estado.APROBADA if aprobado else self.Estado.RECHAZADA
            fecha = timezone.now()
            type(self).objects.filter(
                pk=autorizacion.pk,
                estado=self.Estado.PENDIENTE,
            ).update(
                estado=estado,
                jefe_farmacia=jefe,
                respuesta_jefe=respuesta,
                fecha_resolucion=fecha,
            )
            if not aprobado:
                VentaRecetaRetenida.objects.filter(pk=venta.pk).update(
                    estado=VentaRecetaRetenida.Estado.RECHAZADA,
                    jefe_farmacia=jefe,
                    revisado_por=jefe,
                    observacion_resolucion=respuesta,
                    fecha_resolucion=fecha,
                )

            self.estado = estado
            self.jefe_farmacia = jefe
            self.respuesta_jefe = respuesta
            self.fecha_resolucion = fecha

    def __str__(self):
        return f"Excepción #{self.pk} - venta #{self.venta_id} ({self.get_estado_display()})"


class SolicitudCuenta(models.Model):
    class RolSolicitado(models.TextChoices):
        FARMACEUTICO = "farmaceutico", "Químico en farmacia"

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


class Medicamento(SanitizaTextoMixin, models.Model):
    campos_texto_sanitizables = ("codigo", "nombre", "descripcion")

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
        validators=[MaxValueValidator(MAX_POSITIVE_INTEGER)],
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

    def clean(self):
        super().clean()
        self.codigo = normalizar_texto_seguro(self.codigo).strip()
        self.nombre = normalizar_texto_seguro(self.nombre).strip()
        self.descripcion = normalizar_texto_seguro(self.descripcion).strip()
        if not self.nombre:
            raise ValidationError({"nombre": "Indica el nombre del medicamento."})

    @property
    def stock_actual(self):
        lotes_vigentes = self.ingresos.filter(
            fecha_vencimiento__gte=timezone.localdate()
        )
        return lotes_vigentes.aggregate(total=Sum("stock_disponible"))["total"] or 0

    def __str__(self):
        return f"{self.nombre} ({self.codigo})"


class Proveedor(SanitizaTextoMixin, models.Model):
    campos_texto_sanitizables = ("nombre", "rut")

    nombre = models.CharField("Nombre", max_length=200, unique=True)
    rut = models.CharField("RUT", max_length=12, blank=True)
    activo = models.BooleanField("Activo", default=True)

    class Meta:
        verbose_name = "proveedor"
        verbose_name_plural = "proveedores"
        ordering = ("nombre",)

    def clean(self):
        super().clean()
        self.nombre = normalizar_texto_seguro(self.nombre).strip()
        self.rut = normalizar_texto_seguro(self.rut).strip()
        if not self.nombre:
            raise ValidationError({"nombre": "Indica el nombre del proveedor."})

    def __str__(self):
        return self.nombre


class IngresoInventario(SanitizaTextoMixin, models.Model):
    campos_texto_sanitizables = ("lote",)

    medicamento = models.ForeignKey(
        Medicamento,
        on_delete=models.PROTECT,
        related_name="ingresos",
        verbose_name="Medicamento",
    )
    lote = models.CharField("Número de lote", max_length=100)
    cantidad = models.PositiveIntegerField(
        "Cantidad recibida",
        validators=[
            MinValueValidator(1),
            MaxValueValidator(MAX_POSITIVE_INTEGER),
        ],
    )
    stock_disponible = models.PositiveIntegerField(
        "Stock disponible",
        default=0,
        editable=False,
        validators=[MaxValueValidator(MAX_POSITIVE_INTEGER)],
    )
    fecha_vencimiento = models.DateField("Fecha de vencimiento")
    proveedor = models.ForeignKey(
        Proveedor,
        on_delete=models.PROTECT,
        verbose_name="Proveedor",
    )
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

    def clean(self):
        super().clean()
        self.lote = normalizar_texto_seguro(self.lote).strip()
        if (
            self.fecha_vencimiento
            and self.fecha_vencimiento < timezone.localdate()
        ):
            raise ValidationError(
                {
                    "fecha_vencimiento": (
                        "No se puede ingresar un lote cuya fecha de vencimiento "
                        "ya pasó."
                    )
                }
            )

    def save(self, *args, **kwargs):
        if self._state.adding:
            self.stock_disponible = self.cantidad
            self.full_clean()
        else:
            original = type(self).objects.only("cantidad").get(pk=self.pk)
            if self.cantidad != original.cantidad:
                raise ValidationError(
                    {"cantidad": "No se puede modificar la cantidad de un ingreso guardado."}
                )
        super().save(*args, **kwargs)

    def ajustar_stock(self, usuario, cantidad_nueva, fecha_vencimiento_nueva, motivo):
        if not usuario.is_active or not (
            usuario.is_superuser
            or usuario.groups.filter(name__in=GRUPOS_JEFE_FARMACIA).exists()
        ):
            raise PermissionDenied(
                "Solo el Jefe de Farmacia puede ajustar el inventario."
            )
        if (
            isinstance(cantidad_nueva, bool)
            or not isinstance(cantidad_nueva, int)
            or not 1 <= cantidad_nueva <= MAX_POSITIVE_INTEGER
        ):
            raise ValidationError(
                {"cantidad": "La cantidad debe ser un entero positivo válido."}
            )
        if not isinstance(fecha_vencimiento_nueva, date):
            raise ValidationError(
                {"fecha_vencimiento": "Indica una fecha de vencimiento válida."}
            )

        motivo = normalizar_texto_seguro(motivo or "").strip()
        if not motivo:
            raise ValidationError({"motivo": "Indica el motivo del ajuste."})
        if len(motivo) > MAX_MOTIVO_AJUSTE_LENGTH:
            raise ValidationError(
                {"motivo": "El motivo supera el máximo permitido."}
            )

        with transaction.atomic():
            lote = type(self).objects.select_for_update().get(pk=self.pk)
            if (
                cantidad_nueva == lote.cantidad
                and fecha_vencimiento_nueva == lote.fecha_vencimiento
            ):
                raise ValidationError(
                    {"__all__": "No hay cambios que registrar para este lote."}
                )
            motivo_auditoria = (
                f"{motivo}\nCantidad recibida corregida de {lote.cantidad} a "
                f"{cantidad_nueva}; vencimiento corregido de "
                f"{lote.fecha_vencimiento.isoformat()} a "
                f"{fecha_vencimiento_nueva.isoformat()}."
            )
            if len(motivo_auditoria) > MAX_MOTIVO_AJUSTE_LENGTH:
                raise ValidationError(
                    {"motivo": "El motivo supera el máximo permitido."}
                )

            stock_anterior = lote.stock_disponible
            stock_nuevo = stock_anterior + cantidad_nueva - lote.cantidad
            if stock_nuevo < 0:
                raise ValidationError(
                    {
                        "cantidad": (
                            "La reducción supera el stock disponible; "
                            "no se puede dejar el inventario negativo."
                        )
                    }
                )
            if stock_nuevo > MAX_POSITIVE_INTEGER:
                raise ValidationError(
                    {"cantidad": "El ajuste supera el máximo de stock permitido."}
                )

            actualizado = type(self).objects.filter(
                pk=lote.pk,
                cantidad=lote.cantidad,
                stock_disponible=stock_anterior,
                fecha_vencimiento=lote.fecha_vencimiento,
            ).update(
                cantidad=cantidad_nueva,
                stock_disponible=stock_nuevo,
                fecha_vencimiento=fecha_vencimiento_nueva,
            )
            if not actualizado:
                raise ValidationError(
                    {"__all__": "El lote cambió; recarga la página y reintenta."}
                )

            AuditoriaInventario.objects.create(
                usuario=usuario,
                lote=lote,
                tipo_ajuste=(
                    AuditoriaInventario.TipoAjuste.REDUCCION_STOCK
                    if cantidad_nueva < lote.cantidad
                    else AuditoriaInventario.TipoAjuste.CORRECCION_INGRESO
                ),
                cantidad_anterior=stock_anterior,
                cantidad_nueva=stock_nuevo,
                motivo=motivo_auditoria,
            )

        self.cantidad = cantidad_nueva
        self.stock_disponible = stock_nuevo
        self.fecha_vencimiento = fecha_vencimiento_nueva

    @property
    def mermas_totales(self):
        return (
            self.mermas.filter(revertida=False).aggregate(total=Sum("cantidad"))[
                "total"
            ]
            or 0
        )

    def __str__(self):
        return f"{self.medicamento} — Lote {self.lote}"


class Merma(SanitizaTextoMixin, models.Model):
    campos_texto_sanitizables = ("observacion",)

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
        validators=[
            MinValueValidator(1),
            MaxValueValidator(MAX_POSITIVE_INTEGER),
        ],
    )
    motivo = models.CharField("Motivo", max_length=12, choices=Motivo.choices)
    observacion = models.TextField("Observación", max_length=2000)
    fecha_registro = models.DateTimeField("Fecha de registro", auto_now_add=True)
    revertida = models.BooleanField(
        "Merma revertida",
        default=False,
        editable=False,
    )
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
        if self._state.adding and self.revertida:
            raise ValidationError(
                {"revertida": "Una merma nueva no puede crearse como revertida."}
            )
        self.observacion = normalizar_texto_seguro(
            self.observacion or ""
        ).strip()
        if not self.observacion:
            raise ValidationError(
                {
                    "observacion": (
                        "Describe el motivo de la merma (por ejemplo, "
                        "vencimiento o daño)."
                    )
                }
            )
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

    def revertir(self, usuario, motivo):
        if not usuario.is_active or not (
            usuario.is_superuser
            or usuario.groups.filter(name__in=GRUPOS_JEFE_FARMACIA).exists()
        ):
            raise PermissionDenied(
                "Solo el Jefe de Farmacia puede revertir una merma."
            )
        motivo = normalizar_texto_seguro(motivo or "").strip()
        if not motivo:
            raise ValidationError({"motivo": "Indica el motivo de la reversión."})
        if len(motivo) > MAX_MOTIVO_AJUSTE_LENGTH:
            raise ValidationError(
                {"motivo": "El motivo supera el máximo permitido."}
            )

        try:
            with transaction.atomic():
                lote = IngresoInventario.objects.select_for_update().get(
                    pk=self.ingreso_id
                )
                merma = type(self).objects.select_for_update().get(pk=self.pk)
                if merma.revertida or AuditoriaInventario.objects.filter(
                    merma=merma,
                    tipo_ajuste=AuditoriaInventario.TipoAjuste.REVERSION_MERMA,
                ).exists():
                    raise ValidationError("Esta merma ya fue revertida.")

                stock_anterior = lote.stock_disponible
                stock_nuevo = stock_anterior + merma.cantidad
                if stock_nuevo > MAX_POSITIVE_INTEGER:
                    raise ValidationError(
                        "La reversión supera el máximo de stock permitido."
                    )

                actualizado = IngresoInventario.objects.filter(
                    pk=lote.pk,
                    stock_disponible=stock_anterior,
                ).update(stock_disponible=stock_nuevo)
                if not actualizado:
                    raise ValidationError(
                        "El stock del lote cambió; recarga la página y reintenta."
                    )
                merma_actualizada = type(self).objects.filter(
                    pk=merma.pk,
                    revertida=False,
                ).update(revertida=True)
                if not merma_actualizada:
                    raise ValidationError("Esta merma ya fue revertida.")

                AuditoriaInventario.objects.create(
                    usuario=usuario,
                    lote=lote,
                    merma=merma,
                    tipo_ajuste=AuditoriaInventario.TipoAjuste.REVERSION_MERMA,
                    cantidad_anterior=stock_anterior,
                    cantidad_nueva=stock_nuevo,
                    motivo=motivo,
                )
        except IntegrityError as error:
            raise ValidationError(
                "Esta merma ya tiene una reversión registrada."
            ) from error

        self.revertida = True

    def __str__(self):
        return f"Merma de {self.cantidad} unidades — {self.ingreso}"


class AuditoriaInventario(models.Model):
    class TipoAjuste(models.TextChoices):
        CORRECCION_INGRESO = "CORRECCION_INGRESO", "Corrección de ingreso"
        REDUCCION_STOCK = "REDUCCION_STOCK", "Reducción de stock"
        REVERSION_MERMA = "REVERSION_MERMA", "Reversión de merma"

    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="auditorias_inventario",
        verbose_name="Usuario",
    )
    lote = models.ForeignKey(
        IngresoInventario,
        on_delete=models.PROTECT,
        related_name="auditorias",
        verbose_name="Lote de inventario",
    )
    merma = models.ForeignKey(
        Merma,
        on_delete=models.PROTECT,
        related_name="auditorias_reversion",
        null=True,
        blank=True,
        verbose_name="Merma revertida",
    )
    tipo_ajuste = models.CharField(
        "Tipo de ajuste",
        max_length=24,
        choices=TipoAjuste.choices,
    )
    cantidad_anterior = models.IntegerField(
        "Stock anterior",
        validators=[
            MinValueValidator(0),
            MaxValueValidator(MAX_POSITIVE_INTEGER),
        ],
    )
    cantidad_nueva = models.IntegerField(
        "Stock nuevo",
        validators=[
            MinValueValidator(0),
            MaxValueValidator(MAX_POSITIVE_INTEGER),
        ],
    )
    motivo = models.TextField(
        "Motivo",
        max_length=MAX_MOTIVO_AJUSTE_LENGTH,
    )
    fecha_registro = models.DateTimeField("Fecha de registro", auto_now_add=True)

    class Meta:
        verbose_name = "auditoría de inventario"
        verbose_name_plural = "auditorías de inventario"
        ordering = ("-fecha_registro", "-pk")
        constraints = [
            models.CheckConstraint(
                check=Q(cantidad_anterior__gte=0, cantidad_nueva__gte=0),
                name="auditoria_inventario_cantidades_no_negativas",
            ),
            models.UniqueConstraint(
                fields=("merma",),
                condition=Q(tipo_ajuste="REVERSION_MERMA"),
                name="auditoria_reversion_merma_unica",
            ),
        ]

    def clean(self):
        super().clean()
        self.motivo = normalizar_texto_seguro(self.motivo or "").strip()
        if not self.motivo:
            raise ValidationError({"motivo": "La justificación es obligatoria."})
        if self.tipo_ajuste == self.TipoAjuste.REVERSION_MERMA:
            if not self.merma_id or self.merma.ingreso_id != self.lote_id:
                raise ValidationError(
                    {"merma": "La reversión debe identificar una merma del mismo lote."}
                )
        elif self.merma_id:
            raise ValidationError(
                {"merma": "Solo una reversión puede asociar una merma."}
            )

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Los registros de auditoría son inmutables.")
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.get_tipo_ajuste_display()} — {self.lote}"


class ConsultaJefeFarmacia(SanitizaTextoMixin, models.Model):
    campos_texto_sanitizables = ("asunto", "pregunta", "respuesta")

    class Estado(models.TextChoices):
        PENDIENTE = "pendiente", "Pendiente de respuesta"
        RESPONDIDA = "respondida", "Respondida"

    solicitante = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="consultas_jefe_farmacia",
        verbose_name="Químico farmacéutico",
    )
    asunto = models.CharField("Asunto", max_length=160)
    pregunta = models.TextField("Consulta")
    foto_receta = models.ImageField(
        "Foto de la receta",
        upload_to=consulta_receta_upload_to,
        storage=PrivatePrescriptionStorage(),
        validators=[validar_archivo_receta],
    )
    estado = models.CharField(
        "Estado",
        max_length=12,
        choices=Estado.choices,
        default=Estado.PENDIENTE,
    )
    respuesta = models.TextField("Respuesta del Jefe de Farmacia", blank=True)
    jefe_farmacia = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="consultas_jefe_farmacia_respondidas",
        null=True,
        blank=True,
        verbose_name="Jefe de Farmacia que respondió",
    )
    fecha_solicitud = models.DateTimeField("Fecha de envío", auto_now_add=True)
    fecha_respuesta = models.DateTimeField(
        "Fecha de respuesta",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "consulta al Jefe de Farmacia"
        verbose_name_plural = "consultas al Jefe de Farmacia"
        ordering = ("estado", "-fecha_solicitud")
        constraints = [
            models.CheckConstraint(
                check=(
                    Q(
                        estado="pendiente",
                        jefe_farmacia__isnull=True,
                        fecha_respuesta__isnull=True,
                    )
                    | Q(
                        estado="respondida",
                        jefe_farmacia__isnull=False,
                        fecha_respuesta__isnull=False,
                    )
                ),
                name="consulta_jefe_respuesta_consistente",
            ),
        ]

    def clean(self):
        super().clean()
        errores = {}
        self.asunto = normalizar_texto_seguro(self.asunto).strip()
        self.pregunta = normalizar_texto_seguro(self.pregunta).strip()
        self.respuesta = normalizar_texto_seguro(self.respuesta).strip()
        if self.solicitante_id and not self.solicitante.groups.filter(
            name=GRUPO_FARMACEUTICO
        ).exists():
            errores["solicitante"] = (
                "La consulta debe ser enviada por un Químico Farmacéutico."
            )
        if self.jefe_farmacia_id and not (
            self.jefe_farmacia.is_superuser
            or self.jefe_farmacia.groups.filter(
                name__in=GRUPOS_JEFE_FARMACIA
            ).exists()
        ):
            errores["jefe_farmacia"] = (
                "La respuesta debe registrarla el Jefe de Farmacia."
            )
        if errores:
            raise ValidationError(errores)

    def responder(self, jefe, respuesta):
        if not (
            jefe.is_superuser
            or jefe.groups.filter(name__in=GRUPOS_JEFE_FARMACIA).exists()
        ):
            raise PermissionDenied(
                "Solo el Jefe de Farmacia puede responder estas consultas."
            )
        respuesta = normalizar_texto_seguro(respuesta).strip()
        if not respuesta:
            raise ValidationError(
                {"respuesta": "Escribe una respuesta antes de enviarla."}
            )
        if len(respuesta) > 5000:
            raise ValidationError(
                {"respuesta": "La respuesta no puede superar los 5000 caracteres."}
            )

        fecha_respuesta = timezone.now()
        actualizadas = type(self).objects.filter(
            pk=self.pk,
            estado=self.Estado.PENDIENTE,
        ).update(
            estado=self.Estado.RESPONDIDA,
            respuesta=respuesta,
            jefe_farmacia=jefe,
            fecha_respuesta=fecha_respuesta,
        )
        if not actualizadas:
            raise ValidationError("Esta consulta ya fue respondida.")

        self.estado = self.Estado.RESPONDIDA
        self.respuesta = respuesta
        self.jefe_farmacia = jefe
        self.fecha_respuesta = fecha_respuesta

    def __str__(self):
        return f"Consulta #{self.pk} — {self.asunto} ({self.get_estado_display()})"
