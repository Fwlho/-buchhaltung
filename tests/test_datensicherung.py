"""Lokale Datensicherung (nur bei Änderungen, letzte N behalten) und Firmen-Export."""
import io
import json
import zipfile

import pytest

from app.services import sicherung
from tests.conftest import IS_SQLITE, post
from tests.test_mandantentrennung import _two_tenants
from tests.test_rechnungen import _final
from tests.test_rechte import _invite


def _clear_backups():
    for b in sicherung.backups():
        b.path.unlink()
    (sicherung.backup_dir() / "letzte_sicherung.txt").unlink(missing_ok=True)


local_only = pytest.mark.skipif(not IS_SQLITE, reason="Lokale Sicherung gibt es nur mit SQLite")


@local_only
def test_sicherung_nur_bei_aenderungen(client, outbox, db):
    _two_tenants(client, outbox, db)  # Firma A mit Logo-Datei
    _clear_backups()
    first = sicherung.create_local_backup()
    assert first is not None and first.exists()
    with zipfile.ZipFile(first) as zf:
        names = zf.namelist()
        assert "app.db" in names and any(n.startswith("files/") and n.endswith(".png") for n in names)
        assert zf.read("app.db").startswith(b"SQLite format 3")
    assert sicherung.create_local_backup() is None          # nichts geändert -> keine neue Sicherung

    post(client, "/kunden/neu", {"kind": "firma", "name": "Neukunde", "country": "Deutschland"})
    second = sicherung.create_local_backup()
    assert second is not None and second != first
    assert sicherung.create_local_backup(force=True) is not None
    assert len(sicherung.backups()) >= 2
    assert sicherung.prune(keep=1) >= 1 and len(sicherung.backups()) == 1
    assert not list(sicherung.backup_dir().glob("*.teil"))


@local_only
def test_sicherung_ueber_einstellungen(client, outbox, db):
    _two_tenants(client, outbox, db)
    _clear_backups()
    page = client.get("/datensicherung").text
    assert "Jetzt sichern" in page and "Firmen-Export herunterladen" in page   # Chefin = Plattform-Admin + Inhaberin
    r = post(client, "/datensicherung/jetzt")
    assert "angelegt" in r.text and len(sicherung.backups()) == 1
    bh = _invite(client, outbox, "bh@example.org", "buchhaltung")
    assert bh.get("/datensicherung").status_code == 403  # kein Recht „Einstellungen“


def test_firmen_export(client, outbox, db):
    client_b, tenant_a, tenant_b = _two_tenants(client, outbox, db)
    post(client, "/einstellungen/email", {"imap.host": "imap.example.de", "imap.benutzer": "a@example.de",
                                          "imap.passwort": "SuperGeheim123"})
    doc = _final(client, db)
    r = post(client, "/datensicherung/firmen-export")
    assert r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    customers = json.loads(zf.read("daten/customers.json"))
    assert [c["name"] for c in customers] == ["Bäckerei Krume GmbH"]
    documents = json.loads(zf.read("daten/documents.json"))
    assert [d["number"] for d in documents] == [doc.number] and documents[0]["gross_total"] == "182.29"
    settings = {s["key"]: s["value"] for s in json.loads(zf.read("daten/tenant_settings.json"))}
    assert settings["imap.passwort"] == "(Passwort – nicht exportiert)" and settings["firma.name"] == "Muster Service"
    assert any(n.startswith("dateien/") and n.endswith(".pdf") for n in names)
    users = json.loads(zf.read("daten/benutzer.json"))
    assert [u["email"] for u in users] == ["chefin@example.org"]
    everything = b"".join(zf.read(n) for n in names if n.startswith("daten/"))
    assert b"SuperGeheim123" not in everything and b"password_hash" not in everything and b"Bodo" not in everything
    assert all(row["tenant_id"] == tenant_a.id for row in documents)
    assert "firmen_export" in client.get("/einstellungen/protokoll").text

    zf_b = zipfile.ZipFile(io.BytesIO(post(client_b, "/datensicherung/firmen-export").content))
    assert json.loads(zf_b.read("daten/customers.json")) == []
    assert json.loads(zf_b.read("daten/documents.json")) == []
    assert not [n for n in zf_b.namelist() if n.endswith(".pdf")]
    assert tenant_b.id != tenant_a.id
