from django import forms

from farmacia.models import SolicitudRevisionReceta


class SolicitudRevisionRecetaForm(forms.ModelForm):
    class Meta:
        model = SolicitudRevisionReceta
        fields = (
            "cliente_nombre",
            "cliente_rut",
            "medicamento",
            "cantidad",
            "medico_prescriptor",
            "foto_receta",
        )
        widgets = {
            "foto_receta": forms.ClearableFileInput(
                attrs={"accept": "image/jpeg,image/png,image/webp"}
            ),
        }

    def clean_foto_receta(self):
        archivo = self.cleaned_data["foto_receta"]
        if archivo.size > 5 * 1024 * 1024:
            raise forms.ValidationError("La imagen no puede superar los 5 MB.")

        extensiones = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}
        extension = extensiones.get(archivo.image.format)
        if extension is None:
            raise forms.ValidationError(
                "Sube una imagen JPG, PNG o WEBP válida."
            )
        archivo.name = f"receta.{extension}"
        return archivo
