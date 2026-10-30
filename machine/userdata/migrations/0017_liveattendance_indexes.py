from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('userdata', '0016_liveattendance_zk_user_id'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='liveattendance',
            index=models.Index(fields=['att_datetime'], name='live_att_time'),
        ),
        migrations.AddIndex(
            model_name='liveattendance',
            index=models.Index(fields=['api_status', 'att_datetime'], name='live_att_status_time'),
        ),
    ]
