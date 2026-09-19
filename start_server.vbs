Set sh = WScript.CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\User\Documents\Default Project"
sh.Run """C:\Users\User\Documents\Default Project\venv\Scripts\python.exe"" server\app.py", 0, False