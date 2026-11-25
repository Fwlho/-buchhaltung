"""Ist-Versteuerung: Häkchen-Zahlungen („heute“) durch Bankumsätze mit echtem Datum ersetzen, Warnhinweis,
Jahresübersicht auf der Steuerseite."""
from datetime import date
from decimal import Decimal as D

from app.services import auswertung, belege, bank
from app.formatting import format_decimal
from tests.conftest import post
from tests.test_auswertung import _ready, _with_tenant
from tests.test_bank import _Ref, _doc, _import_csv, _txs
from tests.test_rechnungen import _final

MAPPING = {"datum": "Buchungstag", "valuta": "Valutadatum", "betrag": "Betrag", "zweck": "Verwendungszweck",
           "name": "Beguenstigter/Zahlungspflichtiger", "iban": "Kontonummer/IBAN"}


def _csv(rows: list[tuple[str, str, str]]) -> bytes:
    lines = ['"Auftragskonto";"Buchungstag";"Valutadatum";"Buchungstext";"Verwendungszweck";'
             '"Beguenstigter/Zahlungspflichtiger";"Kontonummer/IBAN";"BIC (SWIFT-Code)";"Betrag";"Waehrung";"Info"']
    for day, purpose, amount in rows:
        lines.append(f'"DE89370400440532013000";"{day}";"{day}";"GUTSCHRIFT";"{purpose}";"Kunde";"";"";'
                     f'"{amount}";"EUR";"Umsatz gebucht"')
    return "\r\n".join(lines).encode("cp1252")


def _import(client, rows):
    r = _import_csv(client, _csv(rows))
    if "CSV-Spalten zuordnen" in r.text:
        post(client, "/bank/import/" + r.url.path.rsplit("/", 1)[-1], MAPPING)


def _haken(client, doc, day: date):
    r = post(client, f"/rechnungen/{doc.id}/bezahlt", {"paid_on": day.strftime("%d.%m.%Y")}, follow_redirects=False)
    assert r.status_code == 303


def test_bankumsatz_ersetzt_haekchen_zahlung(client, db):
    _ready(client)
    out = _Ref(_final(client, db, doc_date="10.03.2026", service_date="10.03.2026"))
    gross = _doc(db, out.id).gross_total
    _haken(client, _doc(db, out.id), date.today())          # nachträglich abgehakt: Datum „heute“
    today_q = auswertung.period_containing("quartal", date.today())
    late = _with_tenant(db, lambda: auswertung.ust(db, today_q))
    if today_q != auswertung.period("quartal", 2026, 1):
        assert any(h.startswith("Achtung: 1 Zahlung") for h in late.hints)

    amount = format_decimal(gross)
    _import(client, [("20.03.26", f"Rechnung {out.number}", amount)])
    tx = next(t for t in _txs(db) if t.amount > 0)
    found = _with_tenant(db, lambda: bank.suggestions(db, tx))
    assert found and found[0].doc.id == out.id and any("ersetzt die Zahlung" in r for r in found[0].reasons)
    page = client.get(f"/bank/umsatz/{tx.id}").text
    assert "ersetzt die Zahlung" in page

    r = post(client, f"/bank/umsatz/{tx.id}/zuordnen", {"beleg": str(out.id)})
    assert r.status_code == 200, r.text
    doc = _doc(db, out.id)
    assert doc.status == "bezahlt" and len(doc.payments) == 1
    assert doc.payments[0].paid_on == date(2026, 3, 20) and doc.payments[0].bank_transaction_id == tx.id
    assert doc.payments[0].note != belege.CHECK_NOTE
    q1 = _with_tenant(db, lambda: auswertung.ust(db, auswertung.period("quartal", 2026, 1)))
    assert q1.output_tax > 0 and not any(h.startswith("Achtung") for h in q1.hints)


def test_teilbetrag_ersetzt_nicht_und_datum_vor_rechnung(client, db):
    _ready(client)
    out = _Ref(_final(client, db, doc_date="10.03.2026", service_date="10.03.2026"))
    _haken(client, _doc(db, out.id), date(2026, 3, 31))
    _import(client, [("20.03.26", f"Rechnung {out.number}", "10,00"), ("05.03.26", f"Rechnung {out.number}", "1,00")])
    for tx in [t for t in _txs(db) if t.amount > 0]:
        assert not _with_tenant(db, lambda: bank.suggestions(db, tx))
    tx = next(t for t in _txs(db) if t.amount == D("10.00"))
    r = post(client, f"/bank/umsatz/{tx.id}/zuordnen", {"beleg": str(out.id)})
    assert "ganzen Betrag" in r.text
    doc = _doc(db, out.id)
    assert len(doc.payments) == 1 and doc.payments[0].note == belege.CHECK_NOTE   # bleibt unverändert


def test_gleiche_betraege_nach_datum(client, db):
    _ready(client)
    first = _Ref(_final(client, db, doc_date="10.03.2026", service_date="10.03.2026"))
    second = _Ref(_final(client, db, doc_date="10.05.2026", service_date="10.05.2026"))
    for ref in (first, second):
        _haken(client, _doc(db, ref.id), date(2026, 6, 30))
    amount = format_decimal(_doc(db, first.id).gross_total)
    _import(client, [("20.05.26", "Danke Kunde", amount), ("25.03.26", "Danke Kunde", amount)])
    by_day = {t.booking_date: t for t in _txs(db) if t.amount > 0}
    best_may = _with_tenant(db, lambda: bank.suggestions(db, by_day[date(2026, 5, 20)])[0].doc.id)
    best_march = _with_tenant(db, lambda: bank.suggestions(db, by_day[date(2026, 3, 25)]))
    assert best_may == second.id                                     # zeitlich nächste Rechnung zuerst
    assert [s.doc.id for s in best_march] == [first.id]              # die Mai-Rechnung war im März noch nicht da


def test_jahresuebersicht_auf_der_steuerseite(client, db):
    _ready(client)
    out = _Ref(_final(client, db, doc_date="10.03.2026", service_date="10.03.2026"))
    post(client, f"/rechnungen/{out.id}/zahlung", {"paid_on": "20.03.2026"})
    page = client.get("/steuern?jahr=2026&nr=1").text
    assert "Jahr 2026 gesamt" in page and 'href="/steuern?jahr=2026&amp;nr=0"' in page
    page = client.get("/steuern?jahr=2026&nr=0").text
    assert "<h2>Jahr 2026</h2>" in page and "Werte für die Voranmeldung" in page
    year = _with_tenant(db, lambda: auswertung.ust(db, auswertung.period("jahr", 2026)))
    q1 = _with_tenant(db, lambda: auswertung.ust(db, auswertung.period("quartal", 2026, 1)))
    assert year.payable == q1.payable and year.output_tax > 0
