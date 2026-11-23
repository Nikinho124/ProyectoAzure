from django import forms
from PIL import Image

from farmacia.models import (
    MAX_RECETA_IMAGE_PIXELS,
    ConsultaJefeFarmacia,
    normalizar_texto_seguro,
)
from farmacia.receta_forms import RecetaImageField, SanitizedCharField

Image.MAX_IMAGE_PIXELS = MAX_RECETA_IMAGE_PIXELS


class ConsultaJefeFarmaciaForm(forms.ModelForm):
    asunto = SanitizedCharField(
        label="Asunto",
        max_length=160,
    )
    pregunta = SanitizedCharField(
        label="Consulta",
        max_length=5000,
        widget=forms.Textarea(attrs={"rows": 5, "maxlength": "5000"}),
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
        asunto = normalizar_texto_seguro(self.cleaned_data["asunto"]).strip()
        if not asunto:
            raise forms.ValidationError("Escribe un asunto.")
        return asunto

    def clean_pregunta(self):
        pregunta = normalizar_texto_seguro(self.cleaned_data["pregunta"]).strip()
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
