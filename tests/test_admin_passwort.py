"""Admin-Passwort (zweites Passwort für Admin-Bereiche) und Zugriffsmatrix."""
from datetime import timedelta

from sqlalchemy import select

from app.database import utcnow
from app.models import AuditLog, Membership, User, UserSession
from app.permissions import EDIT, NONE, READ
from app.services import admin_freigabe
from tests.conftest import (
    ADMIN_PASSWORD,
    PASSWORD,
    code_from,
    complete_wizard,
    ersteinrichtung,
    login,
    new_client,
    post,
)
from tests.test_rechte import _invite

ADMIN_BEREICHE = ["/einstellungen/firma", "/benutzer", "/benutzer/matrix", "/datensicherung", "/admin"]


def _setup(client):
    ersteinrichtung(client)
    complete_wizard(client)


def _fresh_login(outbox, email="chefin@example.org"):
    c = new_client()
    login(c, outbox, email, admin=False)
    return c


def _expire(db, email="chefin@example.org"):
    user = db.scalar(select(User).where(User.email == email))
    for sess in db.scalars(select(UserSession).where(UserSession.user_id == user.id)):
        sess.admin_until = utcnow() - timedelta(seconds=1)
    db.commit()


def test_ersteinrichtung_verlangt_eigenes_admin_passwort(client):
    client.get("/ersteinrichtung")
    base = {"name": "Chefin", "email": "chefin@example.org", "password": PASSWORD, "password2": PASSWORD,
            "firma": "Firma A"}
    r = post(client, "/ersteinrichtung", base)
    assert r.status_code == 400 and "mindestens 10 Zeichen" in r.text
    r = post(client, "/ersteinrichtung", {**base, "admin_password": PASSWORD, "admin_password2": PASSWORD})
    assert r.status_code == 400 and "muss sich vom Anmelde-Passwort unterscheiden" in r.text
    ersteinrichtung(client)
    # direkt nach der Ersteinrichtung freigegeben (Passwort wurde gerade vergeben)
    complete_wizard(client)
    assert client.get("/einstellungen/firma").status_code == 200


def test_admin_bereiche_nur_nach_freigabe(client, outbox, db):
    _setup(client)
    c = _fresh_login(outbox)
    assert c.get("/rechnungen").status_code == 200   # Alltagsarbeit ohne Admin-Passwort
    for url in ADMIN_BEREICHE:
        r = c.get(url, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/admin-passwort?weiter="), url

    r = c.get("/einstellungen/bank")
    assert "Bitte geben Sie Ihr <strong>Admin-Passwort</strong> ein" in r.text
    r = post(c, "/admin-passwort", {"password": PASSWORD, "weiter": "/einstellungen/bank"})
    assert r.status_code == 401 and "Admin-Passwort ist falsch" in r.text
    r = post(c, "/admin-passwort", {"password": ADMIN_PASSWORD, "weiter": "/einstellungen/bank"},
             follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/einstellungen/bank"
    for url in ADMIN_BEREICHE:
        assert c.get(url, follow_redirects=False).status_code == 200, url
    assert db.scalar(select(AuditLog).where(AuditLog.action == "admin_freigegeben")) is not None

    # Freigabe gilt nur für diese Anmeldung
    other = _fresh_login(outbox)
    assert other.get("/einstellungen/firma", follow_redirects=False).status_code == 303


def test_abgelaufene_freigabe_verwirft_formular(client, outbox, db):
    _setup(client)
    _expire(db)
    r = post(client, "/einstellungen/firma", {"firma.name": "Gehackt"}, follow_redirects=False,
             headers={"referer": "http://testserver/einstellungen/firma"})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin-passwort?weiter=%2Feinstellungen%2Ffirma&verworfen=1"
    assert "Gehackt" not in client.get("/").text
    assert "nicht gespeichert" in client.get(r.headers["location"]).text


def test_fehlversuche_sperren(client, outbox):
    _setup(client)
    c = _fresh_login(outbox)
    for _ in range(admin_freigabe.MAX_FAILURES):
        post(c, "/admin-passwort", {"password": "falsch-falsch"})
    r = post(c, "/admin-passwort", {"password": ADMIN_PASSWORD})
    assert r.status_code == 429 and "Zu viele Fehlversuche" in r.text
    assert c.get("/einstellungen/firma", follow_redirects=False).status_code == 303


def test_eingeladene_person_legt_admin_passwort_fest(client, outbox, db):
    _setup(client)
    post(client, "/benutzer/einladen", {"name": "Bea", "email": "bea@example.org", "role": "inhaber"})
    from tests.conftest import link_from
    c = new_client()
    c.get(link_from(outbox, "bea@example.org"))
    post(c, link_from(outbox, "bea@example.org"), {"name": "Bea", "password": PASSWORD, "password2": PASSWORD})

    r = c.get("/einstellungen/firma")
    assert "Admin-Passwort festlegen" in r.text
    r = post(c, "/admin-passwort/festlegen", {"login_password": "falsch-falsch", "password": "anderes-pw-123",
                                             "password2": "anderes-pw-123", "weiter": "/einstellungen/firma"})
    assert r.status_code == 400 and "Anmelde-Passwort ist falsch" in r.text
    r = post(c, "/admin-passwort/festlegen", {"login_password": PASSWORD, "password": PASSWORD,
                                             "password2": PASSWORD, "weiter": "/einstellungen/firma"})
    assert r.status_code == 400 and "muss sich vom Anmelde-Passwort unterscheiden" in r.text
    r = post(c, "/admin-passwort/festlegen", {"login_password": PASSWORD, "password": "anderes-pw-123",
                                             "password2": "anderes-pw-123", "weiter": "/einstellungen/firma"})
    assert r.status_code == 200 and "Admin-Passwort ist festgelegt" in r.text and "Muster Service" in r.text
    bea = db.scalar(select(User).where(User.email == "bea@example.org"))
    db.refresh(bea)
    assert bea.admin_password_hash and bea.admin_password_hash != bea.password_hash
    # einmal festgelegt, kann es nicht ohne das alte Passwort erneut „festgelegt“ werden
    r = post(c, "/admin-passwort/festlegen", {"login_password": PASSWORD, "password": "noch-eins-12345",
                                             "password2": "noch-eins-12345"})
    assert "Bitte geben Sie Ihr <strong>Admin-Passwort</strong> ein" in r.text


def test_admin_passwort_vergessen(client, outbox, db):
    _setup(client)
    c = _fresh_login(outbox)
    r = post(c, "/admin-passwort/vergessen/code", {"weiter": "/einstellungen/firma"})
    assert "Bestätigungscode aus der E-Mail" in r.text
    code = code_from(outbox, "chefin@example.org")
    data = {"code": code, "login_password": PASSWORD, "password": "neues-admin-pw-1",
            "password2": "neues-admin-pw-1", "weiter": "/einstellungen/firma"}
    r = post(c, "/admin-passwort/vergessen", {**data, "login_password": "falsch-falsch"})
    assert r.status_code == 400 and "Anmelde-Passwort ist falsch" in r.text
    r = post(c, "/admin-passwort/vergessen", data)   # Code wurde beim Fehlversuch nicht verbraucht
    assert r.status_code == 200 and "neues Admin-Passwort" in r.text and "Muster Service" in r.text
    # andere Sitzung braucht das neue Passwort
    other = _fresh_login(outbox)
    assert post(other, "/admin-passwort", {"password": ADMIN_PASSWORD}).status_code == 401
    assert post(other, "/admin-passwort", {"password": "neues-admin-pw-1"}).status_code == 200


def test_konto_admin_passwort_aendern_und_sperren(client, outbox):
    _setup(client)
    r = post(client, "/konto/admin-passwort", {"old_admin_password": "falsch-falsch",
                                               "admin_password": "neues-admin-pw-1", "admin_password2": "neues-admin-pw-1"})
    assert r.status_code == 400 and "bisherige Admin-Passwort ist falsch" in r.text
    r = post(client, "/konto/admin-passwort", {"old_admin_password": ADMIN_PASSWORD,
                                               "admin_password": PASSWORD, "admin_password2": PASSWORD})
    assert r.status_code == 400 and "unterscheiden" in r.text
    r = post(client, "/konto/admin-passwort", {"old_admin_password": ADMIN_PASSWORD,
                                               "admin_password": "neues-admin-pw-1", "admin_password2": "neues-admin-pw-1"})
    assert "Admin-Passwort wurde geändert" in r.text
    # Anmelde-Passwort darf nicht gleich dem Admin-Passwort werden
    r = post(client, "/konto/passwort", {"old_password": PASSWORD, "password": "neues-admin-pw-1",
                                         "password2": "neues-admin-pw-1"})
    assert r.status_code == 400 and "vom Admin-Passwort unterscheiden" in r.text

    assert client.get("/einstellungen/firma", follow_redirects=False).status_code == 200
    r = post(client, "/admin-passwort/sperren")
    assert "wieder gesperrt" in r.text
    assert client.get("/einstellungen/firma", follow_redirects=False).status_code == 303


def test_weiterleitung_nur_innerhalb_des_programms():
    for bad in ("https://boese.example", "//boese.example", "/\\boese.example", "", None, "/admin-passwort"):
        assert admin_freigabe.safe_next(bad) == "/"
    assert admin_freigabe.safe_next("/einstellungen/bank?x=1") == "/einstellungen/bank?x=1"


# ------------------------------------------------------------- Zugriffsmatrix
def _membership(db, email) -> Membership:
    db.expire_all()
    user = db.scalar(select(User).where(User.email == email))
    return db.scalar(select(Membership).where(Membership.user_id == user.id))


def test_matrix_zeigt_und_speichert_rechte(client, outbox, db):
    _setup(client)
    _invite(client, outbox, "max@example.org", "mitarbeiter")
    m = _membership(db, "max@example.org")
    owner = _membership(db, "chefin@example.org")

    page = client.get("/benutzer/matrix").text
    assert "Zugriffsmatrix" in page and "max@example.org" in page
    assert f'name="p_{m.id}_angebote"' in page and f'name="p_{owner.id}_angebote"' not in page  # Inhaber fest

    data = {f"zeile_{m.id}": "1", **{f"p_{m.id}_{k}": v for k, v in m.permissions.items()},
            f"p_{m.id}_euer": READ, f"p_{m.id}_angebote": NONE, f"x_{m.id}": "festschreiben"}
    r = post(client, "/benutzer/matrix", data)
    assert "1 Person(en) geändert" in r.text
    m = _membership(db, "max@example.org")
    assert m.permissions["euer"] == READ and m.permissions["angebote"] == NONE
    assert m.extra_rights == ["festschreiben"]
    assert db.scalar(select(AuditLog).where(AuditLog.action == "rechte_geaendert")) is not None
    assert "Keine Änderungen" in post(client, "/benutzer/matrix", data).text


def test_matrix_keine_rechte_ausweitung(client, outbox, db):
    _setup(client)
    bea_client = _invite(client, outbox, "bea@example.org", "buchhaltung")
    _invite(client, outbox, "max@example.org", "mitarbeiter")
    bea = _membership(db, "bea@example.org")
    perms = {**bea.permissions, "benutzer": EDIT}
    post(client, f"/benutzer/{bea.id}", {"role": "buchhaltung", "is_active": "1", "aktion": "speichern",
                                         **{f"perm_{k}": v for k, v in perms.items()}})
    m = _membership(db, "max@example.org")

    # Bea darf Max kein Recht auf Einstellungen geben (hat sie selbst nicht) – nichts wird gespeichert
    data = {f"zeile_{m.id}": "1", **{f"p_{m.id}_{k}": v for k, v in m.permissions.items()},
            f"p_{m.id}_einstellungen": EDIT, f"p_{m.id}_euer": READ}
    r = post(bea_client, "/benutzer/matrix", data)
    assert r.status_code == 400 and "keine Rechte vergeben, die Sie selbst nicht haben" in r.text
    assert _membership(db, "max@example.org").permissions["euer"] == m.permissions["euer"]
    # … und sich nicht selbst aus der Benutzerverwaltung aussperren
    data = {f"zeile_{bea.id}": "1", **{f"p_{bea.id}_{k}": v for k, v in perms.items()}, f"p_{bea.id}_benutzer": READ}
    r = post(bea_client, "/benutzer/matrix", data)
    assert r.status_code == 400 and "eigenes Recht auf die Benutzerverwaltung" in r.text
    # Inhaber-Zeilen lassen sich auch mit gefälschtem Formular nicht ändern
    owner = _membership(db, "chefin@example.org")
    post(bea_client, "/benutzer/matrix", {f"zeile_{owner.id}": "1", f"p_{owner.id}_benutzer": NONE})
    assert _membership(db, "chefin@example.org").role == "inhaber"


def test_matrix_nur_mit_recht_benutzerverwaltung(client, outbox):
    _setup(client)
    c = _invite(client, outbox, "max@example.org", "mitarbeiter")
    assert c.get("/benutzer/matrix").status_code == 403
    assert post(c, "/benutzer/matrix", {}).status_code == 403
