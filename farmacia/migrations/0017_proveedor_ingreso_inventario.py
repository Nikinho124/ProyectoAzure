from django.db import migrations, models
import django.db.models.deletion


PROVEEDOR_SIN_REGISTRO = "Proveedor no especificado (legado)"


def migrar_proveedores(apps, schema_editor):
    IngresoInventario = apps.get_model("farmacia", "IngresoInventario")
    Proveedor = apps.get_model("farmacia", "Proveedor")
    base_datos = schema_editor.connection.alias
    nombre_sin_registro = PROVEEDOR_SIN_REGISTRO
    while (
        IngresoInventario.objects.using(base_datos)
        .filter(proveedor_legacy=nombre_sin_registro)
        .exists()
    ):
        nombre_sin_registro += " (importado)"

    for ingreso in IngresoInventario.objects.using(base_datos).all().iterator(
        chunk_size=500
    ):
        nombre = (ingreso.proveedor_legacy or "").strip()
        if not nombre:
            nombre = nombre_sin_registro
            activo = False
        else:
            activo = True
        proveedor, _ = Proveedor.objects.using(base_datos).get_or_create(
            nombre=nombre,
            defaults={"activo": activo},
        )
        ingreso.proveedor_id = proveedor.pk
        ingreso.save(using=base_datos, update_fields=("proveedor",))


def restaurar_proveedores(apps, schema_editor):
    IngresoInventario = apps.get_model("farmacia", "IngresoInventario")
    base_datos = schema_editor.connection.alias
    for ingreso in (
        IngresoInventario.objects.using(base_datos)
        .select_related("proveedor")
        .all()
        .iterator(chunk_size=500)
    ):
        ingreso.proveedor_legacy = (
            ""
            if not ingreso.proveedor.activo
            else ingreso.proveedor.nombre
        )
        ingreso.save(using=base_datos, update_fields=("proveedor_legacy",))


class Migration(migrations.Migration):
    dependencies = [
        ("farmacia", "0016_alter_consultajefefarmacia_foto_receta_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="Proveedor",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("nombre", models.CharField(max_length=200, unique=True, verbose_name="Nombre")),
                ("rut", models.CharField(blank=True, max_length=12, verbose_name="RUT")),
                ("activo", models.BooleanField(default=True, verbose_name="Activo")),
            ],
            options={
                "verbose_name": "proveedor",
                "verbose_name_plural": "proveedores",
                "ordering": ("nombre",),
            },
        ),
        migrations.RenameField(
            model_name="ingresoinventario",
            old_name="proveedor",
            new_name="proveedor_legacy",
        ),
        migrations.AddField(
            model_name="ingresoinventario",
            name="proveedor",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="farmacia.proveedor",
                verbose_name="Proveedor",
            ),
        ),
        migrations.RunPython(migrar_proveedores, restaurar_proveedores),
        migrations.RemoveField(
            model_name="ingresoinventario",
            name="proveedor_legacy",
        ),
        migrations.AlterField(
            model_name="ingresoinventario",
            name="proveedor",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                to="farmacia.proveedor",
                verbose_name="Proveedor",
            ),
        ),
    ]
