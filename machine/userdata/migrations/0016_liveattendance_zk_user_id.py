from django.db import migrations, models
from django.db.models.functions import Cast


def separate_zk_ids(apps, schema_editor):
    """Rows with no name were saved with the raw ZK number in user_id. Move it to
    zk_user_id so those scans wait for a real player ID instead of being pushed."""
    LiveAttendance = apps.get_model('userdata', 'LiveAttendance')
    LiveAttendance.objects.filter(api_status='sent').update(api_status='success')

    LiveAttendance.objects.filter(full_name='').update(
        zk_user_id=Cast('user_id', models.CharField(max_length=24))
    )
    LiveAttendance.objects.filter(full_name='').exclude(
        api_status__in=['success', 'duplicate']
    ).update(
        user_id=None, api_status='pending', api_reason='Player not registered on website yet'
    )


def restore_user_ids(apps, schema_editor):
    LiveAttendance = apps.get_model('userdata', 'LiveAttendance')
    LiveAttendance.objects.filter(api_status='success').update(api_status='sent')
    for rec in LiveAttendance.objects.filter(user_id__isnull=True):
        rec.user_id = int(rec.zk_user_id) if rec.zk_user_id.isdigit() else 0
        rec.save(update_fields=['user_id'])


class Migration(migrations.Migration):

    dependencies = [
        ('userdata', '0015_liveattendance_unique_scan'),
    ]

    operations = [
        # The constraint must go first: SQL Server cannot alter an indexed column
        migrations.RemoveConstraint(
            model_name='liveattendance',
            name='unique_live_attendance_scan',
        ),
        migrations.AddField(
            model_name='liveattendance',
            name='zk_user_id',
            field=models.CharField(blank=True, default='', max_length=24),
        ),
        migrations.AlterField(
            model_name='liveattendance',
            name='user_id',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.RunPython(separate_zk_ids, restore_user_ids),
        migrations.AddConstraint(
            model_name='liveattendance',
            constraint=models.UniqueConstraint(
                condition=models.Q(user_id__isnull=False),
                fields=('user_id', 'att_datetime', 'device_ip'),
                name='unique_live_attendance_scan',
            ),
        ),
        migrations.AddConstraint(
            model_name='liveattendance',
            constraint=models.UniqueConstraint(
                condition=models.Q(zk_user_id__gt=''),
                fields=('zk_user_id', 'att_datetime', 'device_ip'),
                name='unique_live_attendance_zk_scan',
            ),
        ),
    ]
