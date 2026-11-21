from os.path import splitext

from django import forms

from farmacia.models import ConsultaJefeFarmacia


MAX_RECETA_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_RECETA_IMAGE_PIXELS = 40_000_000
EXTENSIONES_RECETA_PERMITIDAS = {".jpg", ".jpeg", ".png"}


class RecetaImageField(forms.ImageField):
    def to_python(self, data):
        extension = splitext(getattr(data, "name", ""))[1].lower() if data else ""
        if data and extension not in EXTENSIONES_RECETA_PERMITIDAS:
            raise forms.ValidationError(
                "La imagen de la receta debe tener extensión .jpg, .jpeg o .png.",
                code="invalid_image_extension",
            )
        if data and getattr(data, "size", 0) > MAX_RECETA_UPLOAD_BYTES:
            raise forms.ValidationError(
                "La imagen de la receta no puede superar los 5 MB.",
                code="image_too_large",
            )

        imagen = super().to_python(data)
        if imagen and imagen.image.width * imagen.image.height > MAX_RECETA_IMAGE_PIXELS:
            raise forms.ValidationError(
                "La imagen supera el tamaño máximo permitido.",
                code="image_dimensions_too_large",
            )
        return imagen


class ConsultaJefeFarmaciaForm(forms.ModelForm):
    pregunta = forms.CharField(
        label="Consulta",
        max_length=5000,
        widget=forms.Textarea(attrs={"rows": 5}),
    )
    foto_receta = RecetaImageField(
        label="Foto de la receta",
        help_text="Sube una imagen legible de la receta para que el Jefe de Farmacia la revise.",
        widget=forms.ClearableFileInput(
            attrs={"accept": "image/jpeg,image/png,.jpg,.jpeg,.png"}
        ),
    )

    class Meta:
        model = ConsultaJefeFarmacia
        fields = ("asunto", "pregunta", "foto_receta")

    def clean_asunto(self):
        asunto = self.cleaned_data["asunto"].strip()
        if not asunto:
            raise forms.ValidationError("Escribe un asunto.")
        return asunto

    def clean_pregunta(self):
        pregunta = self.cleaned_data["pregunta"].strip()
        if not pregunta:
            raise forms.ValidationError("Describe tu consulta.")
        return pregunta

    def clean_foto_receta(self):
        foto = self.cleaned_data["foto_receta"]
        if foto.image.format not in {"JPEG", "PNG"}:
            raise forms.ValidationError("Sube una imagen JPG o PNG válida.")
        extension = {"JPEG": "jpg", "PNG": "png"}[foto.image.format]
        foto.name = f"receta.{extension}"
        return foto
