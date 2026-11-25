"""PDF-Layout mit sehr langen Namen und Texten: nichts darf über den Rand oder übereinander laufen."""
import io
from collections import defaultdict

import pdfplumber
import pytest
from reportlab.lib.units import mm

from app.database import tenant_scope
from app.services import einstellungen, pdf_vorlagen
from app.services.pdf_rechnung import FONT, LEFT, RIGHT, H, W, fit_text, wrap_text
from tests.conftest import VALID_SETTINGS, post
from tests.test_rechnungen import _customer, _draft, _final, _ready

LONG_FIRM = "Glas- und Gebäudereinigung Sonnenschein & Partner Dienstleistungsgesellschaft mbH & Co. KG"
LONG_CUSTOMER = ("Gemeinnützige Wohnungsbaugesellschaft Musterstadt-Nord mit beschränkter Haftung "
                 "& Co. Kommanditgesellschaft")


def _chars(data: bytes) -> list[dict]:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return [c for page in pdf.pages for c in page.chars if c.get("upright", True) and c["text"].strip()]


def _overlaps(chars: list[dict]) -> list[str]:
    """Zeichen derselben Zeile, die sich überdecken (= Texte laufen übereinander)."""
    lines = defaultdict(list)
    for c in chars:
        lines[(c.get("page_number"), round(c["top"]))].append(c)
    found = []
    for row in lines.values():
        row.sort(key=lambda c: c["x0"])
        for a, b in zip(row, row[1:], strict=False):
            if b["x0"] < a["x1"] - 1.0:
                found.append(f"{a['text']}|{b['text']} bei x={a['x0']:.0f}")
    return found


def test_hilfsfunktionen():
    assert fit_text("kurz", FONT, 10, 100) == ("kurz", 10)
    text, size = fit_text("x" * 200, FONT, 10, 100, min_size=6)
    assert size == 6 and text.endswith("…")
    lines = wrap_text("Ein sehr langer Name " * 5 + "ohneleerzeichen" * 6, FONT, 10, 85 * mm)
    from reportlab.pdfbase.pdfmetrics import stringWidth
    assert len(lines) > 1 and all(stringWidth(line, FONT, 10) <= 85 * mm for line in lines)


@pytest.mark.parametrize("vorlage", [v.key for v in pdf_vorlagen.VORLAGEN])
def test_lange_texte_bleiben_im_rahmen(client, db, vorlage):
    _ready(client)
    with tenant_scope(db, _customer(client, db).tenant_id):
        einstellungen.set_values(db, {"design.vorlage_rechnung": vorlage})
        db.commit()
    post(client, "/einstellungen/firma", {**VALID_SETTINGS["firma"], "firma.name": LONG_FIRM,
                                          "firma.strasse": "Am Langen Industriegebiet Nordwest Gewerbehof 117-119a",
                                          "firma.email": "buchhaltung.und.rechnungswesen@sonnenschein-partner-reinigung.example",
                                          "firma.website": "www.sonnenschein-partner-gebaeudereinigung-musterstadt.example",
                                          "firma.handelsregister": "Amtsgericht Musterstadt-Nord, HRA 123456, "
                                                                   "Komplementärin: Sonnenschein Verwaltungs GmbH"})
    post(client, "/einstellungen/bank", {**VALID_SETTINGS["bank"], "bank.name": "Volks- und Raiffeisenbank "
                                         "Musterstadt-Nord-Westliches Umland eG"})
    customer = _customer(client, db, name=LONG_CUSTOMER, street="Hinter den langen Gärten am alten "
                         "Bahnhofsgelände 1234 Hinterhaus Aufgang C")
    doc = _final(client, db, _draft(client, db, customer), **{
        "pos-0-titel": "Unterhaltsreinigung sämtlicher Büro-, Verwaltungs- und Sozialräume " * 3,
        "pos-0-beschreibung": "Grundreinigungsleistungsverzeichnisposition" * 3,
        "object_address": "Verwaltungsgebäude Nord, " * 5})
    data = client.get(f"/rechnungen/{doc.id}/pdf").content
    chars = _chars(data)
    text = "".join(c["text"] for c in chars)
    assert "Gemeinnützige" in text and "Kommanditgesellschaft" in text   # umbrochen, nicht abgeschnitten

    right_edge = W - RIGHT + 1
    outside = [c["text"] for c in chars if c["x1"] > right_edge or c["x0"] < 10 * mm]
    assert not outside, f"Text ragt über den Rand: {''.join(outside)[:80]}"
    assert not _overlaps(chars), _overlaps(chars)[:10]

    # Anschriftfeld (Fensterumschlag): Text bleibt innerhalb von 20–105 mm
    window = [c for c in chars if c["page_number"] == 1 and 45 * mm < c["top"] < 95 * mm and c["x0"] < 110 * mm]
    assert window and max(c["x1"] for c in window) <= 105 * mm + 1

    # Fußzeile vollständig (umbrochen statt mit „…“ gekürzt)
    footer = "".join(c["text"] for c in chars if c["top"] > H - 40 * mm)
    assert "…" not in footer
    for word in ("Raiffeisenbank", "Musterstadt-Nord-Westliches", "Komplementärin", "Sonnenschein", "Dienstleistungsgesellschaft"):
        assert word in footer.replace(" ", "") or word in text, word


def _pdf(client, doc_id: int) -> bytes:
    return client.get(f"/rechnungen/{doc_id}/pdf").content


def _png(width: int = 300, height: int = 100) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_briefkopf_zahlung_unten_trennlinie_falzmarken(client, db):
    _ready(client)
    doc = _final(client, db)
    with pdfplumber.open(io.BytesIO(_pdf(client, doc.id))) as pdf:
        page = pdf.pages[0]
        chars = [c for c in page.chars if c.get("upright", True)]
        lines = page.lines

        # Firmenname zentriert im Briefkopf
        name = [c for c in chars if c["top"] < 40 * mm and c["size"] > 12]
        assert "".join(c["text"] for c in name).replace(" ", "") == "MusterService"
        middle = (min(c["x0"] for c in name) + max(c["x1"] for c in name)) / 2
        assert abs(middle - W / 2) < 2

        # Zahlungshinweis + GiroCode unten direkt über der Fußzeile (untere Seitenhälfte)
        pay = next(w for w in page.extract_words() if w["text"] == "Bitte")
        rule_top = H - 40 * mm   # Fußzeilen-Linie liegt tiefer als 40 mm vom unteren Rand
        assert pay["top"] > H / 2 and pay["top"] < rule_top
        total = next(w for w in page.extract_words() if w["text"] == "Gesamtbetrag")
        assert pay["top"] > H - 85 * mm  # unten auf der Seite, nicht direkt unter den Summen
        assert pay["top"] - total["top"] > 40 * mm

        # Durchgehende Linie unter den Summen über die ganze Breite
        full = [ln for ln in lines if ln["x0"] <= LEFT + 1 and ln["x1"] >= W - RIGHT - 1
                and total["bottom"] < ln["top"] < total["bottom"] + 8 * mm]
        assert full, "Trennlinie unter den Summen fehlt"

        # Falzmarken standardmäßig aus
        assert not [ln for ln in lines if ln["x0"] < 6 * mm]

    post(client, "/einstellungen/design", {**VALID_SETTINGS["design"], "design.falzmarken": "1"})
    doc2 = _final(client, db)
    with pdfplumber.open(io.BytesIO(_pdf(client, doc2.id))) as pdf:
        assert len([ln for ln in pdf.pages[0].lines if ln["x0"] < 6 * mm]) == 3


def test_logo_links_oder_rechts(client, db):
    _ready(client)
    post(client, "/einstellungen/logo", {"zurueck": "/einstellungen/design"},
         files={"datei": ("logo.png", _png(), "image/png")})
    doc = _final(client, db)
    with pdfplumber.open(io.BytesIO(_pdf(client, doc.id))) as pdf:
        image = pdf.pages[0].images[0]
        assert image["x0"] > W / 2                      # Standard: rechts
    post(client, "/einstellungen/design", {**VALID_SETTINGS["design"], "design.logo_position": "links"})
    doc = _final(client, db)
    with pdfplumber.open(io.BytesIO(_pdf(client, doc.id))) as pdf:
        page = pdf.pages[0]
        image = page.images[0]
        assert image["x1"] < W / 2                      # links
        name = [c for c in page.chars if c["top"] < 40 * mm and c["size"] > 12]
        assert name and min(c["x0"] for c in name) > image["x1"]   # Name überlappt das Logo nicht


@pytest.mark.parametrize("vorlage", ["klassisch", "petrol"])
def test_logo_mit_speichern_knopf_landet_auf_der_rechnung(client, db, vorlage):
    """Datei auswählen und unten „Speichern“ drücken (statt „Logo hochladen“) übernimmt das Logo trotzdem."""
    _ready(client)
    page = client.get("/einstellungen/design").text
    assert 'form="einstellungen-formular"' in page and 'enctype="multipart/form-data"' in page
    r = post(client, "/einstellungen/design", {**VALID_SETTINGS["design"], "design.vorlage_rechnung": vorlage},
             files={"datei": ("logo.png", _png(), "image/png")})
    assert r.status_code == 200 and "gespeichert" in r.text
    doc = _final(client, db)
    with pdfplumber.open(io.BytesIO(_pdf(client, doc.id))) as pdf:
        assert pdf.pages[0].images, "Logo fehlt auf der Rechnung"


def test_falsches_logo_beim_speichern_wird_gemeldet(client, db):
    _ready(client)
    r = post(client, "/einstellungen/design", {**VALID_SETTINGS["design"], "design.vorlage_rechnung": "petrol"},
             files={"datei": ("logo.gif", b"GIF89a....", "image/gif")})
    assert r.status_code == 400 and "das Logo aber nicht" in r.text and "Erlaubt: JPG, PNG" in r.text
    assert "Noch kein Logo hinterlegt" in r.text
    # die übrigen Angaben sind trotzdem gespeichert
    assert client.get("/einstellungen/design").text.count('value="petrol" selected') == 1
    # ohne Datei speichert die Seite wie bisher
    r = post(client, "/einstellungen/design", VALID_SETTINGS["design"])
    assert r.status_code == 200 and "Noch kein Logo hinterlegt" in r.text
