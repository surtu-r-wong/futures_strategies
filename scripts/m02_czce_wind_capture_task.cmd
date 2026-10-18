@echo off
rem M02 one-shot CZCE capture wrapper: the schtasks /IT session-1 vehicle.
rem Pushed to C:\ProgramData\MarketMonitor\futures-minute\m02\ and launched by
rem scripts/m02_czce_wind_capture.py; args pass through to the host worker.
set "PYTHON=C:\Users\ghls\AppData\Local\Programs\Python\Python312\python.exe"
set "ROOT=C:\ProgramData\MarketMonitor\futures-minute"
set "STAGE=%ROOT%\staging\domestic-no-night-20261014-v1\data-collecter"
set "PYTHONPATH=%STAGE%"
cd /d "%STAGE%"
"%PYTHON%" "%ROOT%\m02\m02_czce_wind_capture_host.py" %* > "%ROOT%\m02\last-run-stdout.txt" 2>&1
echo exit=%ERRORLEVEL% >> "%ROOT%\m02\last-run-stdout.txt"
