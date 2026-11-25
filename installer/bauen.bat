@echo off
rem Baut "Buchhaltung-Installer.exe" (Ergebnis: installer\dist\Buchhaltung-Installer.exe).
rem Braucht Python 3.12+ auf diesem Rechner; PyInstaller wird in einer eigenen Umgebung installer\.bau abgelegt.
cd /d "%~dp0"
if not exist ".bau\Scripts\python.exe" (
    python -m venv .bau || goto fehler
    ".bau\Scripts\python.exe" -m pip install --disable-pip-version-check -q pyinstaller || goto fehler
)
".bau\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --console --name Buchhaltung-Installer ^
    --distpath dist --workpath build --specpath build buchhaltung_installer.py || goto fehler
echo.
echo Fertig: %~dp0dist\Buchhaltung-Installer.exe
exit /b 0
:fehler
echo Fehler beim Bauen.
exit /b 1
