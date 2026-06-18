from django.contrib import admin
from .models import *

admin.site.register(UserAttendence)

@admin.register(LiveAttendance)
class LiveAttendanceAdmin(admin.ModelAdmin):
    list_display  = ('user_id', 'full_name', 'squad', 'status', 'att_datetime', 'device_name', 'device_ip')
    list_filter   = ('status', 'device_name')
    search_fields = ('user_id', 'full_name', 'reg_no', 'squad')
    ordering      = ('-att_datetime',)
    readonly_fields = ('created_at',)

@admin.register(PayRoll_payroll_deviceinfo)
class DeviceInfoAdmin(admin.ModelAdmin):
    list_display  = ('DeviceName', 'DeviceIp', 'Port', 'Location', 'IsActive')
    list_editable = ('IsActive',)
    search_fields = ('DeviceName', 'DeviceIp', 'Location')
    list_filter   = ('IsActive',)