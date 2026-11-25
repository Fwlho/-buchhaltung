"""PDF-Vorlagen: Auswahl je Belegart, Rückfall auf „Klassisch“, Muster-PDFs ohne Datenbank-Einträge."""
import io

import pdfplumber
import pytest
from sqlalchemy import func, select

from app.database import tenant_scope
from app.models import Document, Quote
from app.services import einstellungen, pdf_designs, pdf_rechnung, pdf_vorlagen
from tests.conftest import post
from tests.test_rechnungen import _draft, _ready


def _text(data: bytes) -> str:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "".join(page.extract_text() or "" for page in pdf.pages)


class _TestPage(pdf_rechnung._Page):
    def first(self, canv, pdf):
        super().first(canv, pdf)
        canv.setFont(pdf_rechnung.FONT, 8)
        canv.drawString(30, 30, "TESTVORLAGE")


class _TestDesign(pdf_designs.Klassisch):
    page_class = _TestPage


@pytest.fixture
def testvorlage(monkeypatch):
    monkeypatch.setattr(pdf_vorlagen, "VORLAGEN", pdf_vorlagen.VORLAGEN + (
        pdf_vorlagen.Vorlage("test", "Test", "nur für Tests", ("rechnung", "mahnung")),))
    monkeypatch.setitem(pdf_designs.DESIGNS, "test", _TestDesign)


def test_verzeichnis_vollstaendig():
    for v in pdf_vorlagen.VORLAGEN:
        assert v.key in pdf_designs.DESIGNS, v.key
    for art in pdf_vorlagen.ARTEN:
        assert pdf_vorlagen.STANDARD in dict(pdf_vorlagen.choices(art))
        assert pdf_vorlagen.setting_key(art) in einstellungen.FIELDS_BY_KEY


def test_unbekannte_vorlage_faellt_auf_klassisch_zurueck(testvorlage):
    assert pdf_vorlagen.key_for({}, "rechnung") == "klassisch"
    assert pdf_vorlagen.key_for({"design.vorlage_rechnung": "gibtsnicht"}, "rechnung") == "klassisch"
    assert pdf_vorlagen.key_for({"design.vorlage_rechnung": "test"}, "rechnung") == "test"
    # Vorlage, die es für Angebote nicht gibt
    assert pdf_vorlagen.key_for({"design.vorlage_angebot": "test"}, "angebot") == "klassisch"
    assert pdf_vorlagen.key_for({}, "rechnung", wanted="test") == "test"


def test_eingestellte_vorlage_wirkt_nur_fuer_ihre_belegart(client, db, testvorlage):
    _ready(client)
    doc = _draft(client, db)
    assert "TESTVORLAGE" not in _text(client.get(f"/rechnungen/{doc.id}/vorschau").content)

    with tenant_scope(db, doc.tenant_id):
        einstellungen.set_values(db, {"design.vorlage_rechnung": "test"})
        db.commit()
    assert "TESTVORLAGE" in _text(client.get(f"/rechnungen/{doc.id}/vorschau").content)
    assert "TESTVORLAGE" not in _text(client.get("/einstellungen/design/muster?art=angebot").content)
    assert "TESTVORLAGE" not in _text(client.get("/einstellungen/design/muster?art=mahnung").content)
    # Muster mit ausdrücklich gewählter Vorlage (zum Vergleichen vor dem Speichern)
    assert "TESTVORLAGE" in _text(client.get("/einstellungen/design/muster?art=mahnung&vorlage=test").content)


def test_muster_pdfs_ohne_datenbank_eintraege(client, db):
    _ready(client)
    counts = lambda: (db.scalar(select(func.count()).select_from(Document).execution_options(all_tenants=True)),  # noqa: E731
                      db.scalar(select(func.count()).select_from(Quote).execution_options(all_tenants=True)))
    before = counts()
    for art, title in (("rechnung", "Rechnung Nr."), ("angebot", "Angebot Nr."), ("mahnung", "Zahlungserinnerung")):
        r = client.get(f"/einstellungen/design/muster?art={art}")
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
        text = _text(r.content)
        assert "Muster GmbH" in text and "Muster Service" in text, art
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:   # Wasserzeichen: große, schräge Schrift
            assert "".join(c["text"] for c in pdf.pages[0].chars if c["size"] > 60) == "MUSTER", art
        assert title in text, (art, text[:300])
    assert counts() == before
    assert client.get("/einstellungen/design/muster?art=gibtsnicht").status_code == 404


def test_alle_vorlagen_fuer_alle_belegarten(client, db):
    _ready(client)
    for v in pdf_vorlagen.VORLAGEN:
        for art in v.arten:
            r = client.get(f"/einstellungen/design/muster?art={art}&vorlage={v.key}")
            assert r.status_code == 200, (v.key, art)
            text = _text(r.content)
            assert "Muster GmbH" in text and "IBAN" in text, (v.key, art)
            # Angebote der Pakete enthalten das Feld zum Unterschreiben
            if art == "angebot" and v.key != "klassisch":
                assert "auftrag erteilt" in text.lower() and "Unterschrift / Stempel" in text, v.key


def test_vorlagenfarbe_oder_eigene_akzentfarbe():
    from reportlab.lib import colors

    marine = pdf_designs.DESIGNS["marine"]()
    own = colors.HexColor("#123456")
    assert marine.accent({}, own).hexval() == colors.HexColor("#1F3A5F").hexval()
    assert marine.accent({"design.vorlage_farbe": "akzent"}, own) is own
    assert pdf_designs.DESIGNS["klassisch"]().accent({}, own) is own


def test_vorlagen_auswahl_in_den_einstellungen(client):
    _ready(client)
    page = client.get("/einstellungen/design").text
    assert "PDF-Vorlage Rechnung" in page and "PDF-Vorlage Mahnung" in page
    assert "Muster ansehen" in page
