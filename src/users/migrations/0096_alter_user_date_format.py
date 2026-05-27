from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0095_user_obfuscate_episodes"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="date_format",
            field=models.CharField(
                choices=[
                    ("system_default", "System default (locale)"),
                    ("iso_8601", "ISO 8601"),
                    ("month_d_yyyy", "Month D, YYYY"),
                    ("d_mon_yyyy", "D Mon YYYY"),
                    ("m_d_yyyy", "M/D/YYYY"),
                    ("d_m_yyyy", "D/M/YYYY"),
                    ("dd_mm_yyyy", "DD.MM.YYYY"),
                    ("yyyy_mm_dd", "YYYY/MM/DD"),
                    ("long_eu", "18 Jan, 2026"),
                ],
                default="system_default",
                max_length=20,
            ),
        ),
    ]
