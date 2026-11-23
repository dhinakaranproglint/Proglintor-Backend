from django.db import migrations

from pulse.employees_import import import_employees_from_assets


def forwards(apps, schema_editor):
    import_employees_from_assets(apps.get_model("pulse", "Employee"), apps.get_model("pulse", "Asset"))


class Migration(migrations.Migration):
    dependencies = [("pulse", "0011_employees")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
