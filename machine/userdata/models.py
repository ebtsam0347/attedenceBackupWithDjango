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

