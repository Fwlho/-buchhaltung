"""Verschärfte Anmelde-Regeln (entschieden 25.11.2026): nach Neustart neu anmelden, Abmeldung nach 30 Min. ohne
Aktivität, Admin-Bereiche nach 5 Min. ohne Nutzung gesperrt, Authenticator-App ist Pflicht."""
import dataclasses
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.config import get_settings
from app.database import utcnow
from app.models import User, UserSession
from app.services import admin_freigabe, anmeldung, neustart, totp
from tests.conftest import PASSWORD, complete_wizard, ersteinrichtung, post
from tests.test_rechte import _invite


def _ready(client):
    ersteinrichtung(client)
    complete_wizard(client)


def _sessions(db) -> list[UserSession]:
    db.expire_all()
    return list(db.scalars(select(UserSession)))


def _age_sessions(db, **values) -> None:
    db.execute(update(UserSession).values(**values))
    db.commit()


def test_zeiten():
    assert anmeldung.SESSION_IDLE == timedelta(minutes=30)
    assert admin_freigabe.IDLE == timedelta(minutes=5)


def test_neustart_meldet_alle_ab(client, db):
    _ready(client)
    assert client.get("/", follow_redirects=False).status_code == 200
    neustart.main()
    assert not _sessions(db)
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 303) and r.headers["location"] == "/login"


def test_abmeldung_nach_30_minuten_ohne_aktivitaet(client, db):
    _ready(client)
    page = client.get("/").text
    assert '<meta http-equiv="refresh" content="1805;url=/abgelaufen?weiter=%2F">' in page
    # Nachsehen verlängert die Sitzung NICHT (das automatische Neuladen ist keine Aktivität)
    ten_minutes_ago = utcnow() - timedelta(minutes=10)
    _age_sessions(db, last_seen_at=ten_minutes_ago)
    r = client.get("/abgelaufen?weiter=/rechnungen", follow_redirects=False)
    assert r.headers["location"] == "/rechnungen"
    assert abs((_sessions(db)[0].last_seen_at - ten_minutes_ago).total_seconds()) < 1
    # 31 Minuten nichts getan -> abgemeldet
    _age_sessions(db, last_seen_at=utcnow() - timedelta(minutes=31))
    r = client.get("/abgelaufen?weiter=/rechnungen")
    assert "nach 30 Minuten ohne Aktivität abgemeldet" in r.text and 'name="password"' in r.text
    assert client.get("/rechnungen", follow_redirects=False).headers["location"] == "/login"
    # nur Ziele innerhalb des Programms
    post(client, "/login", {"email": "chefin@example.org", "password": PASSWORD})
    r = client.get("/abgelaufen?weiter=https://boese.example", follow_redirects=False)
    assert r.headers["location"] in ("/", "/login", "/login/code")


def test_admin_bereiche_nach_5_minuten_gesperrt(client, db):
    _ready(client)
    page = client.get("/einstellungen/firma").text
    assert 'content="305;url=/abgelaufen?weiter=%2Feinstellungen%2Ffirma&amp;bereich=admin"' in page
    assert 'content="1805;' in client.get("/rechnungen").text          # normale Seiten: 30 Minuten
    _age_sessions(db, admin_until=utcnow() - timedelta(seconds=1))
    r = client.get("/abgelaufen?weiter=/einstellungen/firma&bereich=admin", follow_redirects=False)
    assert r.headers["location"].startswith("/admin-passwort?weiter=%2Feinstellungen%2Ffirma")
    r = client.get("/einstellungen/firma", follow_redirects=False)
    assert r.headers["location"].startswith("/admin-passwort")
    assert client.get("/rechnungen", follow_redirects=False).status_code == 200   # nur Admin-Bereiche gesperrt


@pytest.fixture
def app_pflicht(monkeypatch):
    """Pflicht einschalten (in den übrigen Tests ist sie aus, siehe conftest)."""
    def enable():
        strict = dataclasses.replace(get_settings(), totp_required=True)
        monkeypatch.setattr("app.deps.get_settings", lambda: strict)
        monkeypatch.setattr("app.routers.start.get_settings", lambda: strict)
    return enable


def _setup_app(c, db, email: str) -> None:
    r = post(c, "/konto/app/einrichten", {"password": PASSWORD})
    assert 'src="/konto/app/qr.svg"' in r.text
    db.expire_all()
    secret = totp.secret_of(db.scalar(select(User).where(User.email == email)))
    r = post(c, "/konto/app/bestaetigen", {"code": totp.code_at(secret, totp.current_step())})
    assert "Notfall-Codes" in r.text and "Weiter zum Programm" in r.text


def test_authenticator_app_ist_pflicht(client, db, app_pflicht):
    _ready(client)
    app_pflicht()
    r = client.get("/rechnungen", follow_redirects=False)
    assert r.headers["location"] == "/konto?app=pflicht"
    page = client.get("/konto?app=pflicht").text
    assert "Bitte zuerst die Anmeldung per Authenticator-App einrichten" in page
    assert post(client, "/rechnungen/neu", {"customer_id": "1"}, follow_redirects=False).status_code == 303
    _setup_app(client, db, "chefin@example.org")
    r = client.get("/rechnungen", follow_redirects=False)
    assert r.status_code == 200, r.headers.get("location")
    assert "Bitte zuerst" not in client.get("/konto").text


def test_inhaberin_setzt_app_eines_mitarbeiters_zurueck(client, outbox, db, app_pflicht):
    _ready(client)
    app_pflicht()
    _setup_app(client, db, "chefin@example.org")
    c = _invite(client, outbox, "max@example.org", "mitarbeiter")
    assert c.get("/", follow_redirects=False).headers["location"] == "/konto?app=pflicht"
    _setup_app(c, db, "max@example.org")
    assert c.get("/", follow_redirects=False).status_code == 200
    from app.models import Membership

    max_user = db.scalar(select(User).where(User.email == "max@example.org"))
    m = db.scalar(select(Membership).where(Membership.user_id == max_user.id))
    assert "Authenticator-App zurücksetzen" in client.get(f"/benutzer/{m.id}").text
    r = post(client, f"/benutzer/{m.id}/app-zuruecksetzen")
    assert "zurückgesetzt" in r.text
    db.expire_all()
    assert not totp.enabled(db.get(User, max_user.id))
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"   # Max ist abgemeldet


def test_neue_firma_richtet_zuerst_die_app_ein(client, db, app_pflicht):
    app_pflicht()
    ersteinrichtung(client)
    assert client.get("/einrichtung", follow_redirects=False).headers["location"] == "/konto?app=pflicht"
    _setup_app(client, db, "chefin@example.org")
    complete_wizard(client)
    assert client.get("/rechnungen", follow_redirects=False).status_code == 200
