from django.urls import path
from . import views

urlpatterns = [
    path('', views.auto_attendance_page, name='auto_attendance'),
    path('backup/', views.userdata, name='backup'),
    path('live/', views.live_attendance_page, name='live_attendance'),
    path('live/stream/', views.live_attendance_stream, name='live_attendance_stream'),
    path('records/', views.attendance_records, name='attendance_records'),
    path('api/auto-pull/', views.api_auto_pull, name='api_auto_pull'),
    path('api/today-records/', views.api_today_records, name='api_today_records'),
    path('api/test-push/', views.api_test_push, name='api_test_push'),
    path('back-data/', views.back_data_page, name='back_data'),
    path('devices/', views.devices_page, name='devices'),
]
