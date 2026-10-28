from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible


@deconstructible
class PrivatePrescriptionStorage(FileSystemStorage):
    def __init__(self):
        super().__init__(
            location=settings.BASE_DIR / "private_recetas",
            base_url="/private-recetas/",
        )

    def url(self, name):
        raise ValueError("Las recetas solo se pueden consultar desde una vista protegida.")
