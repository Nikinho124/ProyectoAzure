from io import BytesIO
from os.path import splitext
from warnings import catch_warnings, simplefilter

from django import forms
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image, ImageOps, UnidentifiedImageError

from farmacia.models import (
    EXTENSIONES_RECETA_PERMITIDAS,
    MAX_RECETA_IMAGE_PIXELS,
    MAX_RECETA_UPLOAD_BYTES,
    MAX_UNIDADES_POR_RECETA,
    Medicamento,
    VentaRecetaRetenida,
    validar_archivo_receta,
)


class RecetaImageField(forms.ImageField):
    def __init__(self, *args, **kwargs):
        validators = list(kwargs.pop("validators", ()))
        validators.append(validar_archivo_receta)
        super().__init__(*args, validators=validators, **kwargs)

    def to_python(self, data):
        if not data:
            return super().to_python(data)
        if getattr(data, "size", 0) > MAX_RECETA_UPLOAD_BYTES:
            raise forms.ValidationError(
                "La imagen de la receta no puede superar los 5 MB."
            )

        try:
            with catch_warnings():
                simplefilter("error", Image.DecompressionBombWarning)
                imagen_validada = super().to_python(data)
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            OSError,
            TypeError,
            UnidentifiedImageError,
            ValueError,
        ) as error:
            raise forms.ValidationError(
                "El archivo debe ser una imagen JPG o PNG válida."
            ) from error
        if imagen_validada is None:
            return None

        extension = splitext(imagen_validada.name)[1].lower()
        formato = imagen_validada.image.format
        if extension not in EXTENSIONES_RECETA_PERMITIDAS:
            raise forms.ValidationError(
                "La imagen de la receta debe tener extensión .jpg, .jpeg o .png."
            )
        if formato not in {"JPEG", "PNG"}:
            raise forms.ValidationError("El archivo debe ser una imagen JPG o PNG.")
        if (extension in {".jpg", ".jpeg"} and formato != "JPEG") or (
            extension == ".png" and formato != "PNG"
        ):
            raise forms.ValidationError(
                "La extensión del archivo no coincide con su formato real."
            )
        if imagen_validada.image.width * imagen_validada.image.height > (
            MAX_RECETA_IMAGE_PIXELS
        ):
            raise forms.ValidationError("La imagen supera el tamaño máximo permitido.")

        try:
            imagen_validada.seek(0)
            with catch_warnings():
                simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(imagen_validada) as original:
                    original.load()
                    orientada = ImageOps.exif_transpose(original)
                    modo = (
                        "RGB"
                        if formato == "JPEG" or "A" not in orientada.getbands()
                        else "RGBA"
                    )
                    normalizada = orientada.convert(modo)
                    limpia = Image.new(modo, normalizada.size)
                    limpia.paste(normalizada)
                    limpia.info.clear()
                    salida = BytesIO()
                    opciones = {"format": formato, "exif": b""}
                    if formato == "JPEG":
                        opciones["quality"] = 90
                    limpia.save(salida, **opciones)
        except forms.ValidationError:
            raise
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            OSError,
            TypeError,
            UnidentifiedImageError,
            ValueError,
        ) as error:
            raise forms.ValidationError(
                "No fue posible procesar la imagen de la receta."
            ) from error

        contenido = salida.getvalue()
        if len(contenido) > MAX_RECETA_UPLOAD_BYTES:
            raise forms.ValidationError(
                "La imagen procesada supera el límite de 5 MB."
            )
        nombre = "receta.jpg" if formato == "JPEG" else "receta.png"
        tipo = "image/jpeg" if formato == "JPEG" else "image/png"
        return super().to_python(SimpleUploadedFile(nombre, contenido, tipo))


class VentaRecetaRetenidaForm(forms.ModelForm):
    medicamento = forms.ModelChoiceField(
        label="Medicamento",
        queryset=Medicamento.objects.filter(
            activo=True,
            requiere_receta=True,
        ).order_by("nombre"),
        required=True,
        empty_label="Selecciona un medicamento",
    )
    foto_receta = RecetaImageField(
        label="Foto de la receta",
        widget=forms.ClearableFileInput(
            attrs={"accept": "image/jpeg,image/png,.jpg,.jpeg,.png"}
        ),
    )
    motivo_excepcion = forms.CharField(
        label="Motivo para solicitar excepción",
        max_length=2000,
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "maxlength": "2000",
            }
        ),
    )

    class Meta:
        model = VentaRecetaRetenida
        fields = (
            "cliente_rut",
            "medicamento",
            "cantidad",
            "foto_receta",
            "medico_prescriptor",
        )
        widgets = {
            "cliente_rut": forms.TextInput(attrs={"maxlength": "12"}),
            "medico_prescriptor": forms.TextInput(attrs={"maxlength": "200"}),
        }

    def clean_medico_prescriptor(self):
        medico = self.cleaned_data["medico_prescriptor"].strip()
        if not medico:
            raise forms.ValidationError("Indica el médico prescriptor.")
        return medico

    def clean_motivo_excepcion(self):
        return self.cleaned_data["motivo_excepcion"].strip()

    def clean(self):
        cleaned_data = super().clean()
        cantidad = cleaned_data.get("cantidad")
        motivo = cleaned_data.get("motivo_excepcion", "")
        if cantidad and cantidad > MAX_UNIDADES_POR_RECETA and not motivo:
            self.add_error(
                "motivo_excepcion",
                "La solicitud supera 6 unidades; justifica la excepción para que "
                "el Jefe de Farmacia la revise.",
            )
        return cleaned_data


class RevisionRecetaForm(forms.Form):
    decision = forms.ChoiceField(
        choices=(("aprobar", "Aprobar"), ("rechazar", "Rechazar")),
    )
    observacion = forms.CharField(
        label="Motivo del rechazo",
        max_length=2000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "maxlength": "2000"}),
    )

    def clean_observacion(self):
        return self.cleaned_data["observacion"].strip()

    def clean(self):
        cleaned_data = super().clean()
        if (
            cleaned_data.get("decision") == "rechazar"
            and not cleaned_data.get("observacion")
        ):
            self.add_error(
                "observacion",
                "Escribe el motivo obligatorio del rechazo.",
            )
        return cleaned_data


class ResolverAutorizacionForm(forms.Form):
    decision = forms.ChoiceField(
        choices=(("aprobar", "Aprobar excepción"), ("rechazar", "Rechazar excepción")),
    )
    respuesta = forms.CharField(
        label="Motivo del rechazo",
        max_length=2000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "maxlength": "2000"}),
    )

    def clean_respuesta(self):
        return self.cleaned_data["respuesta"].strip()

    def clean(self):
        cleaned_data = super().clean()
        if (
            cleaned_data.get("decision") == "rechazar"
            and not cleaned_data.get("respuesta")
        ):
            self.add_error(
                "respuesta",
                "Escribe el motivo obligatorio del rechazo.",
            )
        return cleaned_data
