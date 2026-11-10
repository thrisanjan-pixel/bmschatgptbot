@echo off
rem Starts the Open on PC helper without a console window. Nothing is added to Windows startup.
cd /d "%~dp0"
set "PYW=C:\Users\Gaming Pc\AppData\Local\Programs\Python\Python312\pythonw.exe"
if exist "%PYW%" (start "" "%PYW%" pc_opener.py) else (start "" pythonw pc_opener.py)
