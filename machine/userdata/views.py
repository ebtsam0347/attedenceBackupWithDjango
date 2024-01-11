
table_for_attendance = 'tbl_Attendance_Short'
table_for_devices = 'PayRoll_payroll_deviceinfo'



from django.shortcuts import render
from django.http import HttpResponse
import sys
import zklib
import time
from zk import ZK, const
from .models import *
from django.contrib import messages
from datetime import datetime
from django.db import connections
from django.db import IntegrityError
import pandas as pd

# Create your views here.



device_list_with_ip = []
device_list_with_ip_error = []
def add_device_to_list(device_list_with_ip, DeviceIp):
    if DeviceIp not in device_list_with_ip:
        device_list_with_ip.append(DeviceIp)
        
def add_error_device_to_list(device_list_with_ip_error, DeviceIp, error_message):
    for i, entry in enumerate(device_list_with_ip_error):
        if entry[0] == DeviceIp:
            # Update the error message if the same device IP is encountered again
            device_list_with_ip_error[i] = (DeviceIp, error_message)
            break
    else:
        # If the loop did not break, it means the device IP was not found in the list
        device_list_with_ip_error.append((DeviceIp, error_message))


        
def userdata(request):
   
    
    if request.method == 'POST':
        date = request.POST.get('date')
        
        print(date)
        if date:
            try:
                # Attempt to parse the date string
                start_date = datetime.strptime(date, '%Y-%m-%d')
                sql_select = f"""
                    SELECT DeviceIp FROM {table_for_devices}
                """
                with connections['default'].cursor() as cursor:
                    cursor.execute(sql_select)
                    rows = cursor.fetchall()
                device_list = [row[0] for row in rows if row[0] is not None]
                
                with connections['default'].cursor() as cursor:
                    
                    for DeviceIp in device_list:
                        try:
                            zk = ZK(DeviceIp, port=4370,timeout=5)
                            conn = zk.connect()
                        
                            if conn:
                                print(f'Connected with {DeviceIp}')
                                device_name = zk.get_device_name()
                                zk = ZK(DeviceIp, port=4370)
                                conn = zk.connect()
                                DeviceName = zk.get_device_name()
                                attendances = zk.get_attendance()
                                
                            for attendance in attendances:
                                user_id = attendance.user_id
                                timestamp = attendance.timestamp
                                status = attendance.status

                                # Check if the timestamp is within the date range
                                if start_date <= timestamp:
                                    try:
                                            # Attempt to create a datetime object from the timestamp
                                            created_datetime = datetime(
                                                year=timestamp.year,
                                                month=timestamp.month,
                                                day=timestamp.day,
                                                hour=timestamp.hour,
                                                minute=timestamp.minute,
                                                second=timestamp.second
                                            )

                                            # Define your SQL INSERT statement
                                            sql_insert = f"""
                                                INSERT INTO {table_for_attendance} (user_id, datetime, status, device_name, device_ip)
                                                VALUES (%s, %s, %s, %s, %s)
                                                """
                                            values = (user_id, created_datetime, status, DeviceName, DeviceIp)

                                            cursor.execute(sql_insert, values)
                                            
                                            print(f'Inserted into {DeviceIp}')
                                            add_device_to_list(device_list_with_ip, DeviceIp)
                                          
                                        
                                    except Exception as e:
                                            print(f'Error for {DeviceIp}: {str(e)}')
                                            add_error_device_to_list(device_list_with_ip_error,DeviceIp,f'Error for {DeviceIp}: {str(e)}')
                                # Commit the changes
                                    connections['default'].commit()
                        except Exception as e:
                                # print(f'Error capturing attendance: {str(e)}')
                                add_error_device_to_list(device_list_with_ip_error,DeviceIp,f'Error for {DeviceIp}: {str(e)}')
                                continue


            except ValueError:
                # Handle the case when the date is not valid
                messages.warning(request, "Invalid date .")
                return render(request, 'userdata/index.html',context)
        else:
            context={}
            messages.warning(request, "Invalid date .")
            return render(request, 'userdata/index.html',context)
    print(device_list_with_ip_error) 
    
    context = {
        'device_list_with_ip':device_list_with_ip,
        'device_list_with_ip_error':device_list_with_ip_error,
        
    }
    return render(request, 'userdata/index.html',context)



