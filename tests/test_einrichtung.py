from sqlalchemy import select

from app.database import tenant_scope
from app.models import AuditLog, Tenant, TenantSetting
from app.services import einstellungen
from tests.conftest import VALID_SETTINGS, complete_wizard, ersteinrichtung, post

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_ohne_einrichtung_nur_assistent(client):
    ersteinrichtung(client)
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/einrichtung"
    r = client.get("/einstellungen/firma", follow_redirects=False)
    assert r.headers["location"] == "/einrichtung"


def test_stylesheet_mit_versionsstand(client):
    """Nach einem Update muss der Browser das neue Stylesheet laden (sonst alte Darstellung aus dem Cache)."""
    from app.templating import static_version

    html = client.get("/ersteinrichtung").text
    assert f"/static/style.css?v={static_version('style.css')}" in html and static_version("style.css") > 0


def test_assistent_komplett(client, db):
    ersteinrichtung(client)
    r = complete_wizard(client)
    assert "Einrichtung ist abgeschlossen" in r.text
    assert "Übersicht – Muster Service" in r.text

    tenant = db.scalar(select(Tenant))
    assert tenant.setup_completed
    assert tenant.name == "Muster Service"  # Name aus den Firmendaten übernommen
    with tenant_scope(db, tenant.id):
        values = einstellungen.load(db)
    assert values["bank.iban"] == "DE89370400440532013000"
    assert values["design.zahlungsziel_tage"] == 14
    assert values["steuer.kleinunternehmer"] is False


def test_jede_seite_mit_angezeigten_werten_speicherbar(client, db):
    """Jede Einstellungs-Seite muss sich mit genau den Werten speichern lassen, die das Formular anzeigt
    (wie beim Klick auf „Weiter“ im Assistenten). Hier fiel die Mahnwesen-Seite mit Fehler 500 aus, weil
    Standardwerte als Decimal ins Änderungsprotokoll (JSON) geschrieben werden sollten."""
    ersteinrichtung(client)
    complete_wizard(client)
    tenant = db.scalar(select(Tenant))
    for group in einstellungen.GROUPS:
        with tenant_scope(db, tenant.id):
            shown = einstellungen.form_values(db, group)
        db.rollback()
        r = post(client, f"/einrichtung/{group.key}", shown)
        assert r.status_code == 200, (group.key, r.status_code, r.text[-500:])
        r = post(client, f"/einstellungen/{group.key}", shown)
        assert r.status_code == 200, (group.key, r.status_code, r.text[-500:])
    with tenant_scope(db, tenant.id):
        assert str(einstellungen.get(db, "mahnung.stufe2.gebuehr")) == "5.00"
        # Unveränderte Standardwerte erzeugen keinen Eintrag im Änderungsprotokoll
        logged = db.scalars(select(AuditLog).where(AuditLog.entity == "einstellungen",
                                                   AuditLog.entity_id == "mahnwesen")).all()
        assert logged == []
    r = post(client, "/einstellungen/mahnwesen", {**shown_mahnwesen(db, tenant.id), "mahnung.stufe2.gebuehr": "7,50"})
    assert r.status_code == 200
    with tenant_scope(db, tenant.id):
        assert einstellungen.get(db, "mahnung.stufe2.gebuehr") == "7.50"
        entry = db.scalars(select(AuditLog).where(AuditLog.entity_id == "mahnwesen")).one()
        assert entry.changes["mahnung.stufe2.gebuehr"] == {"alt": "5.00", "neu": "7.50"}


def test_fehlerseite_bei_unerwartetem_fehler(client, monkeypatch):
    """Nach einem unerwarteten Fehler ist die Datenbank-Sitzung geschlossen – die Fehlerseite darf dann nicht
    selbst abstürzen (früher: DetachedInstanceError beim Anzeigen des Firmennamens)."""
    from fastapi.testclient import TestClient

    from app import main

    ersteinrichtung(client)
    complete_wizard(client)

    def broken(db, *args, **kwargs):
        db.rollback()  # wie bei einem gescheiterten Speichern: alle geladenen Objekte verfallen
        raise RuntimeError("Testfehler")

    monkeypatch.setattr(einstellungen, "save_group", broken)
    with TestClient(main.app, raise_server_exceptions=False) as c:
        c.cookies = client.cookies
        r = post(c, "/einstellungen/firma", VALID_SETTINGS["firma"])
    assert r.status_code == 500 and "unerwarteter Fehler" in r.text


def shown_mahnwesen(db, tenant_id):
    with tenant_scope(db, tenant_id):
        shown = einstellungen.form_values(db, einstellungen.GROUPS_BY_KEY["mahnwesen"])
    db.rollback()
    return shown


def test_pflichtfelder_und_pruefungen(client):
    ersteinrichtung(client)
    r = post(client, "/einrichtung/firma", {"firma.name": ""})
    assert r.status_code == 400
    assert "Bitte ausfüllen" in r.text

    r = post(client, "/einrichtung/bank", {"bank.kontoinhaber": "X", "bank.iban": "DE00 1234"})
    assert r.status_code == 400
    assert "IBAN ist ungültig" in r.text

    r = post(client, "/einrichtung/steuer", {"steuer.versteuerung": "ist", "steuer.voranmeldung": "monatlich"})
    assert r.status_code == 400
    assert "Steuernummer oder USt-IdNr." in r.text

    r = post(client, "/einrichtung/nummern", {**VALID_SETTINGS["nummern"], "nummern.rechnung.format": "{JJJJ}"})
    assert r.status_code == 400
    assert "laufende Nummer" in r.text


def test_abschluss_nur_mit_allen_pflichtangaben(client):
    ersteinrichtung(client)
    post(client, "/einrichtung/firma", VALID_SETTINGS["firma"])
    r = post(client, "/einrichtung/abschluss")
    assert r.status_code == 400
    assert "Bankverbindung" in r.text


def test_passwoerter_verschluesselt(client, db):
    ersteinrichtung(client)
    complete_wizard(client)
    r = post(client, "/einstellungen/email", {"imap.aktiv": "1", "imap.host": "imap.example.org", "imap.port": "993",
                                               "imap.ssl": "1", "imap.benutzer": "postfach",
                                               "imap.passwort": "streng-geheim", "imap.ordner_eingang": "Rechnungen",
                                               "imap.intervall_minuten": "10", "smtp.port": "465"})
    assert r.status_code == 200, r.text
    row = db.scalar(select(TenantSetting).where(TenantSetting.key == "imap.passwort")
                    .execution_options(all_tenants=True))
    assert row.value and "streng-geheim" not in row.value
    with tenant_scope(db, row.tenant_id):
        assert einstellungen.get_secret(db, "imap.passwort") == "streng-geheim"

    # Leeres Passwortfeld lässt das gespeicherte unverändert
    post(client, "/einstellungen/email", {"imap.aktiv": "1", "imap.host": "imap2.example.org", "imap.port": "993",
                                          "imap.benutzer": "postfach", "imap.ordner_eingang": "Rechnungen",
                                          "imap.intervall_minuten": "10", "smtp.port": "465"})
    db.expire_all()
    with tenant_scope(db, row.tenant_id):
        assert einstellungen.get_secret(db, "imap.passwort") == "streng-geheim"

    # Im Protokoll steht das Passwort nicht im Klartext
    for entry in db.scalars(select(AuditLog)):
        assert "streng-geheim" not in str(entry.changes)


def test_aenderungsprotokoll(client, db):
    ersteinrichtung(client)
    complete_wizard(client)
    post(client, "/einstellungen/design", {**VALID_SETTINGS["design"], "design.zahlungsziel_tage": "30"})
    entry = db.scalars(select(AuditLog).where(AuditLog.entity_id == "design")
                       .order_by(AuditLog.id.desc())).first()
    assert entry.changes["design.zahlungsziel_tage"] == {"alt": 14, "neu": 30}
    r = client.get("/einstellungen/protokoll")
    assert "design.zahlungsziel_tage" in r.text


def test_logo_upload(client):
    ersteinrichtung(client)
    files = {"datei": ("logo.png", PNG, "image/png")}
    r = post(client, "/einstellungen/logo", {"zurueck": "/einrichtung/design"}, files=files)
    assert "Logo wurde gespeichert" in r.text
    r = client.get("/dateien/logo")
    assert r.status_code == 200
    assert r.content == PNG

    # Getarnte Datei (Endung .png, Inhalt kein Bild) wird abgelehnt
    files = {"datei": ("boese.png", b"MZ\x90\x00 kein bild", "image/png")}
    r = post(client, "/einstellungen/logo", {"zurueck": "/einrichtung/design"}, files=files)
    assert "Dateityp nicht erlaubt" in r.text
