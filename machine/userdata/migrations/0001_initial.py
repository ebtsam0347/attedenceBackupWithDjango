# Generated by Django 4.2.6 on 2023-10-09 10:18

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='UserAttendence',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('user_id', models.IntegerField()),
                ('timestamp', models.DateTimeField()),
                ('status', models.BooleanField()),
            ],
        ),
    ]
