@echo off
rem Startet das Buchhaltungsprogramm per Doppelklick.
rem Beim ersten Mal wird eine eigene Python-Umgebung (.venv) angelegt.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Lege Python-Umgebung an ...
    python -m venv .venv || goto fehler
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt || goto fehler
)

".venv\Scripts\python.exe" run.py --browser
pause
exit /b 0

:fehler
echo.
echo Fehler beim Einrichten. Ist Python 3.12 oder neuer installiert?
pause
exit /b 1
