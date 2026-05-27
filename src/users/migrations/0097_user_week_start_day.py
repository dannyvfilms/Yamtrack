from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0096_alter_user_date_format"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="week_start_day",
            field=models.CharField(
                choices=[("monday", "Monday"), ("sunday", "Sunday")],
                default="monday",
                max_length=10,
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(week_start_day__in=["monday", "sunday"]),
                name="week_start_day_valid",
            ),
        ),
    ]
