"""Beim Programmstart: alle Anmeldungen beenden (entschieden 25.11.2026).

Nach einem Neustart muss sich jede/r neu anmelden (Passwort + App-Code) und die Admin-Bereiche neu freischalten.
Aufruf lokal aus ``run.py``, auf dem Server im Start-Befehl des Docker-Containers (``python -m app.services.neustart``).
"""


def main() -> None:
    from app.database import SessionLocal
    from app.services import anmeldung

    with SessionLocal() as db:
        count = anmeldung.end_all_sessions(db)
    if count:
        print(f"Sicherheit: {count} Anmeldung(en) aus der letzten Sitzung beendet – bitte neu anmelden.")


if __name__ == "__main__":
    main()
