Set WShell = CreateObject("WScript.Shell")

' server.log is a redirected file, so Python would default to cp1252 and crash on
' non-cp1252 characters in print() - that killed the API push and its retries
WShell.Environment("Process")("PYTHONUTF8") = "1"

' Start Django server silently using venv python directly (no CMD window)
' --noreload: autoreloader would start the background pull/retry threads twice
WShell.Run "cmd /c ""C:\machine\.venv\Scripts\python.exe C:\machine\manage.py runserver 0.0.0.0:8002 --noreload > C:\machine\server.log 2>&1""", 0, False
