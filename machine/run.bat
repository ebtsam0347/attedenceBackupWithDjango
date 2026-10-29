@echo off
call .venv\scripts\activate
python manage.py runserver 0.0.0.0:8002
start chrome http://127.0.0.1:8002/
