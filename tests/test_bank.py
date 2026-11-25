"""Bankabgleich: CSV/CAMT-Import, Duplikate, Vorschläge, Zuordnung, Teilzahlung, Finanzamt-Zahlungen."""
from datetime import date
from decimal import Decimal as D

from sqlalchemy import select

from app.database import tenant_scope
from app.models import BankTransaction, Document, Payment
from app.services import auswertung, bank, kontoauszug
from tests.conftest import post
from tests.test_auswertung import _ready, _with_tenant
from tests.test_eingangsrechnungen import PDF_ZWEI, _all_docs, accepted_doc, upload
from tests.test_mandantentrennung import _two_tenants
from tests.test_rechnungen import _final
from tests.test_rechte import _invite


def _csv(out_number: str) -> bytes:
    lines = [
        '"Auftragskonto";"Buchungstag";"Valutadatum";"Buchungstext";"Verwendungszweck";'
        '"Beguenstigter/Zahlungspflichtiger";"Kontonummer/IBAN";"BIC (SWIFT-Code)";"Betrag";"Waehrung";"Info"',
        f'"DE89370400440532013000";"20.03.26";"20.03.26";"GUTSCHRIFT";"Rechnung {out_number} vielen Dank";'
        '"Bäckerei Krume GmbH";"DE44500105175407324931";"";"182,29";"EUR";"Umsatz gebucht"',
        '"DE89370400440532013000";"05.04.26";"05.04.26";"LASTSCHRIFT";"RE-2026-0815 Kd 4711";'
        '"Bürobedarf Schulz GmbH";"DE02120300000000202051";"";"-489,80";"EUR";"Umsatz gebucht"',
        '"DE89370400440532013000";"10.04.26";"10.04.26";"UEBERWEISUNG";"USt-VA I/2026 St-Nr 12/345/67890";'
        '"Finanzamt Teststadt";"";"";"-120,00";"EUR";"Umsatz gebucht"',
        '"DE89370400440532013000";"11.04.26";"11.04.26";"DAUERAUFTRAG";"Miete April";"Vermieter";"";"";'
        '"-500,00";"EUR";"Umsatz gebucht"',
    ]
    return "\r\n".join(lines).encode("cp1252")


def _txs(db) -> list[BankTransaction]:
    stmt = (select(BankTransaction).execution_options(all_tenants=True, populate_existing=True)
            .order_by(BankTransaction.id))
    return list(db.scalars(stmt))


def _import_csv(client, data: bytes, name: str = "umsaetze.csv"):
    return post(client, "/bank/import", files={"datei": (name, data, "text/csv")})


class _Ref:
    """Nur IDs und Nummern merken – Objekte selbst verfallen in den Tests (Nachladen nur mit aktiver Firma)."""

    def __init__(self, doc: Document):
        self.id, self.number, self.tenant_id = doc.id, doc.number, doc.tenant_id


def _setup(client, db):
    _ready(client)
    out = _Ref(_final(client, db, doc_date="10.03.2026", service_date="10.03.2026"))
    inc = _Ref(accepted_doc(client, db))
    return out, inc


def _doc(db, doc_id: int) -> Document:
    db.expire_all()
    return next(d for d in _all_docs(db) if d.id == doc_id)


# ------------------------------------------------------------------- CSV-Import
def test_csv_import_mit_zuordnung_und_duplikaten(client, db):
    out, _ = _setup(client, db)
    r = _import_csv(client, _csv(out.number))
    assert "CSV-Spalten zuordnen" in r.text
    for selected in ('value="Buchungstag" selected', 'value="Betrag" selected',
                     'value="Verwendungszweck" selected', 'value="Beguenstigter/Zahlungspflichtiger" selected'):
        assert selected in r.text, selected
    imp_id = r.url.path.rsplit("/", 1)[-1]
    mapping = {"datum": "Buchungstag", "valuta": "Valutadatum", "betrag": "Betrag", "zweck": "Verwendungszweck",
               "name": "Beguenstigter/Zahlungspflichtiger", "iban": "Kontonummer/IBAN"}
    r = post(client, f"/bank/import/{imp_id}", mapping)
    assert "4 Umsätze eingelesen" in r.text
    txs = _txs(db)
    assert [t.amount for t in txs] == [D("182.29"), D("-489.80"), D("-120.00"), D("-500.00")]
    assert txs[0].booking_date == date(2026, 3, 20) and txs[0].name == "Bäckerei Krume GmbH"

    # Gleiche Datei noch einmal: Zuordnung ist gespeichert -> direkt eingelesen, alles schon vorhanden
    r = _import_csv(client, _csv(out.number), "nochmal.csv")
    assert "0 Umsätze eingelesen" in r.text and "4 waren schon vorhanden" in r.text
    assert len(_txs(db)) == 4


def test_fehlende_spalten_werden_gemeldet(client, db):
    out, _ = _setup(client, db)
    r = _import_csv(client, _csv(out.number))
    imp_id = r.url.path.rsplit("/", 1)[-1]
    r = post(client, f"/bank/import/{imp_id}", {"datum": "Buchungstag"})
    assert r.status_code == 400 and "Betrags-Spalte" in r.text
    assert _txs(db) == []


def _imported(client, db, out):
    r = _import_csv(client, _csv(out.number))
    imp_id = r.url.path.rsplit("/", 1)[-1]
    post(client, f"/bank/import/{imp_id}", {"datum": "Buchungstag", "betrag": "Betrag", "zweck": "Verwendungszweck",
                                            "name": "Beguenstigter/Zahlungspflichtiger", "iban": "Kontonummer/IBAN"})
    return _txs(db)


# ------------------------------------------------------------------ Vorschläge
def test_sichere_vorschlaege_mit_skonto(client, db):
    out, inc = _setup(client, db)
    upload(client, ("offen.pdf", PDF_ZWEI))  # Prüfbereich – darf nie vorgeschlagen werden
    txs = _imported(client, db, out)
    page = client.get("/bank").text
    assert "2 sichere Vorschläge übernehmen" in page and "Rechnungsnummer im Verwendungszweck" in page

    with tenant_scope(db, out.tenant_id):
        found = bank.suggestions(db, db.get(BankTransaction, txs[1].id))
        assert [s.doc.number for s in found] == ["RE-2026-0815"]
        assert found[0].skonto and found[0].sure and found[0].amount == D("489.80")
        assert all(not s.doc.pending for s in found)
    db.rollback()

    r = post(client, "/bank/vorschlaege")
    assert "2 Zahlungen zugeordnet" in r.text
    assert _doc(db, out.id).status == "bezahlt"
    doc_in = _doc(db, inc.id)
    assert doc_in.status == "bezahlt"
    pay_in = doc_in.payments[0]
    assert pay_in.amount == D("489.80") and pay_in.skonto == D("10.00") and pay_in.paid_on == date(2026, 4, 5)
    assert pay_in.bank_transaction_id == txs[1].id
    assert [t.status for t in _txs(db)] == ["erledigt", "erledigt", "offen", "offen"]


def test_teilzahlung_sammelzahlung_und_aufheben(client, db):
    out, _ = _setup(client, db)
    txs = _imported(client, db, out)
    tx = txs[0]  # +182,29
    r = post(client, f"/bank/umsatz/{tx.id}/zuordnen", {"beleg": str(out.id), "betrag": "100,00"})
    assert "Vom Umsatz ist noch ein Betrag frei" in r.text and "82,29 €" in r.text
    doc = _doc(db, out.id)
    assert doc.status == "offen" and doc.open_amount == D("82.29")

    r = post(client, f"/bank/umsatz/{tx.id}/zuordnen", {"beleg": str(out.id), "betrag": "500,00"})
    assert r.status_code == 400 and "Höchstens der noch freie Betrag" in r.text
    r = post(client, f"/bank/umsatz/{tx.id}/zuordnen", {"beleg": str(out.id), "betrag": "82,29"})
    assert "Rechnung ist bezahlt" in r.text
    assert _txs(db)[0].status == "erledigt"

    # Geldausgang passt nicht zu einer Ausgangsrechnung
    r = post(client, f"/bank/umsatz/{txs[3].id}/zuordnen", {"beleg": str(out.id)})
    assert r.status_code == 400 and "Geldeingänge passen nur zu Ausgangsrechnungen" in r.text

    r = post(client, f"/bank/umsatz/{tx.id}/aufheben")
    assert "Zuordnung aufgehoben" in r.text
    doc = _doc(db, out.id)
    assert doc.status == "offen" and doc.payments == [] and _txs(db)[0].status == "offen"


def test_finanzamt_zahlung_zaehlt_in_euer(client, db):
    out, _ = _setup(client, db)
    txs = _imported(client, db, out)
    r = post(client, f"/bank/umsatz/{txs[2].id}/markieren", {"kategorie": "ust"})
    assert "Umsatzsteuer an das / vom Finanzamt" in r.text
    post(client, f"/bank/umsatz/{txs[3].id}/markieren", {"kategorie": "ignorieren"})
    result = _with_tenant(db, lambda: auswertung.euer(db, auswertung.period("jahr", 2026)))
    assert result.vat_to_office == D("120.00") and result.vat_refund == 0
    assert result.expense_total == D("120.00")  # Eingangsrechnung noch unbezahlt
    page = client.get("/euer?zeitraum=jahr&jahr=2026").text
    assert "An das Finanzamt gezahlte Umsatzsteuer" in page and "120,00 €" in page
    # Markierte Umsätze lassen sich keiner Rechnung mehr zuordnen
    r = post(client, f"/bank/umsatz/{txs[2].id}/zuordnen", {"beleg": str(out.id)})
    assert r.status_code == 400


# ------------------------------------------------------------------------ CAMT
CAMT = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08">
 <BkToCstmrStmt><Stmt><Id>1</Id>
  <Ntry><Amt Ccy="EUR">182.29</Amt><CdtDbtInd>CRDT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts>
   <BookgDt><Dt>2026-03-21</Dt></BookgDt><ValDt><Dt>2026-03-21</Dt></ValDt>
   <NtryDtls><TxDtls><RltdPties><Dbtr><Pty><Nm>Baeckerei Krume GmbH</Nm></Pty></Dbtr>
    <DbtrAcct><Id><IBAN>DE44500105175407324931</IBAN></Id></DbtrAcct></RltdPties>
    <RmtInf><Ustrd>Rechnung {number}</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
  <Ntry><Amt Ccy="EUR">50.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts><Cd>PDNG</Cd></Sts>
   <BookgDt><Dt>2026-03-22</Dt></BookgDt></Ntry>
 </Stmt></BkToCstmrStmt>
</Document>"""


def test_camt_import(client, db):
    out, _ = _setup(client, db)
    r = post(client, "/bank/import", files={"datei": ("auszug.xml", CAMT.format(number=out.number).encode(),
                                                      "application/xml")})
    assert "1 Umsätze eingelesen" in r.text  # vorgemerkter Umsatz (PDNG) zählt nicht
    tx = _txs(db)[0]
    assert tx.amount == D("182.29") and tx.name == "Baeckerei Krume GmbH" and tx.iban == "DE44500105175407324931"
    page = client.get(f"/bank/umsatz/{tx.id}").text
    assert out.number in page and "Rechnungsnummer im Verwendungszweck" in page and "182,29 € zuordnen" in page


def test_ungueltige_dateien(client, db):
    _ready(client)
    r = post(client, "/bank/import", files={"datei": ("x.xml", b"<html><body>kein Auszug</body></html>", "text/xml")})
    assert "kein CAMT-Kontoauszug" in r.text
    r = post(client, "/bank/import", files={"datei": ("leer.csv", b"", "text/csv")})
    assert "leer" in r.text
    assert _txs(db) == []


# ------------------------------------------------------------------ Hilfsfunktionen
def test_betraege_und_spalten_erkennen():
    assert kontoauszug.parse_amount("-1.234,56") == D("-1234.56")
    assert kontoauszug.parse_amount("1234.56") == D("1234.56")
    assert kontoauszug.parse_amount("12,00 S") == D("-12.00")
    assert kontoauszug.parse_amount("+5,5") == D("5.50")
    assert kontoauszug.parse_amount("abc") is None
    assert kontoauszug.parse_date("05.04.26") == date(2026, 4, 5)
    header = ["Buchungsdatum", "Wertstellung", "Zahlungspflichtige*r", "Zahlungsempfänger*in", "Verwendungszweck",
              "Soll (EUR)", "Haben (EUR)"]
    mapping = kontoauszug.guess_mapping(header)
    assert mapping["datum"] == "Buchungsdatum" and mapping["valuta"] == "Wertstellung"
    assert mapping["soll"] == "Soll (EUR)" and mapping["haben"] == "Haben (EUR)" and "betrag" not in mapping
    rows, skipped = kontoauszug.parse_csv(
        "Buchungsdatum;Soll (EUR);Haben (EUR);Verwendungszweck\n01.02.2026;12,50;;Porto\n02.02.2026;;100,00;RE 7\n"
        "Kontostand;;;\n".encode(), {"datum": "Buchungsdatum", "soll": "Soll (EUR)", "haben": "Haben (EUR)",
                                    "zweck": "Verwendungszweck"})
    assert [r.amount for r in rows] == [D("-12.50"), D("100.00")] and skipped == 1


def test_rechnungsnummer_mit_grenzen():
    pattern = bank._number_pattern("2026-001")
    assert pattern.search("rechnung 2026-001 danke") and pattern.search("re 2026/001")
    assert not pattern.search("rechnung 2026-0012")


# ---------------------------------------------------------------------- Rechte
def test_rechte_und_mandantentrennung(client, outbox, db):
    client_b, tenant_a, tenant_b = _two_tenants(client, outbox, db)
    out = _Ref(_final(client, db, doc_date="10.03.2026", service_date="10.03.2026"))
    txs = _imported(client, db, out)
    assert client_b.get(f"/bank/umsatz/{txs[0].id}").status_code == 404
    assert "Bäckerei Krume" not in client_b.get("/bank?zeigen=alle").text
    r = post(client_b, f"/bank/umsatz/{txs[0].id}/markieren", {"kategorie": "privat"})
    assert r.status_code == 404
    with tenant_scope(db, tenant_b.id):
        assert db.scalars(select(BankTransaction)).all() == []
        assert db.scalars(select(Payment)).all() == []
    db.rollback()

    ma = _invite(client, outbox, "max@example.org", "mitarbeiter")
    assert ma.get("/bank").status_code == 403
    stb = _invite(client, outbox, "stb@example.org", "steuerberater")
    assert stb.get("/bank").status_code == 200
    assert post(stb, f"/bank/umsatz/{txs[0].id}/markieren", {"kategorie": "privat"}).status_code == 403
    assert _doc(db, out.id).status == "offen"
