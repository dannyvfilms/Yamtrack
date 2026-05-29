from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0097_user_week_start_day"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="statistics_compare_mode",
            field=models.CharField(
                choices=[
                    ("previous_period", "Previous period"),
                    ("last_year", "Last year"),
                    ("none", "No comparison"),
                ],
                default="previous_period",
                help_text="Default comparison mode for finite ranges on the Statistics page",
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("statistics_compare_mode__in", ["previous_period", "last_year", "none"]),
                ),
                name="statistics_compare_mode_valid",
            ),
        ),
    ]
