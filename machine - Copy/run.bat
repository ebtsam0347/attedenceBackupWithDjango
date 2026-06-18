@echo off
call env\scripts\activate
python manage.py runserver 8002
start chrome http://127.0.0.1:8002/