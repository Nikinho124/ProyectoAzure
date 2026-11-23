from django import forms

from farmacia.models import MAX_MOTIVO_AJUSTE_LENGTH, MAX_POSITIVE_INTEGER


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
    motivo = forms.CharField(
        label="Motivo del ajuste",
        max_length=1800,
        strip=True,
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    password_confirmacion = forms.CharField(
        label="Contraseña de confirmación",
        max_length=256,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )


class RevertirMermaForm(forms.Form):
    motivo = forms.CharField(
        label="Motivo de la reversión",
        max_length=MAX_MOTIVO_AJUSTE_LENGTH,
        strip=True,
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    password_confirmacion = forms.CharField(
        label="Contraseña de confirmación",
        max_length=256,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )
