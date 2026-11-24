import unicodedata

from django import forms
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordResetForm,
    SetPasswordForm,
)
from django.core.exceptions import ValidationError

from farmacia.models import GRUPO_FARMACEUTICO, GRUPOS_JEFE_FARMACIA


class FarmaciaPasswordResetForm(PasswordResetForm):
    email = forms.EmailField(
        max_length=100,
        widget=forms.EmailInput(
            attrs={"autocomplete": "email", "maxlength": "100"}
        ),
    )

    def clean_email(self):
        return self.cleaned_data["email"].strip()


class BoundedPasswordField(forms.CharField):
    def __init__(self, *args, **kwargs):
        widget = kwargs.pop("widget", forms.PasswordInput())
        kwargs["max_length"] = 20
        kwargs["strip"] = False
        widget.attrs["maxlength"] = "20"
        super().__init__(*args, widget=widget, **kwargs)

    def to_python(self, value):
        if isinstance(value, str) and len(value) > self.max_length:
            raise ValidationError(
                "La contraseña no puede superar los 20 caracteres.",
                code="max_length",
            )
        value = super().to_python(value)
        if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
            raise ValidationError(
                "La contraseña contiene caracteres no permitidos.",
                code="invalid_characters",
            )
        return value


class EmployeeAuthenticationForm(AuthenticationForm):
    password = BoundedPasswordField(
        label="Contraseña",
        widget=forms.PasswordInput(
            attrs={"autocomplete": "current-password"}
        ),
    )
    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": "Usuario o contraseña incorrectos.",
    }

    def confirm_login_allowed(self, user):
        if not user.is_active or not (
            user.is_superuser
            or user.groups.filter(
                name__in=(GRUPO_FARMACEUTICO, *GRUPOS_JEFE_FARMACIA)
            ).exists()
        ):
            raise ValidationError(
                self.error_messages["invalid_login"],
                code="invalid_login",
            )

    def clean(self):
        cleaned_data = super().clean()
        if "password" in self.errors:
            self.errors.pop("password")
            self.add_error(None, self.error_messages["invalid_login"])
        return cleaned_data

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = "Usuario"
        self.fields["password"].label = "Contraseña"
        self.fields["username"].widget.attrs.update(
            {"autocomplete": "username"}
        )
        self.fields["password"].widget.attrs.update(
            {"autocomplete": "current-password"}
        )
        self.fields["username"].widget.attrs.pop("autofocus", None)


class BoundedSetPasswordForm(SetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in ("new_password1", "new_password2"):
            original = self.fields[field_name]
            self.fields[field_name] = BoundedPasswordField(
                label=original.label,
                help_text=original.help_text,
                widget=original.widget,
            )
