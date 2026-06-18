Set WShell = CreateObject("WScript.Shell")

' Start Django server silently (no CMD window) on port 8002
WShell.Run "cmd /c ""D:\attedenceBackup\machine\env\Scripts\activate && python D:\attedenceBackup\machine\manage.py runserver 8002""", 0, False
