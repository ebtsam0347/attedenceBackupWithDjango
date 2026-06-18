Set WShell = CreateObject("WScript.Shell")

' Start Django server silently using venv python directly (no CMD window)
WShell.Run "cmd /c ""D:\attedenceBackup\machine\env\Scripts\python.exe D:\attedenceBackup\machine\manage.py runserver 8002 > D:\attedenceBackup\machine\server.log 2>&1""", 0, False
