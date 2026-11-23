from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError

from farmacia.models import GRUPO_FARMACEUTICO, GRUPO_JEFE_FARMACIA


class EmployeeAuthenticationForm(AuthenticationForm):
    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": "Usuario o contraseña incorrectos.",
    }

    def confirm_login_allowed(self, user):
        if not user.is_active or not (
            user.is_superuser
            or user.groups.filter(
                name__in=(GRUPO_FARMACEUTICO, GRUPO_JEFE_FARMACIA)
            ).exists()
        ):
            raise ValidationError(
                self.error_messages["invalid_login"],
                code="invalid_login",
            )

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
