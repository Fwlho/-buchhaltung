"""Test-Umgebung: eigene, temporäre Datenbank und Ablage – niemals data/ des Programms.

Standard ist SQLite. Gegen PostgreSQL testen (wie auf dem Server):
    set TEST_DATABASE_URL=postgresql+psycopg://benutzer:passwort@localhost:5432/buchhaltung_test
Der Datenbankname MUSS „test“ enthalten – die Tests leeren vor jedem Test alle Tabellen.
"""
import os
import re
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

_TMP = Path(tempfile.mkdtemp(prefix="buchhaltung-test-"))
_PG_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
if _PG_URL and "test" not in _PG_URL.rsplit("/", 1)[-1]:
    raise SystemExit("TEST_DATABASE_URL muss auf eine Datenbank mit „test“ im Namen zeigen (Schutz echter Daten).")
os.environ["APP_ENV"] = "lokal"
os.environ["DATABASE_URL"] = _PG_URL or f"sqlite:///{(_TMP / 'test.db').as_posix()}"
IS_SQLITE = not _PG_URL
os.environ["DATA_DIR"] = str(_TMP / "data")
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["SMTP_HOST"] = ""
os.environ["SETUP_TOKEN"] = ""
os.environ["ZWEI_FAKTOR_PFLICHT"] = "false"  # eigene Tests schalten die Pflicht ein (test_sicherheit_sitzung.py)
os.environ["BACKGROUND_JOBS"] = "false"  # Hintergrund-Abruf in Tests nicht automatisch starten
os.environ.pop("ANTHROPIC_API_KEY", None)  # Tests dürfen nie die echte KI-Schnittstelle erreichen

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import main  # noqa: E402
from app.config import BASE_DIR  # noqa: E402
from app.database import Base, SessionLocal, get_engine  # noqa: E402
from app.services import mail  # noqa: E402

PASSWORD = "geheim-passwort-1"
ADMIN_PASSWORD = "admin-passwort-2"


@pytest.fixture(scope="session", autouse=True)
def _migrated_db():
    """Datenbank einmal per Alembic aufbauen – testet zugleich die Migration."""
    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    cfg.attributes["configure_logger"] = False
    if not IS_SQLITE:  # PostgreSQL: mit leerer Datenbank beginnen
        with get_engine().begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    command.upgrade(cfg, "head")
    yield
    get_engine().dispose()


@pytest.fixture(autouse=True)
def _clean_db():
    """Vor jedem Test alle Tabellen leeren."""
    with get_engine().begin() as conn:
        if IS_SQLITE:
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())
            conn.execute(text("PRAGMA foreign_keys=ON"))
        else:
            names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
            conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    main._setup_done = False
    yield


@pytest.fixture(autouse=True)
def _kein_mailprogramm(monkeypatch):
    """Tests sehen kein installiertes Thunderbird/Outlook und starten nie ein echtes Programm
    (eigene Nachbildung in tests/test_mailprogramm.py)."""
    from app.services import mailprogramm

    def blocked(args):
        raise AssertionError(f"Test wollte ein echtes Mailprogramm starten: {args[:1]}")

    monkeypatch.setattr(mailprogramm, "find", lambda program: None)
    monkeypatch.setattr(mailprogramm, "_launch", blocked)


@pytest.fixture
def outbox(monkeypatch):
    """Fängt System-Mails ab (statt .eml-Dateien zu schreiben)."""
    sent: list = []

    def capture(msg, text_body):
        sent.append({"to": msg["To"], "subject": msg["Subject"], "text": text_body})
        return Path("abgefangen.eml")

    monkeypatch.setattr(mail, "_write_outbox", capture)
    return sent


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


def new_client() -> TestClient:
    return TestClient(main.app)


@pytest.fixture
def client():
    with new_client() as c:
        yield c


# --------------------------------------------------------------- Helfer
def csrf(c: TestClient) -> str:
    if "csrftoken" not in c.cookies:
        c.get("/health")
    return c.cookies["csrftoken"]


def post(c: TestClient, url: str, data: dict | None = None, **kwargs):
    payload = dict(data or {})
    payload["csrf_token"] = csrf(c)
    return c.post(url, data=payload, **kwargs)


def code_from(outbox: list, email: str) -> str:
    mails = [m for m in outbox if m["to"] == email and "Bestätigungscode" in m["subject"]]
    assert mails, f"Kein Code-Mail an {email}"
    return re.search(r"\b(\d{6})\b", mails[-1]["text"]).group(1)


def link_from(outbox: list, email: str) -> str:
    mails = [m for m in outbox if m["to"] == email and "Einladung" in m["subject"]]
    assert mails, f"Keine Einladung an {email}"
    url = re.search(r"(http\S+/einladung/\S+)", mails[-1]["text"]).group(1)
    return url.replace("http://testserver", "")


def ersteinrichtung(c: TestClient, email: str = "chefin@example.org", firma: str = "Firma A"):
    c.get("/ersteinrichtung")
    r = post(c, "/ersteinrichtung", {"name": "Chefin", "email": email, "password": PASSWORD,
                                      "password2": PASSWORD, "admin_password": ADMIN_PASSWORD,
                                      "admin_password2": ADMIN_PASSWORD, "firma": firma})
    assert r.status_code == 200, r.text
    return r


def login(c: TestClient, outbox: list, email: str, password: str = PASSWORD, admin: bool = True):
    """Anmelden (Passwort + Code). ``admin``: danach auch die Admin-Bereiche freigeben."""
    c.get("/login")
    r = post(c, "/login", {"email": email, "password": password})
    assert r.status_code == 200, r.text
    r = post(c, "/login/code", {"code": code_from(outbox, email)})
    assert r.status_code == 200, r.text
    if admin:
        admin_freigabe(c, password)
    return r


def admin_freigabe(c: TestClient, password: str = PASSWORD, admin_password: str = ADMIN_PASSWORD):
    """Admin-Passwort eingeben – bzw. beim ersten Mal festlegen (mit dem Anmelde-Passwort)."""
    page = c.get("/admin-passwort").text
    if "Admin-Passwort festlegen" in page:
        r = post(c, "/admin-passwort/festlegen", {"login_password": password, "password": admin_password,
                                                  "password2": admin_password})
    else:
        r = post(c, "/admin-passwort", {"password": admin_password})
    assert r.status_code == 200, r.text
    return r


VALID_SETTINGS = {
    "firma": {"firma.name": "Muster Service", "firma.rechtsform": "Einzelunternehmen", "firma.inhaber": "Erika Muster",
              "firma.strasse": "Teststraße 1", "firma.plz": "12345", "firma.ort": "Teststadt",
              "firma.land": "Deutschland"},
    "steuer": {"steuer.steuernummer": "12/345/67890", "steuer.versteuerung": "ist",
               "steuer.voranmeldung": "quartalsweise"},
    "bank": {"bank.kontoinhaber": "Erika Muster", "bank.iban": "DE89 3704 0044 0532 0130 00"},
    "konten": {"konten.rahmen": "SKR03"},
    "nummern": {"nummern.rechnung.format": "{JJJJ}-{NNN}", "nummern.rechnung.start": "1",
                "nummern.angebot.format": "A-{JJJJ}-{NNN}", "nummern.angebot.start": "1",
                "nummern.mahnung.format": "M-{JJJJ}-{NNN}", "nummern.mahnung.start": "1",
                "nummern.storno.format": "S-{JJJJ}-{NNN}", "nummern.storno.start": "1",
                "nummern.auftrag.format": "AU-{JJJJ}-{NNN}", "nummern.auftrag.start": "1",
                "nummern.kunde.start": "10000", "nummern.lieferant.start": "70000"},
    "design": {"design.akzentfarbe": "#1f5fbf", "design.zahlungsziel_tage": "14", "design.ust_satz": "19",
               "design.danke_text": "Danke!", "rechnung.zahlungs_qr": "girocode", "rechnung.e_rechnung": "zugferd",
               "rechnung.einleitung": "Für unsere Leistungen berechnen wir Ihnen:"},
    "angebote": {"angebot.gueltig_tage": "30", "angebot.einleitung": "Gerne bieten wir an:",
                 "angebot.schluss": "Wir freuen uns auf Ihren Auftrag."},
    "email": {},
}


def complete_wizard(c: TestClient, overrides: dict | None = None):
    for group, values in VALID_SETTINGS.items():
        data = {**values, **((overrides or {}).get(group, {}))}
        r = post(c, f"/einrichtung/{group}", data)
        assert r.status_code == 200, (group, r.text)
    r = post(c, "/einrichtung/abschluss")
    assert r.status_code == 200
    return r


def read_stored(stored) -> bytes:
    """Gespeicherte Datei entschlüsselt lesen (auf der Platte liegt sie verschlüsselt, app/services/tresor.py)."""
    from app.config import get_settings
    from app.services import tresor

    with SessionLocal() as session:
        blob = (get_settings().files_dir / stored.path).read_bytes()
        return tresor.decrypt(session, stored.tenant_id, stored.path, blob)
