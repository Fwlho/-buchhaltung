"""Automatische Auslese von Rechnungsdaten (PDF-Textebene)."""
import io
from datetime import date
from decimal import Decimal as D

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.services.auslese import OwnCompany, Partner, parse, pdf_text
from app.services.betraege import AmountError, TaxLine, complete_line, skonto_amount, split_by_rate

OWN = OwnCompany(name="Muster Service", iban="DE89370400440532013000")

RECHNUNG_EINFACH = """Bürobedarf Schulz GmbH · Papierweg 3 · 20095 Hamburg
Muster Service
Teststraße 1
12345 Teststadt
Rechnung
Rechnungsnummer: RE-2026-0815
Rechnungsdatum: 14.03.2026
Kundennummer: 4711
Pos. Artikel Menge Preis
1 Kopierpapier A4 10 42,00
Nettobetrag 420,00 €
zzgl. 19 % MwSt. 79,80 €
Gesamtbetrag 499,80 €
Zahlbar innerhalb von 14 Tagen ohne Abzug.
Bei Zahlung innerhalb von 7 Tagen gewähren wir 2 % Skonto.
USt-IdNr. DE987654321 · IBAN DE02 1203 0000 0000 2020 51
"""

RECHNUNG_ZWEI_SAETZE = """Großhandel Nord KG
Rechnung Nr. 2026/117
Datum: 02.04.2026
Netto 19 %: 100,00 €
Netto 7 %: 50,00 €
USt 19 % auf 100,00 € 19,00 €
USt 7 % auf 50,00 € 3,50 €
Rechnungsbetrag 172,50 €
fällig am 16.04.2026
"""

RECHNUNG_KLEINUNTERNEHMER = """Hausmeister Kramer
Rechnung 17
Rechnungsdatum 5. Mai 2026
Arbeitszeit 3 Std. 120,00 €
Gesamtbetrag 120,00 €
Gemäß § 19 UStG wird keine Umsatzsteuer berechnet.
"""


def test_einfache_rechnung():
    r = parse(RECHNUNG_EINFACH, own=OWN)
    assert r.number == "RE-2026-0815"
    assert r.doc_date == date(2026, 3, 14)
    assert r.due_date == date(2026, 3, 28)
    assert r.skonto_percent == D("2") and r.skonto_until == date(2026, 3, 21)
    assert r.gross == D("499.80")
    assert [(x.rate, x.net, x.vat) for x in r.lines] == [(D("19"), D("420.00"), D("79.80"))]
    assert r.partner_name == "Bürobedarf Schulz GmbH"
    assert "partner" in r.uncertain  # nur geraten, kein bekannter Lieferant
    assert not ({"number", "doc_date", "lines", "gross"} & r.uncertain)


def test_bekannter_lieferant_ueber_ust_id():
    partners = [Partner(id=7, name="Schulz Bürobedarf", ust_id="DE987654321")]
    r = parse(RECHNUNG_EINFACH, partners=partners, own=OWN)
    assert r.supplier_id == 7 and r.partner_name == "Schulz Bürobedarf"
    assert "partner" not in r.uncertain


def test_eigene_firma_wird_nicht_als_absender_erkannt():
    text = "Muster Service GmbH\nTeststraße 1\nRechnung Nr. 5\nGesamtbetrag 10,00 €\n"
    r = parse(text, own=OwnCompany(name="Muster Service GmbH"))
    assert r.partner_name != "Muster Service GmbH"


def test_zwei_steuersaetze():
    r = parse(RECHNUNG_ZWEI_SAETZE, own=OWN)
    assert r.number == "2026/117"
    assert r.doc_date == date(2026, 4, 2)
    assert r.due_date == date(2026, 4, 16)
    rates = {x.rate: (x.net, x.vat) for x in r.lines}
    assert rates == {D("19"): (D("100.00"), D("19.00")), D("7"): (D("50.00"), D("3.50"))}
    assert r.gross == D("172.50")
    assert "lines" not in r.uncertain
    assert r.partner_name == "Großhandel Nord KG"


def test_kleinunternehmer_ohne_umsatzsteuer():
    r = parse(RECHNUNG_KLEINUNTERNEHMER, own=OWN)
    assert r.doc_date == date(2026, 5, 5)
    assert [(x.rate, x.net, x.vat, x.gross) for x in r.lines] == [(D("0"), D("120.00"), D("0.00"), D("120.00"))]
    assert r.number == "17"


def test_leerer_text_scan():
    r = parse("   ")
    assert {"number", "doc_date", "partner", "lines"} <= r.uncertain
    assert r.hints


def test_pdf_textebene():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for line in RECHNUNG_EINFACH.splitlines():
        c.drawString(50, y, line)
        y -= 16
    c.save()
    text = pdf_text(buf.getvalue())
    r = parse(text, own=OWN)
    assert r.number == "RE-2026-0815" and r.gross == D("499.80")


def test_kaputte_pdf_liefert_leeren_text():
    assert pdf_text(b"%PDF-1.4 kaputt") == ""


# ------------------------------------------------------- Betragsberechnung
def test_fehlende_betraege_ergaenzen():
    assert complete_line(D(19), D("100.00"), None, None) == TaxLine(D(19), D("100.00"), D("19.00"), D("119.00"))
    assert complete_line(D(19), None, None, D("119.00")) == TaxLine(D(19), D("100.00"), D("19.00"), D("119.00"))
    assert complete_line(D(7), None, D("7.00"), None) == TaxLine(D(7), D("100.00"), D("7.00"), D("107.00"))
    assert complete_line(D(19), None, D("19.00"), D("119.00")).net == D("100.00")


def test_widerspruechliche_betraege():
    import pytest

    with pytest.raises(AmountError):
        complete_line(D(19), D("100.00"), D("19.00"), D("120.00"))
    with pytest.raises(AmountError):
        complete_line(D(19), D("100.00"), D("7.00"), None)
    with pytest.raises(AmountError):
        complete_line(D(19), None, None, None)


def test_skonto_anteilig_verteilen():
    lines = [TaxLine(D(19), D("100.00"), D("19.00"), D("119.00")), TaxLine(D(7), D("50.00"), D("3.50"), D("53.50"))]
    s = skonto_amount(D("172.50"), D(2))
    assert s == D("3.45")
    parts = split_by_rate(s, lines)
    assert sum(p.gross for p in parts) == s
    assert all(p.net + p.vat == p.gross for p in parts)
    by_rate = {p.rate: p for p in parts}
    assert by_rate[D(19)].gross == D("2.38") and by_rate[D(7)].gross == D("1.07")
