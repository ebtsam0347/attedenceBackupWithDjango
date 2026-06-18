from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('userdata', '0013_liveattendance_slot'),
    ]

    operations = [
        migrations.AddField(
            model_name='liveattendance',
            name='api_status',
            field=models.CharField(default='pending', max_length=20),
        ),
        migrations.AddField(
            model_name='liveattendance',
            name='api_reason',
            field=models.CharField(blank=True, max_length=500),
        ),
    ]
