from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import farmacia.models
import farmacia.storage


def vincular_medicamentos(apps, schema_editor):
    Receta = apps.get_model("farmacia", "VentaRecetaRetenida")
    Medicamento = apps.get_model("farmacia", "Medicamento")
    db_alias = schema_editor.connection.alias

    for receta in Receta.objects.using(db_alias).exclude(medicamento="").iterator():
        nombre = receta.medicamento.strip()
        receta.medicamento_legado = nombre
        medicamento = (
            Medicamento.objects.using(db_alias)
            .filter(nombre__iexact=nombre)
            .order_by("pk")
            .first()
        )
        if medicamento is None:
            medicamento = (
                Medicamento.objects.using(db_alias)
                .filter(codigo__iexact=nombre)
                .first()
            )
        if medicamento is not None:
            receta.medicamento_ref_id = medicamento.pk
        receta.save(
            using=db_alias,
            update_fields=("medicamento_ref", "medicamento_legado"),
        )

    Receta.objects.using(db_alias).filter(
        jefe_farmacia__isnull=False,
        revisado_por__isnull=True,
    ).update(revisado_por_id=models.F("jefe_farmacia_id"))


def restaurar_nombres_medicamentos(apps, schema_editor):
    Receta = apps.get_model("farmacia", "VentaRecetaRetenida")
    db_alias = schema_editor.connection.alias
    for receta in Receta.objects.using(db_alias).iterator():
        if receta.medicamento_ref_id:
            receta.medicamento = receta.medicamento_ref.nombre
        else:
            receta.medicamento = receta.medicamento_legado
        receta.save(using=db_alias, update_fields=("medicamento",))


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("farmacia", "0011_recipe_exception_text_limits"),
    ]

    operations = [
        migrations.AlterField(
            model_name="ventarecetaretenida",
            name="medicamento",
            field=models.CharField(
                blank=True,
                max_length=200,
                null=True,
                verbose_name="Medicamento",
            ),
        ),
        migrations.AddField(
            model_name="ventarecetaretenida",
            name="medicamento_ref",
            field=models.ForeignKey(
                blank=True,
                limit_choices_to={"activo": True, "requiere_receta": True},
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="recetas_retenidas_migracion",
                to="farmacia.medicamento",
                verbose_name="Medicamento",
            ),
        ),
        migrations.AddField(
            model_name="ventarecetaretenida",
            name="revisado_por",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="recetas_retenidas_revisadas",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Revisado por",
            ),
        ),
        migrations.AddField(
            model_name="ventarecetaretenida",
            name="venta_caja",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="recetas_retenidas",
                to="farmacia.ventacaja",
                verbose_name="Venta de caja",
            ),
        ),
        migrations.AddField(
            model_name="ventarecetaretenida",
            name="medicamento_legado",
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=200,
                verbose_name="Nombre histórico del medicamento",
            ),
        ),
        migrations.RunPython(vincular_medicamentos, restaurar_nombres_medicamentos),
        migrations.RemoveField(
            model_name="ventarecetaretenida",
            name="medicamento",
        ),
        migrations.RenameField(
            model_name="ventarecetaretenida",
            old_name="medicamento_ref",
            new_name="medicamento",
        ),
        migrations.AlterField(
            model_name="ventarecetaretenida",
            name="medicamento",
            field=models.ForeignKey(
                blank=True,
                limit_choices_to={"activo": True, "requiere_receta": True},
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="recetas_retenidas",
                to="farmacia.medicamento",
                verbose_name="Medicamento",
            ),
        ),
        migrations.AlterField(
            model_name="ventarecetaretenida",
            name="foto_receta",
            field=models.ImageField(
                storage=farmacia.storage.PrivatePrescriptionStorage(),
                upload_to="recetas_retenidas/%Y/%m/",
                validators=[farmacia.models.validar_archivo_receta],
                verbose_name="Foto de la receta",
            ),
        ),
        migrations.AlterField(
            model_name="ventarecetaretenida",
            name="estado",
            field=models.CharField(
                choices=[
                    ("pendiente_revision", "Pendiente de revisión"),
                    ("aprobada", "Aprobada"),
                    ("rechazada", "Rechazada"),
                    ("bloqueada", "Bloqueada"),
                    ("dispensada", "Dispensada"),
                ],
                default="pendiente_revision",
                max_length=24,
                verbose_name="Estado",
            ),
        ),
    ]
