"""Installations- und Update-Programm für das Buchhaltungsprogramm (wird zu „Buchhaltung-Installer.exe“ gebaut).

Ablauf:
1. Ordner wählen (beim ersten Mal; danach merkt sich das Programm den Ordner).
2. Neueste Version als ZIP von GitHub laden (öffentliches Repository, kein Passwort nötig).
3. Programmdateien einspielen. NIE angefasst werden: ``data`` (Datenbank, Belege, Sicherungen), ``.env``
   (Schlüssel), ``.venv`` (Python-Umgebung) und alles, was nicht von einer früheren Installation stammt.
   Vor einem Update wird der bisherige Programmstand als ``alte_version\\programm_vorher.zip`` aufgehoben.
4. Python einrichten: vorhandenes Python 3.12+ verwenden, sonst die offizielle Version von python.org laden.
5. Pakete installieren, Verknüpfung auf dem Desktop anlegen, auf Wunsch starten.
Die Datensicherung der Daten macht das Programm selbst bei jedem Start (vor dem Datenbank-Update).

Nur Standardbibliothek – damit die .exe klein bleibt. Bauen: ``installer\\bauen.bat``.
Test ohne Internet: ``python buchhaltung_installer.py --zip pfad\\zum.zip --ordner C:\\Test --ja --ohne-python``.
"""
import argparse
import json
import re
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = "Fwlho/-buchhaltung"
BRANCH = "main"
ZIP_URL = f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip"
PYTHON_VERSION = "3.14.0"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-amd64.exe"
MIN_PYTHON = (3, 12)
DEFAULT_DIR = Path("C:/Buchhaltung")
CONFIG = Path(os.environ.get("APPDATA", str(Path.home()))) / "Buchhaltung-Installer" / "einstellungen.json"
MANIFEST = ".installiert.json"            # welche Einträge im Ordner vom Installer stammen
KEEP = {"data", ".env", ".venv", "alte_version", MANIFEST}
PORT = 8000
MAX_DIR_LEN = 60                          # längere Pfade scheitern an der Windows-Pfadgrenze (260 Zeichen)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Abbruch(Exception):
    """Verständlicher Fehler für den Benutzer."""


# ------------------------------------------------------------------ Ausgabe
def say(text: str = "") -> None:
    print(text, flush=True)


def ask_yes(question: str, auto: bool) -> bool:
    if auto:
        return True
    answer = _input(f"{question} [J/n] ").lower()
    return answer in ("", "j", "ja", "y", "yes")


def kept(name: str) -> bool:
    return name in KEEP or name.lower().startswith("data")


# --------------------------------------------------------------- Einstellung
def remembered_dir() -> Path | None:
    try:
        return Path(json.loads(CONFIG.read_text(encoding="utf-8-sig"))["ordner"])
    except (OSError, ValueError, KeyError):
        return None


def remember_dir(path: Path) -> None:
    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps({"ordner": str(path)}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _input(prompt: str) -> str:
    """Eingabe ohne Anführungszeichen und ohne unsichtbare Steuerzeichen vorn (z. B. BOM aus PowerShell).
    Ist keine Eingabe mehr möglich (Fenster ohne Tastatur), gilt das wie Enter."""
    try:
        raw = input(prompt)
    except EOFError:
        return ""
    raw = raw.replace("\ufeff", "").strip().strip('"')
    drive = re.search(r"[A-Za-z]:[\\/]", raw)               # Pfad: ab dem Laufwerksbuchstaben
    if drive:
        return raw[drive.start():]
    return raw if raw.isascii() else "".join(ch for ch in raw if ch.isascii() and ch.isprintable())


def _ask_folder(suggestion: Path) -> Path:
    while True:
        answer = _input(f"In welchen Ordner soll das Programm? [Enter = {suggestion}] ")
        if not answer:
            return suggestion
        folder = Path(answer)
        if folder.is_absolute() and folder.drive:
            return folder
        say("Bitte den vollständigen Pfad mit Laufwerk angeben, z. B. C:\\Buchhaltung oder D:\\Programme\\Buchhaltung.")


def _free_suggestion(known: Path | None) -> Path:
    """Vorschlag für einen neuen Ordner, in dem noch keine Installation liegt."""
    candidate, n = DEFAULT_DIR, 1
    while (candidate / "run.py").exists() or (known and candidate.resolve() == known.resolve()):
        n += 1
        candidate = DEFAULT_DIR.with_name(f"{DEFAULT_DIR.name}{n}")
    return candidate


def choose_dir(arg: str | None, auto: bool) -> Path:
    """Ordner wählen: vorhandene Installation aktualisieren ODER in einen neuen Ordner installieren."""
    if arg:
        return Path(arg)
    known = remembered_dir()
    if not (known and (known / "run.py").exists()):
        return DEFAULT_DIR if auto else _ask_folder(DEFAULT_DIR)
    if auto:
        return known
    say(f"Vorhandene Installation gefunden: {known}")
    say("  1 = diese Installation aktualisieren (Daten bleiben erhalten)")
    say("  2 = in einen NEUEN Ordner installieren")
    while True:
        choice = _input("Bitte 1 oder 2 eingeben [Enter = 1]: ")
        if choice in ("", "1"):
            return known
        if choice == "2":
            folder = _ask_folder(_free_suggestion(known))
            if (folder / "run.py").exists():
                say(f"Hinweis: In {folder} liegt schon eine Installation – sie wird aktualisiert.")
            return folder
        say("Bitte nur 1 oder 2 eingeben.")


# ----------------------------------------------------------------- Laden
def download(url: str, target: Path, what: str) -> None:
    say(f"Lade {what} …")
    request = urllib.request.Request(url, headers={"User-Agent": "Buchhaltung-Installer"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(target, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while chunk := response.read(256 * 1024):
                out.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  {done * 100 // total:3d} %", end="", flush=True)
        if total:
            say()
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and "github.com" in url:
            raise Abbruch("Das Programm wurde auf GitHub nicht gefunden. Ist das Repository "
                          f"github.com/{REPO} öffentlich („Public“) und hat es den Zweig „{BRANCH}“?") from exc
        raise Abbruch(f"Herunterladen fehlgeschlagen ({exc.code}): {url}") from exc
    except OSError as exc:
        raise Abbruch(f"Keine Verbindung zum Internet oder Server nicht erreichbar: {exc}") from exc


# ------------------------------------------------------- Programmdateien
def program_running() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def wait_until_closed(auto: bool) -> None:
    while program_running():
        if auto:
            raise Abbruch("Das Programm läuft noch – bitte erst beenden.")
        _input("Das Buchhaltungsprogramm läuft gerade. Bitte sein Fenster schließen und dann Enter drücken … ")
        time.sleep(1)


def _installed_entries(folder: Path) -> list[str]:
    try:
        return list(json.loads((folder / MANIFEST).read_text(encoding="utf-8"))["eintraege"])
    except (OSError, ValueError, KeyError):
        return []


def _backup_old(folder: Path, entries: list[str]) -> None:
    """Bisherigen Programmstand als ZIP aufheben (immer nur der letzte)."""
    target_dir = folder / "alte_version"
    target_dir.mkdir(exist_ok=True)
    target = target_dir / "programm_vorher.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for name in entries:
            path = folder / name
            if path.is_file():
                z.write(path, name)
            elif path.is_dir():
                for sub in path.rglob("*"):
                    if sub.is_file() and "__pycache__" not in sub.parts:
                        z.write(sub, sub.relative_to(folder).as_posix())
    say(f"Bisheriger Programmstand aufgehoben: {target}")


def install_code(archive: Path, folder: Path, auto: bool) -> bool:
    """Spielt die Programmdateien ein. Gibt True zurück, wenn es ein Update war."""
    folder.mkdir(parents=True, exist_ok=True)
    old = _installed_entries(folder)
    update = bool(old) or (folder / "run.py").exists()
    if update and not old:                    # Ordner ohne Liste (z. B. von Hand kopiert): nur Programmteile
        old = [p.name for p in folder.iterdir() if not kept(p.name)]
    if not update:
        foreign = [p.name for p in folder.iterdir() if not kept(p.name)]
        if foreign and not ask_yes(f"Der Ordner {folder} ist nicht leer ({len(foreign)} Einträge). Trotzdem "
                                   "hineininstallieren? Vorhandene Dateien bleiben, gleichnamige werden ersetzt.", auto):
            raise Abbruch("Abgebrochen – bitte einen leeren Ordner wählen.")

    if not zipfile.is_zipfile(archive):
        raise Abbruch("GitHub hat kein Programmpaket geliefert. Ist das Repository "
                      f"github.com/{REPO} öffentlich („Public“) und enthält es den Zweig „{BRANCH}“?")
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(archive) as z:
            z.extractall(tmp)
        roots = [p for p in Path(tmp).iterdir()]
        root = roots[0] if len(roots) == 1 and roots[0].is_dir() else Path(tmp)
        if not (root / "run.py").exists():
            raise Abbruch("Das heruntergeladene Paket enthält kein Buchhaltungsprogramm (run.py fehlt).")
        new = [p.name for p in root.iterdir() if not kept(p.name)]
        if update:
            wait_until_closed(auto)
            _backup_old(folder, old)
            for name in old:
                if kept(name):
                    continue
                path = folder / name
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                elif path.exists():
                    path.unlink()
        for name in new:
            src, dst = root / name, folder / name
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
    (folder / MANIFEST).write_text(json.dumps({"eintraege": new, "quelle": ZIP_URL,
                                               "installiert": time.strftime("%d.%m.%Y %H:%M")},
                                              ensure_ascii=False, indent=1), encoding="utf-8")
    return update


# ------------------------------------------------------------------ Python
def _version_of(command: list[str]) -> tuple[int, int] | None:
    try:
        out = subprocess.run([*command, "-c", "import sys;print(sys.version_info[0], sys.version_info[1])"],
                             capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW)
        if out.returncode == 0:
            major, minor = out.stdout.split()
            return int(major), int(minor)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def find_python() -> list[str] | None:
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
    candidates = [["py", f"-3.{m}"] for m in (14, 13, 12)]
    candidates += [[str(p)] for p in sorted(local.glob("Python3*/python.exe"), reverse=True)]
    candidates += [["python"]]
    for command in candidates:
        version = _version_of(command)
        if version and version >= MIN_PYTHON:
            return command
    return None


def ensure_python(auto: bool) -> list[str]:
    command = find_python()
    if command:
        return command
    if not ask_yes(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} oder neuer fehlt. Jetzt Python {PYTHON_VERSION} "
                   "von python.org installieren (nur für Ihren Benutzer)?", auto):
        raise Abbruch("Ohne Python kann das Programm nicht laufen.")
    with tempfile.TemporaryDirectory() as tmp:
        setup = Path(tmp) / f"python-{PYTHON_VERSION}-amd64.exe"
        download(PYTHON_URL, setup, f"Python {PYTHON_VERSION} von python.org")
        say("Installiere Python (dauert etwa eine Minute) …")
        result = subprocess.run([str(setup), "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_test=0",
                                 "Include_doc=0", "Include_launcher=1"])
        if result.returncode != 0:
            raise Abbruch(f"Die Python-Installation ist fehlgeschlagen (Code {result.returncode}).")
    command = find_python()
    if not command:
        raise Abbruch("Python wurde installiert, aber nicht gefunden – bitte den Rechner neu starten und den "
                      "Installer noch einmal ausführen.")
    return command


def setup_environment(folder: Path, python: list[str]) -> None:
    venv_python = folder / ".venv" / "Scripts" / "python.exe"
    if not venv_python.exists():
        say("Lege die Python-Umgebung an …")
        if subprocess.run([*python, "-m", "venv", str(folder / ".venv")]).returncode != 0:
            raise Abbruch("Die Python-Umgebung (.venv) konnte nicht angelegt werden.")
    say("Installiere bzw. aktualisiere die benötigten Pakete (beim ersten Mal einige Minuten) …")
    result = subprocess.run([str(venv_python), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                             "-r", str(folder / "requirements.txt")])
    if result.returncode != 0:
        raise Abbruch("Die Pakete konnten nicht installiert werden. Bitte die Internetverbindung prüfen; "
                      "bei der Meldung „Long Path“ einen kürzeren Ordner wählen (z. B. C:\\Buchhaltung).")


# --------------------------------------------------------------- Abschluss
def desktop_shortcut(folder: Path) -> None:
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
        "[Environment]::GetFolderPath('Desktop')+'\\Buchhaltung.lnk');"
        f"$s.TargetPath='{folder / 'start.bat'}';$s.WorkingDirectory='{folder}';"
        "$s.Description='Buchhaltungsprogramm starten';$s.Save()"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                            capture_output=True, creationflags=NO_WINDOW)
    say("Verknüpfung „Buchhaltung“ auf dem Desktop angelegt." if result.returncode == 0
        else "Hinweis: Die Desktop-Verknüpfung konnte nicht angelegt werden – start.bat im Ordner nutzen.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Buchhaltung installieren oder aktualisieren")
    parser.add_argument("--ordner", help="Installationsordner (ohne Nachfrage)")
    parser.add_argument("--zip", help="Programm aus dieser ZIP-Datei statt von GitHub (zum Testen)")
    parser.add_argument("--ja", action="store_true", help="alle Fragen mit Ja beantworten")
    parser.add_argument("--ohne-python", action="store_true", help="Python/Pakete/Verknüpfung überspringen")
    parser.add_argument("--ohne-verknuepfung", action="store_true", help="keine Desktop-Verknüpfung anlegen")
    args = parser.parse_args(argv)

    say("=" * 64)
    say("  Buchhaltung – Installation und Update")
    say(f"  Quelle: github.com/{REPO}")
    say("=" * 64)
    try:
        folder = choose_dir(args.ordner, args.ja).resolve()
        if len(str(folder)) > MAX_DIR_LEN and not ask_yes(
                f"Der Ordnerpfad ist sehr lang ({len(str(folder))} Zeichen). Windows kann dann manche Programmteile "
                "nicht anlegen. Trotzdem fortfahren? (Empfehlung: C:\\Buchhaltung)", args.ja):
            raise Abbruch("Abgebrochen – bitte einen kürzeren Ordner wählen.")
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(args.zip) if args.zip else Path(tmp) / "programm.zip"
            if not args.zip:
                download(ZIP_URL, archive, "die neueste Programmversion von GitHub")
            update = install_code(archive, folder, args.ja)
        remember_dir(folder)
        say(("Update" if update else "Installation") + f" der Programmdateien in {folder} fertig.")
        say("Ihre Daten (Ordner data) und Schlüssel (.env) wurden nicht verändert.")
        if not args.ohne_python:
            setup_environment(folder, ensure_python(args.ja))
            if not update and not args.ohne_verknuepfung:
                desktop_shortcut(folder)
        say()
        say("Fertig." + (" Beim nächsten Start sichert das Programm zuerst Ihre Daten und bringt die Datenbank "
                         "auf den neuen Stand." if update else ""))
        if not args.ohne_python and not args.ja and ask_yes("Programm jetzt starten?", False):
            subprocess.Popen(["cmd", "/c", "start", "Buchhaltung", str(folder / "start.bat")], cwd=folder)
        elif not args.ja:
            _input("Enter drücken zum Schließen … ")
        return 0
    except Abbruch as exc:
        say()
        say(f"FEHLER: {exc}")
        if not args.ja:
            _input("Enter drücken zum Schließen … ")
        return 1
    except KeyboardInterrupt:
        say("\nAbgebrochen.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
