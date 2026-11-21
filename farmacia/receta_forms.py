from django import forms

from farmacia.models import (
    MAX_UNIDADES_POR_RECETA,
    Medicamento,
    VentaRecetaRetenida,
    validar_archivo_receta,
)


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
    foto_receta = forms.ImageField(
        label="Foto de la receta",
        validators=[validar_archivo_receta],
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
