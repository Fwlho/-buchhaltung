"""Eingangsordner: Dateien hineinlegen → passender Prüfbereich; danach „verarbeitet“ bzw. „fehler“."""
import os
import time

import pytest
from sqlalchemy import select

from app import worker
from app.database import tenant_scope
from app.models import Tenant
from app.services import eingangsordner, einstellungen
from tests.conftest import post
from tests.test_eingangsrechnungen import PDF_EINFACH, PNG, _all_docs, _ready, make_pdf
from tests.test_rechnungs_import import ALTE_RECHNUNG
from tests.test_rechte import _invite


@pytest.fixture(autouse=True)
def _sofort(monkeypatch):
    monkeypatch.setattr(eingangsordner, "MIN_AGE_SECONDS", 0)


def _setup(client, tmp_path, auto=False):
    _ready(client)
    ein, aus = tmp_path / "Rechnungen" / "Eingang", tmp_path / "Rechnungen" / "Ausgang"
    data = {"ordner.eingang": str(ein), "ordner.ausgang": str(aus), **({"ordner.aktiv": "1"} if auto else {})}
    r = post(client, "/einstellungen/ordner", data)
    assert r.status_code == 200 and "gespeichert" in r.text, r.text
    assert ein.is_dir() and aus.is_dir()       # werden beim Speichern angelegt
    return ein, aus


def test_einstellungen_pruefen(client, tmp_path):
    _ready(client)
    r = post(client, "/einstellungen/ordner", {"ordner.eingang": "relativ\\ordner"})
    assert r.status_code == 400 and "vollständigen Pfad" in r.text
    same = str(tmp_path / "x")
    r = post(client, "/einstellungen/ordner", {"ordner.eingang": same, "ordner.ausgang": same})
    assert r.status_code == 400 and "zwei getrennte Ordner" in r.text
    r = post(client, "/einstellungen/ordner", {"ordner.eingang": same, "ordner.ausgang": str(tmp_path / "x" / "y")})
    assert r.status_code == 400 and "zwei getrennte Ordner" in r.text
    r = post(client, "/einstellungen/ordner", {"ordner.aktiv": "1"})
    assert r.status_code == 400 and "mindestens einen Ordner" in r.text
    assert "Noch kein Ordner eingerichtet" in client.get("/pruefbereich").text


def test_eingangsrechnungen_per_knopf(client, db, tmp_path):
    ein, _ = _setup(client, tmp_path)
    (ein / "rechnung.pdf").write_bytes(PDF_EINFACH)
    (ein / "foto.png").write_bytes(PNG)
    (ein / "notiz.txt").write_text("keine Rechnung", encoding="utf-8")
    (ein / "Thumbs.db").write_bytes(b"x")          # Windows-Datei wird übersprungen
    page = client.get("/pruefbereich").text
    assert str(ein) in page and "Jetzt einlesen" in page

    r = post(client, "/pruefbereich/ordner-einlesen")
    assert "2 Datei(en) eingelesen" in r.text and "notiz.txt" in r.text
    docs = _all_docs(db)
    assert len(docs) == 2 and all(d.pending and d.source == "ordner" for d in docs)
    assert "Eingangsordner" in client.get("/pruefbereich").text           # Herkunft in der Liste
    assert sorted(p.name for p in (ein / "verarbeitet").iterdir()) == ["foto.png", "rechnung.pdf"]
    assert (ein / "fehler" / "notiz.txt").exists()
    assert "Dateityp nicht erlaubt" in (ein / "fehler" / "notiz.txt.grund.txt").read_text(encoding="utf-8")
    assert (ein / "Thumbs.db").exists()

    # Dieselbe Datei noch einmal -> „fehler“, gleichnamige Datei wird nicht überschrieben
    (ein / "rechnung.pdf").write_bytes(PDF_EINFACH)
    r = post(client, "/pruefbereich/ordner-einlesen")
    assert "bereits eingelesen" in r.text
    assert len(_all_docs(db)) == 2
    (ein / "notiz.txt").write_text("nochmal", encoding="utf-8")
    post(client, "/pruefbereich/ordner-einlesen")
    assert len([p for p in (ein / "fehler").iterdir() if p.suffix == ".txt" and ".grund" not in p.name]) == 2
    assert "Keine neuen Dateien" in post(client, "/pruefbereich/ordner-einlesen").text


def test_ausgangsrechnungen_landen_im_import(client, db, tmp_path):
    _, aus = _setup(client, tmp_path)
    (aus / "alt.pdf").write_bytes(make_pdf(ALTE_RECHNUNG))
    (aus / "foto.png").write_bytes(PNG)                      # Ausgang: nur PDF
    r = post(client, "/rechnungen/import/ordner-einlesen")
    assert "1 Datei(en) eingelesen" in r.text and "foto.png" in r.text
    out = [d for d in _all_docs(db) if d.direction == "ausgang"]
    assert len(out) == 1 and out[0].pending and out[0].number == "2026-016"
    assert not [d for d in _all_docs(db) if d.direction == "eingang"]
    assert (aus / "verarbeitet" / "alt.pdf").exists() and (aus / "fehler" / "foto.png").exists()


def test_frisch_kopierte_dateien_warten(client, db, tmp_path, monkeypatch):
    monkeypatch.setattr(eingangsordner, "MIN_AGE_SECONDS", 3600)
    ein, _ = _setup(client, tmp_path)
    (ein / "rechnung.pdf").write_bytes(PDF_EINFACH)
    assert "Keine neuen Dateien" in post(client, "/pruefbereich/ordner-einlesen").text
    old = time.time() - 7200
    os.utime(ein / "rechnung.pdf", (old, old))
    assert "1 Datei(en) eingelesen" in post(client, "/pruefbereich/ordner-einlesen").text


def test_hintergrund_nur_wenn_eingeschaltet(client, db, tmp_path):
    ein, _ = _setup(client, tmp_path)
    (ein / "rechnung.pdf").write_bytes(PDF_EINFACH)
    worker.run_due_jobs()
    assert not _all_docs(db)                       # automatisch aus
    post(client, "/einstellungen/ordner", {"ordner.eingang": str(ein), "ordner.aktiv": "1"})
    worker.run_due_jobs()
    assert len(_all_docs(db)) == 1
    tenant_id = db.scalar(select(Tenant.id))
    with tenant_scope(db, tenant_id):
        assert eingangsordner.last_status(db)["eingelesen"] == 1


def test_gleichzeitiges_einlesen_wird_verhindert(client, db, tmp_path):
    _setup(client, tmp_path)
    eingangsordner._lock.acquire()
    try:
        assert "gerade schon eingelesen" in post(client, "/pruefbereich/ordner-einlesen").text
    finally:
        eingangsordner._lock.release()


def test_rechte(client, outbox, tmp_path):
    _setup(client, tmp_path)
    stb = _invite(client, outbox, "stb@example.org", "steuerberater")   # nur lesen
    assert post(stb, "/pruefbereich/ordner-einlesen").status_code == 403
    assert post(stb, "/rechnungen/import/ordner-einlesen").status_code == 403


def test_nur_lokal(client, db, tmp_path, monkeypatch):
    ein, _ = _setup(client, tmp_path)
    from app.config import get_settings

    monkeypatch.setattr(type(get_settings()), "is_local", property(lambda self: False))
    tenant_id = db.scalar(select(Tenant.id))
    with tenant_scope(db, tenant_id):
        assert eingangsordner.folders(db) == {}
        assert "Nur in der lokalen Version" in eingangsordner.setting_problems(str(ein), None, False)["ordner.eingang"]
        assert einstellungen.get(db, "ordner.eingang") == str(ein)
