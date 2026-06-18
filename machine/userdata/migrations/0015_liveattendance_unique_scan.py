from django.db import migrations, models


def remove_exact_duplicates(apps, schema_editor):
    """Keep the first (lowest id) record for each (user_id, att_datetime, device_ip) combo."""
    LiveAttendance = apps.get_model('userdata', 'LiveAttendance')
    seen = {}
    for rec in LiveAttendance.objects.order_by('id'):
        key = (rec.user_id, rec.att_datetime, rec.device_ip)
        if key in seen:
            rec.delete()
        else:
            seen[key] = rec.id


class Migration(migrations.Migration):

    dependencies = [
        ('userdata', '0014_liveattendance_api_status_reason'),
    ]

    operations = [
        migrations.RunPython(remove_exact_duplicates, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='liveattendance',
            constraint=models.UniqueConstraint(
                fields=['user_id', 'att_datetime', 'device_ip'],
                name='unique_live_attendance_scan',
            ),
        ),
    ]
