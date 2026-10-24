from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.forms import UserCreationForm
from django.db import transaction

from farmacia.models import (
    GRUPO_CLIENTE,
    SolicitudCuenta,
)

User = get_user_model()


class RegistroUsuarioForm(UserCreationForm):
    email = forms.EmailField(label="Correo electrónico", required=True)
    password1 = forms.CharField(
        label="Contraseña",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text=(
            "La contraseña no puede ser demasiado similar a tu información personal, "
            "debe tener al menos 8 caracteres, no puede ser una contraseña común "
            "ni estar compuesta solo por números."
        ),
    )
    password2 = forms.CharField(
        label="Confirmar contraseña",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Escribe nuevamente la contraseña para verificarla.",
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email")
        labels = {
            "username": "Nombre de usuario",
            "first_name": "Nombre",
            "last_name": "Apellido",
        }

    rol_solicitado = forms.ChoiceField(
        label="Tipo de cuenta",
        choices=SolicitudCuenta.RolSolicitado.choices,
    )

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Ya existe una cuenta con este correo.")
        return email

    def save(self, commit=True):
        usuario = super().save(commit=False)
        usuario.email = self.cleaned_data["email"]
        es_cliente = (
            self.cleaned_data["rol_solicitado"]
            == SolicitudCuenta.RolSolicitado.CLIENTE
        )
        usuario.is_active = es_cliente
        if commit:
            with transaction.atomic():
                usuario.save()
                if es_cliente:
                    grupo, _ = Group.objects.get_or_create(name=GRUPO_CLIENTE)
                    usuario.groups.add(grupo)
                else:
                    SolicitudCuenta.objects.create(
                        usuario=usuario,
                        rol_solicitado=self.cleaned_data["rol_solicitado"],
                    )
        return usuario
