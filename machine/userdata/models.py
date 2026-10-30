from django.db import models

# Create your models here.
class UserAttendence(models.Model):
    user_id = models.IntegerField(null=True,blank=True)
    timestamp = models.DateTimeField(null=True,blank=True)
    status = models.CharField(max_length=45,null=True,blank=True)

    def __str__(self):
        return f'{ self.user_id }'
    class Meta:
        ordering = ['-timestamp']

#database table 
class tbl_Shop_ATT(models.Model):
    CardNo = models.IntegerField(null=True,blank=True)
    AttTime = models.DateTimeField(null=True,blank=True)
    attenStatus = models.IntegerField()
    DeviceName = models.CharField(max_length=600)
    DeviceIp = models.CharField(max_length=600,null=True)
    def __str__(self):
        return f'{ self.user_id }'
    
    class Meta:
        ordering = ['-AttTime']

class dami(models.Model):
    CardNo = models.IntegerField(null=True,blank=True)
    AttTime = models.DateTimeField(null=True,blank=True)
    attenStatus = models.IntegerField()
    DeviceName = models.CharField(max_length=600)
    DeviceIp = models.CharField(max_length=600,null=True)
    def __str__(self):
        return f'{ self.CardNo }'

    class Meta:
            ordering = ['-AttTime']


class LiveAttendance(models.Model):
    user_id     = models.IntegerField(null=True, blank=True)  # website player ID, NULL until identified
    zk_user_id  = models.CharField(max_length=24, blank=True, default='')  # ID as stored on the device
    att_datetime= models.DateTimeField()
    status      = models.IntegerField(default=0)
    device_name = models.CharField(max_length=255, blank=True)
    device_ip   = models.CharField(max_length=100, blank=True)
    full_name   = models.CharField(max_length=255, blank=True)
    reg_no      = models.CharField(max_length=100, blank=True)
    squad       = models.CharField(max_length=255, blank=True)
    slot        = models.CharField(max_length=100, blank=True)
    api_status  = models.CharField(max_length=20, default='pending')
    api_reason  = models.CharField(max_length=500, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.full_name or self.user_id} — {self.att_datetime}'

    class Meta:
        db_table = 'live_attendance'
        ordering = ['-att_datetime']
        indexes = [
            models.Index(fields=['att_datetime'], name='live_att_time'),
            models.Index(fields=['api_status', 'att_datetime'], name='live_att_status_time'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['user_id', 'att_datetime', 'device_ip'],
                condition=models.Q(user_id__isnull=False),
                name='unique_live_attendance_scan'
            ),
            # __gt, not ~Q: SQL Server filtered indexes do not accept NOT
            models.UniqueConstraint(
                fields=['zk_user_id', 'att_datetime', 'device_ip'],
                condition=models.Q(zk_user_id__gt=''),
                name='unique_live_attendance_zk_scan'
            ),
        ]


class PayRoll_payroll_deviceinfo(models.Model):
    DeviceName = models.CharField(max_length=255)
    DeviceIp   = models.CharField(max_length=100, unique=True)
    Port       = models.IntegerField(default=4370)
    Location   = models.CharField(max_length=255, null=True, blank=True)
    IsActive   = models.BooleanField(default=True)

    def __str__(self):
        return f'{self.DeviceName} ({self.DeviceIp})'

    class Meta:
        db_table = 'PayRoll_payroll_deviceinfo'
        verbose_name = 'Attendance Device'
        verbose_name_plural = 'Attendance Devices'

