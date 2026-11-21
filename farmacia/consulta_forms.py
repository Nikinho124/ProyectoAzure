from django import forms

from farmacia.models import ConsultaJefeFarmacia
from farmacia.receta_forms import RecetaImageField


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
