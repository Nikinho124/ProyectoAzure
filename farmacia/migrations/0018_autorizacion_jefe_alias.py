from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("farmacia", "0017_proveedor_ingreso_inventario"),
    ]

    operations = [
        migrations.AlterField(
            model_name="autorizacionexcepcional",
            name="jefe_farmacia",
            field=models.ForeignKey(
                blank=True,
                limit_choices_to=models.Q(
                    ("groups__name__in", ("Jefe de Farmacia", "Jefe_QF")),
                    ("is_superuser", True),
                    _connector="OR",
                ),
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="autorizaciones_excepcionales_recetas",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Jefe de Farmacia que resuelve",
            ),
        ),
    ]
