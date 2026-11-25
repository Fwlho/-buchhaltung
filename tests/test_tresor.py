"""Verschlüsselte Datei-Ablage: je Firma eigener Schlüssel, Dateien auf der Platte nicht lesbar."""
import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.database import tenant_scope
from app.models import StoredFile, Tenant
from app.services import ablage, tresor
from tests.test_eingangsrechnungen import PDF_EINFACH, _all_docs, _ready, upload
from tests.test_mandantentrennung import _two_tenants
from tests.test_rechnungen import _final


def _disk(stored) -> bytes:
    return (get_settings().files_dir / stored.path).read_bytes()


def test_beleg_liegt_verschluesselt_und_wird_entschluesselt_angezeigt(client, db):
    _ready(client)
    upload(client, ("rechnung.pdf", PDF_EINFACH))
    doc = _all_docs(db)[0]
    raw = _disk(doc.file)
    assert raw.startswith(tresor.MAGIC) and b"%PDF" not in raw and b"Schulz" not in raw
    assert doc.file.sha256 == hashlib.sha256(PDF_EINFACH).hexdigest()     # Prüfwert des Originals bleibt
    r = client.get(f"/pruefbereich/{doc.id}/datei")
    assert r.status_code == 200 and r.content == PDF_EINFACH
    assert r.headers["cache-control"] == "private, no-store"


def test_rechnungs_pdf_verschluesselt(client, db):
    _ready(client)
    doc = _final(client, db)
    assert _disk(doc.file).startswith(tresor.MAGIC)
    pdf = client.get(f"/rechnungen/{doc.id}/pdf")
    assert pdf.content.startswith(b"%PDF") and "inline" in pdf.headers["content-disposition"]


def test_jede_firma_eigener_schluessel_und_keine_vertauschung(client, outbox, db):
    client_b, tenant_a, tenant_b = _two_tenants(client, outbox, db)   # Firma A hat ein Logo
    upload(client_b, ("b.pdf", PDF_EINFACH))
    db.expire_all()
    keys = dict(db.execute(select(Tenant.id, Tenant.file_key)).all())
    assert keys[tenant_a.id] and keys[tenant_b.id] and keys[tenant_a.id] != keys[tenant_b.id]
    file_a = db.scalars(select(StoredFile).where(StoredFile.tenant_id == tenant_a.id)
                        .execution_options(all_tenants=True)).first()
    blob = _disk(file_a)
    # Datei von Firma A einer Datei von Firma B „unterschieben“ → lässt sich nicht entschlüsseln
    with pytest.raises(tresor.TresorError):
        tresor.decrypt(db, tenant_b.id, file_a.path, blob)
    # an einen anderen Ablageort kopiert (gleiche Firma) → ebenfalls abgelehnt
    with pytest.raises(tresor.TresorError):
        tresor.decrypt(db, tenant_a.id, file_a.path + "x", blob)
    # verändert → abgelehnt
    broken = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(tresor.TresorError):
        tresor.decrypt(db, tenant_a.id, file_a.path, broken)
    assert tresor.decrypt(db, tenant_a.id, file_a.path, blob).startswith(b"\x89PNG")


def test_ohne_richtigen_hauptschluessel_nicht_lesbar(client, db, monkeypatch):
    _ready(client)
    upload(client, ("rechnung.pdf", PDF_EINFACH))
    doc = _all_docs(db)[0]
    blob = _disk(doc.file)
    monkeypatch.setattr(tresor, "get_settings", lambda: SimpleNamespace(encryption_key="anderer-schluessel"))
    with pytest.raises(tresor.TresorError):
        tresor.decrypt(db, doc.tenant_id, doc.file.path, blob)


def test_alte_unverschluesselte_dateien_werden_gelesen_und_nachverschluesselt(client, db):
    _ready(client)
    upload(client, ("rechnung.pdf", PDF_EINFACH))
    doc = _all_docs(db)[0]
    path = get_settings().files_dir / doc.file.path
    path.write_bytes(PDF_EINFACH)                         # Stand vor der Verschlüsselung
    assert client.get(f"/pruefbereich/{doc.id}/datei").content == PDF_EINFACH
    assert tresor.encrypt_existing(db) == 1
    assert path.read_bytes().startswith(tresor.MAGIC) and not path.with_name(path.name + ".teil").exists()
    assert tresor.encrypt_existing(db) == 0               # nichts doppelt verschlüsseln
    with tenant_scope(db, doc.tenant_id):
        stored = db.get(StoredFile, doc.file_id)
        assert ablage.read_bytes(stored, db) == PDF_EINFACH


def test_firmen_export_enthaelt_lesbare_dateien(client, db):
    import io
    import zipfile

    _ready(client)
    upload(client, ("rechnung.pdf", PDF_EINFACH))
    from tests.conftest import post

    data = post(client, "/datensicherung/firmen-export").content
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        pdfs = [zf.read(n) for n in zf.namelist() if n.startswith("dateien/")]
    assert PDF_EINFACH in pdfs
