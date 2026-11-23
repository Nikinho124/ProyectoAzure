import os
import re
import unicodedata
import uuid

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
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

    def get_valid_name(self, name):
        name = name.replace("\x00", "")
        name = re.sub(r"%00", "", name, flags=re.IGNORECASE)
        name = unicodedata.normalize("NFKC", name)
        return super().get_valid_name(name)

    def get_available_name(self, name, max_length=None):
        directory, filename = os.path.split(name)
        extension = os.path.splitext(filename)[1].lower()
        if extension in {".jpg", ".jpeg"}:
            extension = ".jpg"
        elif extension != ".png":
            raise SuspiciousFileOperation(
                "Solo se pueden almacenar imágenes JPG o PNG."
            )
        nombre_aleatorio = f"{uuid.uuid4().hex}{extension}"
        if directory:
            nombre_aleatorio = os.path.join(directory, nombre_aleatorio)
        return super().get_available_name(
            nombre_aleatorio,
            max_length=max_length,
        )
