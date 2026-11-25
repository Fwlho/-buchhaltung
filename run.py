"""Startet das Programm lokal.

0. Installiert fehlende Pakete aus requirements.txt nach (z. B. nach einem Programm-Update).
1. Legt beim ersten Start die Datei .env mit zufälligen Schlüsseln an.
2. Legt eine Datensicherung an (nur wenn sich seit der letzten etwas geändert hat).
3. Bringt die Datenbank auf den neuesten Stand (alembic upgrade head) und verschlüsselt noch offene Belege.
   Beendet alle alten Anmeldungen (nach dem Neustart neu anmelden, Admin-Bereiche neu freischalten).
4. Startet den Webserver auf http://127.0.0.1:8000
"""
import secrets
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
EXAMPLE_PATH = BASE_DIR / ".env.example"


def missing_requirements() -> list[str]:
    """Pakete aus requirements.txt, die in dieser Python-Umgebung fehlen (Version wird nicht geprüft)."""
    import re
    from importlib.metadata import PackageNotFoundError, version

    missing = []
    for line in (BASE_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name = re.split(r"[\[<>=!~; ]", line, maxsplit=1)[0]
        try:
            version(name)
        except PackageNotFoundError:
            missing.append(line)
    return missing


def ensure_requirements() -> None:
    missing = missing_requirements()
    if not missing:
        return
    import subprocess

    print("Installiere fehlende Programmteile: " + ", ".join(missing) + " …")
    result = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *missing])
    # Python merkt sich beim Start, welche Pakete es gibt – ohne das hier fehlen frisch installierte Pakete
    # bis zum nächsten Neustart (z. B. „No module named 'pdfplumber'“ beim Einlesen von Belegen).
    import importlib

    importlib.invalidate_caches()
    if result.returncode != 0:
        print("Die Installation ist fehlgeschlagen (Internetverbindung?). Das Programm startet trotzdem;")
        print("Funktionen, die diese Teile brauchen (z. B. KI-Vorschläge), sind dann nicht verfügbar.")


def ensure_env_file() -> None:
    if ENV_PATH.exists():
        return
    from cryptography.fernet import Fernet

    text = EXAMPLE_PATH.read_text(encoding="utf-8")
    text = text.replace("SECRET_KEY=bitte-ersetzen", f"SECRET_KEY={secrets.token_urlsafe(48)}")
    text = text.replace("ENCRYPTION_KEY=bitte-ersetzen", f"ENCRYPTION_KEY={Fernet.generate_key().decode()}")
    ENV_PATH.write_text(text, encoding="utf-8")
    print("Neue Datei .env mit zufälligen Schlüsseln angelegt.")
    print("WICHTIG: Die .env sicher aufbewahren – ohne sie lassen sich gespeicherte Zugangsdaten nicht entschlüsseln.")


def migrate() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    command.upgrade(cfg, "head")


def encrypt_files() -> None:
    """Noch unverschlüsselte Belege (aus der Zeit vor der Verschlüsselung) einmalig verschlüsseln."""
    from app.services import tresor

    tresor.main()


def end_sessions() -> None:
    """Nach jedem Start neu anmelden – alte Anmeldungen und Admin-Freischaltungen gelten nicht weiter."""
    from app.services import neustart

    neustart.main()


def backup() -> None:
    """Sicherung VOR der Migration – so lässt sich auch ein fehlgeschlagenes Update rückgängig machen."""
    from app.services import sicherung

    try:
        path = sicherung.create_local_backup()
    except Exception as exc:  # noqa: BLE001 – eine fehlgeschlagene Sicherung darf den Start nicht verhindern
        print(f"WARNUNG: Die Datensicherung ist fehlgeschlagen: {exc}")
        return
    if path is not None:
        print(f"Datensicherung angelegt: {path}")


def port_free(host: str, port: int) -> bool:
    """Prüft, ob die Adresse frei ist – sonst läuft meist noch das Programm in einem anderen Fenster."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def main() -> None:
    host = "127.0.0.1"
    port = 8000
    if not port_free(host, port):
        # Sonst öffnet sich der Browser beim ALTEN Programm und Änderungen wirken scheinbar nicht
        print(f"\nDas Programm läuft bereits (Adresse http://{host}:{port} ist belegt).")
        print("Bitte zuerst das alte schwarze Programmfenster schließen (oder dort Strg+C drücken)")
        print("und danach start.bat erneut starten.\n")
        sys.exit(1)
    ensure_requirements()
    ensure_env_file()
    from app.config import get_settings

    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    backup()
    migrate()
    encrypt_files()
    end_sessions()

    import uvicorn

    url = f"http://{host}:{port}"
    print(f"\nProgramm läuft: {url}  (Beenden mit Strg+C)\n")
    if "--browser" in sys.argv:
        import threading
        import webbrowser

        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("app.main:app", host=host, port=port, reload="--reload" in sys.argv)


if __name__ == "__main__":
    main()
