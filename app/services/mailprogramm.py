"""Fertige E-Mail direkt im Mailprogramm öffnen (Thunderbird oder Outlook) – NUR lokal, nie versenden.

Das Programm öffnet ein neues, ausgefülltes Mail-Fenster (Empfänger, Betreff, Text, Anhänge). Senden
tut der Nutzer selbst. Auf dem Server (APP_ENV ≠ lokal) gibt es diese Knöpfe nicht – dort läuft kein
Mailprogramm auf demselben Rechner; stattdessen „Entwurf im Postfach anlegen“.

- Thunderbird: Aufruf `thunderbird.exe -compose "to=…,subject=…,body=…,attachment='file:///…'"`.
  Werte in doppelten Anführungszeichen werden von Thunderbird URL-dekodiert – wir kodieren sie mit
  `quote(…, safe="")`, damit Komma/Anführungszeichen im Text die Liste nicht aufbrechen können.
- Outlook (klassisch): .eml mit der Kopfzeile `X-Unsent: 1` → Outlook öffnet sie als ungesendete Mail
  mit Senden-Knopf (`OUTLOOK.EXE /eml <Datei>`).

Anhänge müssen dafür als normale Datei vorliegen: Sie liegen unverschlüsselt in einem eigenen Ordner
im Temp-Verzeichnis des Windows-Benutzers und werden nach `KEEP_SECONDS` beim nächsten Aufruf gelöscht.
"""
import copy
import re
import secrets
import shutil
import subprocess
import sys
import time
import tempfile
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote

from app.config import get_settings

PROGRAMS = {"thunderbird": "Thunderbird", "outlook": "Outlook"}
KEEP_SECONDS = 3 * 24 * 3600     # Mailprogramme lesen Anhänge teils erst beim Senden – daher nicht sofort löschen
_EXE = {"thunderbird": "thunderbird.exe", "outlook": "OUTLOOK.EXE"}
_KNOWN = {
    "thunderbird": [r"C:\Program Files\Mozilla Thunderbird\thunderbird.exe",
                    r"C:\Program Files (x86)\Mozilla Thunderbird\thunderbird.exe"],
    "outlook": [rf"C:\Program Files{x}\Microsoft Office\root\Office{v}\OUTLOOK.EXE"
                for x in ("", " (x86)") for v in ("16", "15")],
}


class MailProgramError(Exception):
    pass


def _registry_path(exe: str) -> str | None:
    """Pfad aus „App Paths“ der Windows-Registrierung (dort tragen sich Thunderbird und Office ein)."""
    if sys.platform != "win32":
        return None
    import winreg

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as key:
                value = winreg.QueryValue(key, None)
        except OSError:
            continue
        if value:
            return value.strip('"')
    return None


def find(program: str) -> str | None:
    exe = _EXE[program]
    for candidate in (_registry_path(exe), *_KNOWN[program], shutil.which(exe), shutil.which(program)):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def available() -> dict[str, str]:
    """Installierte Mailprogramme {kennung: Name} – leer auf dem Server."""
    if not get_settings().is_local:
        return {}
    return {key: label for key, label in PROGRAMS.items() if find(key)}


def _launch(args: list[str]) -> None:
    """Programm starten, ohne darauf zu warten (in Tests ersetzt)."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(args, close_fds=True, creationflags=flags, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _base_dir() -> Path:
    """Ordner für die Anhänge (in Tests ersetzt)."""
    return Path(tempfile.gettempdir()) / "buchhaltung_mail"


def _work_dir() -> Path:
    base = _base_dir()
    base.mkdir(exist_ok=True)
    now = time.time()
    for old in base.iterdir():          # alte Anhänge aufräumen
        try:
            if now - old.stat().st_mtime > KEEP_SECONDS:
                shutil.rmtree(old, ignore_errors=True) if old.is_dir() else old.unlink()
        except OSError:
            pass
    folder = base / secrets.token_hex(8)
    folder.mkdir()
    return folder


def _safe_name(name: str, fallback: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name or "").name).strip("._")
    return name or fallback


def _body(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain",))
    text = part.get_content() if part is not None else ""
    return text.replace("\r\n", "\n").rstrip("\n")     # set_content() hängt einen Zeilenumbruch an


def thunderbird_args(exe: str, msg: EmailMessage, files: list[Path]) -> list[str]:
    def enc(value: str) -> str:
        return '"' + quote(value or "", safe="") + '"'

    parts = []
    if msg["To"]:
        parts.append("to=" + enc(str(msg["To"])))
    parts.append("subject=" + enc(str(msg["Subject"] or "")))
    parts.append("body=" + enc(_body(msg).replace("\r\n", "\n")))
    if files:   # Dateinamen sind bereinigt (kein Komma/Anführungszeichen) → einfache Anführungszeichen
        parts.append("attachment='" + ",".join(f.as_uri() for f in files) + "'")
    return [exe, "-compose", ",".join(parts)]


def chosen(form) -> str | None:
    """Kennung des Mailprogramms aus dem Knopf „In … öffnen“ (sonst None = Entwurf im Postfach)."""
    value = form.get("aktion")
    return value if value in PROGRAMS else None


def opened_message(label: str) -> str:
    return f"Die E-Mail wurde in {label} geöffnet – bitte dort prüfen und selbst auf „Senden“ klicken."


def open_compose(program: str, msg: EmailMessage) -> str:
    """Öffnet die Mail im gewählten Programm. Gibt dessen Namen zurück; wirft MailProgramError."""
    if program not in PROGRAMS:
        raise MailProgramError("Unbekanntes Mailprogramm.")
    if not get_settings().is_local:
        raise MailProgramError("Direkt öffnen geht nur, wenn das Programm auf Ihrem eigenen Rechner läuft.")
    exe = find(program)
    if exe is None:
        raise MailProgramError(f"{PROGRAMS[program]} wurde auf diesem Rechner nicht gefunden.")
    folder = _work_dir()
    if program == "thunderbird":
        files = []
        for i, part in enumerate(msg.iter_attachments()):
            path = folder / _safe_name(part.get_filename(), f"anhang-{i + 1}")
            path.write_bytes(part.get_payload(decode=True))
            files.append(path)
        args = thunderbird_args(exe, msg, files)
    else:
        unsent = copy.deepcopy(msg)
        for key in ("From", "Date", "Message-ID"):     # Absender/Datum setzt Outlook selbst (Standardkonto)
            del unsent[key]
        unsent["X-Unsent"] = "1"
        name = _safe_name(str(msg["Subject"] or ""), "E-Mail")[:80] + ".eml"
        path = folder / name
        path.write_bytes(bytes(unsent))
        args = [exe, "/eml", str(path)]
    try:
        _launch(args)
    except OSError as exc:
        raise MailProgramError(f"{PROGRAMS[program]} ließ sich nicht starten: {exc}") from exc
    return PROGRAMS[program]
