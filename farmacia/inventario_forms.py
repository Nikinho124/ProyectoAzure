from django import forms

from farmacia.auth_forms import BoundedPasswordField
from farmacia.models import MAX_MOTIVO_AJUSTE_LENGTH, MAX_POSITIVE_INTEGER
from farmacia.receta_forms import SanitizedCharField


class AjustarStockLoteForm(forms.Form):
    cantidad = forms.IntegerField(
        label="Cantidad recibida corregida",
        min_value=1,
        max_value=MAX_POSITIVE_INTEGER,
        widget=forms.NumberInput(attrs={"min": 1, "step": 1}),
    )
    fecha_vencimiento = forms.DateField(
        label="Fecha de vencimiento",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    motivo = SanitizedCharField(
        label="Motivo del ajuste",
        max_length=1800,
        strip=True,
        widget=forms.Textarea(attrs={"rows": 4, "maxlength": "1800"}),
    )
    password_confirmacion = BoundedPasswordField(
        label="Contraseña de confirmación",
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )


class RevertirMermaForm(forms.Form):
    motivo = SanitizedCharField(
        label="Motivo de la reversión",
        max_length=MAX_MOTIVO_AJUSTE_LENGTH,
        strip=True,
        widget=forms.Textarea(
            attrs={"rows": 4, "maxlength": str(MAX_MOTIVO_AJUSTE_LENGTH)}
        ),
    )
    password_confirmacion = BoundedPasswordField(
        label="Contraseña de confirmación",
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )
