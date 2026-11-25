from sqlalchemy import select

from app.models import Membership, Tenant, User
from app.permissions import EDIT, MODULE_KEYS, NONE, READ, has_module, role_template
from tests.conftest import PASSWORD, admin_freigabe, complete_wizard, ersteinrichtung, link_from, new_client, post


def _invite(client, outbox, email: str, role: str):
    r = post(client, "/benutzer/einladen", {"name": email.split("@")[0], "email": email, "role": role})
    assert "Einladungs-Link" in r.text, r.text
    c = new_client()
    c.get(link_from(outbox, email))
    r = post(c, link_from(outbox, email), {"name": "Neu", "password": PASSWORD, "password2": PASSWORD})
    assert r.status_code == 200, r.text
    admin_freigabe(c)
    return c


def _setup(client):
    ersteinrichtung(client)
    complete_wizard(client)


def test_rollen_vorlagen():
    perms, extras = role_template("steuerberater")
    assert all(perms[k] == READ for k in MODULE_KEYS if k not in ("einstellungen", "benutzer"))
    assert perms["einstellungen"] == NONE and extras == []
    perms, extras = role_template("mitarbeiter")
    assert perms["angebote"] == EDIT and "festschreiben" not in extras
    assert has_module("inhaber", {}, "benutzer", edit=True)  # Inhaber darf immer alles


def test_mitarbeiter_kein_zugriff_auf_einstellungen(client, outbox):
    _setup(client)
    c = _invite(client, outbox, "max@example.org", "mitarbeiter")
    assert c.get("/").status_code == 200
    assert c.get("/einstellungen/firma").status_code == 403
    assert c.get("/benutzer").status_code == 403
    assert post(c, "/einstellungen/firma", {"firma.name": "Gehackt"}).status_code == 403
    assert c.get("/admin").status_code == 403
    assert "Muster Service" in client.get("/einstellungen/firma").text


def test_steuerberater_nur_lesen(client, outbox):
    _setup(client)
    c = _invite(client, outbox, "stb@example.org", "steuerberater")
    assert c.get("/einstellungen/firma").status_code == 403
    assert c.get("/").status_code == 200


def test_rechte_feinjustieren(client, outbox, db):
    _setup(client)
    c = _invite(client, outbox, "max@example.org", "mitarbeiter")
    user = db.scalar(select(User).where(User.email == "max@example.org"))
    m = db.scalar(select(Membership).where(Membership.user_id == user.id))
    data = {"role": "mitarbeiter", "is_active": "1", "aktion": "speichern"}
    data.update({f"perm_{k}": v for k, v in m.permissions.items()})
    data["perm_einstellungen"] = READ
    r = post(client, f"/benutzer/{m.id}", data)
    assert "Änderungen für" in r.text
    assert c.get("/einstellungen/firma").status_code == 200
    assert post(c, "/einstellungen/firma", {"firma.name": "X"}).status_code == 403

    # Zugang sperren
    data["is_active"] = ""
    post(client, f"/benutzer/{m.id}", data)
    assert c.get("/", follow_redirects=False).status_code == 403


def test_keine_rechte_ausweitung(client, outbox, db):
    _setup(client)
    # Buchhalterin bekommt Benutzerverwaltung, ist aber keine Inhaberin
    c = _invite(client, outbox, "bea@example.org", "buchhaltung")
    bea = db.scalar(select(User).where(User.email == "bea@example.org"))
    m = db.scalar(select(Membership).where(Membership.user_id == bea.id))
    data = {"role": "buchhaltung", "is_active": "1", "aktion": "speichern",
            **{f"perm_{k}": v for k, v in m.permissions.items()}, "perm_benutzer": EDIT}
    post(client, f"/benutzer/{m.id}", data)

    # Sie darf niemanden zum Inhaber machen …
    r = post(c, "/benutzer/einladen", {"email": "x@example.org", "role": "inhaber"})
    assert "Nur Inhaber" in r.text
    # … und sich selbst nicht hochstufen
    r = post(c, f"/benutzer/{m.id}", {**data, "role": "inhaber"})
    assert r.status_code == 400
    # … und die Inhaberin nicht bearbeiten
    owner = db.scalar(select(Membership).where(Membership.role == "inhaber"))
    r = post(c, f"/benutzer/{owner.id}", {"role": "mitarbeiter", "is_active": ""})
    assert "Nur Inhaber" in r.text


def test_letzter_inhaber_bleibt(client, db):
    _setup(client)
    owner = db.scalar(select(Membership).where(Membership.role == "inhaber"))
    r = post(client, f"/benutzer/{owner.id}", {"role": "mitarbeiter", "is_active": "1", "aktion": "speichern"})
    assert r.status_code == 400


def test_gesperrte_firma(client, outbox, db):
    _setup(client)
    c = _invite(client, outbox, "max@example.org", "mitarbeiter")
    tenant = db.scalar(select(Tenant))
    post(client, f"/admin/firmen/{tenant.id}/status", {"status": "gesperrt"})
    r = c.get("/")
    assert r.status_code == 403
    assert "gesperrt" in r.text
    post(client, f"/admin/firmen/{tenant.id}/status", {"status": "aktiv"})
    assert c.get("/").status_code == 200


def test_einladung_nur_einmal_nutzbar(client, outbox):
    _setup(client)
    _invite(client, outbox, "max@example.org", "mitarbeiter")
    c = new_client()
    r = c.get(link_from(outbox, "max@example.org"))
    assert r.status_code == 404
    assert "Einladung ungültig" in r.text
