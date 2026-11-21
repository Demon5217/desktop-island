@echo off
chcp 65001 >nul
rem ============================================================
rem  Desktop Hub - run as administrator
rem  Why: per-process network traffic (ETW) needs admin rights.
rem  Normal 运行.bat -> network column shows connection count.
rem  This file       -> network column shows real up/down speed.
rem ============================================================

powershell -NoProfile -Command "Start-Process -FilePath 'C:\Program Files\Python313\pythonw.exe' -ArgumentList '\"%~dp0main.py\"' -WorkingDirectory '%~dp0' -Verb RunAs"
exit /b
