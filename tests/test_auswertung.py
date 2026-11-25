"""EÜR, Umsatzsteuer (Ist/Soll), Kleinunternehmer-Grenzen, Übersicht.

Szenario 2026 (von Hand nachgerechnet):
- Ausgangsrechnung vom 10.03.2026: 19 % auf 135,20 € (USt 25,69 €) + 7 % auf 20,00 € (USt 1,40 €) = 182,29 €,
  bezahlt 100,00 € am 20.03.2026 (Q1) und 82,29 € am 15.04.2026 (Q2).
- Eingangsrechnung vom 14.03.2026: 420,00 € + 79,80 € USt = 499,80 €, 2 % Skonto (10,00 €),
  bezahlt 489,80 € am 05.04.2026 (Q2).
"""
from datetime import date
from decimal import Decimal as D

from sqlalchemy import select

from app.database import tenant_scope
from app.models import Document
from app.services import auswertung
from tests.conftest import VALID_SETTINGS, complete_wizard, ersteinrichtung, post
from tests.test_eingangsrechnungen import PDF_ZWEI, _all_docs, _form_of, accepted_doc, upload
from tests.test_mandantentrennung import _two_tenants
from tests.test_rechnungen import _customer, _draft, _final
from tests.test_rechte import _invite


def _ready(client):
    ersteinrichtung(client)
    complete_wizard(client)


def _scenario(client, db):
    out = _final(client, db, doc_date="10.03.2026", service_date="10.03.2026")
    post(client, f"/rechnungen/{out.id}/zahlung", {"paid_on": "20.03.2026", "amount": "100,00"})
    post(client, f"/rechnungen/{out.id}/zahlung", {"paid_on": "15.04.2026"})
    inc = accepted_doc(client, db)
    r = post(client, f"/eingangsrechnungen/{inc.id}/zahlung", {"paid_on": "05.04.2026", "mit_skonto": "1"})
    assert r.status_code == 200, r.text
    return out, inc


def _with_tenant(db, fn):
    tenant_id = db.scalars(select(Document.tenant_id).execution_options(all_tenants=True)).first()
    db.expire_all()
    with tenant_scope(db, tenant_id):
        return fn()


# ------------------------------------------------------------------------ EÜR
def test_euer_jahr(client, db):
    _ready(client)
    _scenario(client, db)
    r = _with_tenant(db, lambda: auswertung.euer(db, auswertung.period("jahr", 2026)))
    assert r.income_net == D("155.20") and r.vat_received == D("27.09") and r.income_total == D("182.29")
    assert [(g.label, g.net) for g in r.income] == [("Umsatzsteuerpflichtige Betriebseinnahmen", D("155.20"))]
    accounts = r.income[0].accounts
    assert accounts["8400"][1] + accounts["8300"][1] == D("155.20")
    # Ausgabe: nur der tatsächlich gezahlte Betrag (Skonto mindert Netto und Vorsteuer)
    assert r.expense_net == D("411.60") and r.vat_paid == D("78.20") and r.expense_total == D("489.80")
    assert r.surplus == D("-307.51")

    page = client.get("/euer?zeitraum=jahr&jahr=2026").text
    for text in ("182,29 €", "489,80 €", "-307,51 €", "Verlust", "8400 Erlöse 19 % USt", "Vereinnahmte Umsatzsteuer"):
        assert text in page, text


def test_euer_nach_zahlungsdatum(client, db):
    _ready(client)
    _scenario(client, db)
    q1 = _with_tenant(db, lambda: auswertung.euer(db, auswertung.period("quartal", 2026, 1)))
    assert q1.income_total == D("100.00") and q1.expense_total == 0  # Ausgabe erst im April bezahlt
    march = client.get("/euer?zeitraum=monat&jahr=2026&nr=3").text
    assert "100,00 €" in march and "März 2026" in march
    r = client.get("/euer?zeitraum=frei&von=01.04.2026&bis=30.04.2026").text
    assert "82,29 €" in r and "489,80 €" in r
    assert "Bitte einen gültigen Zeitraum" in client.get("/euer?zeitraum=frei&von=05.05.2026&bis=01.05.2026").text


def test_euer_export(client, db):
    _ready(client)
    _scenario(client, db)
    r = client.get("/euer/export.csv?zeitraum=jahr&jahr=2026")
    text = r.content.decode("utf-8")
    assert text.startswith("﻿") and "Bereich;Position;Konto;Betrag (EUR)" in text
    assert "Summe Betriebseinnahmen;;182,29" in text and "Verlust;;-307,51" in text
    r = client.get("/euer/export.pdf?zeitraum=jahr&jahr=2026")
    import io

    import pdfplumber
    with pdfplumber.open(io.BytesIO(r.content)) as pdf:
        pdf_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    assert "Einnahmen-Überschuss-Rechnung" in pdf_text and "182,29 €" in pdf_text and "Muster Service" in pdf_text


def test_pruefbereich_zaehlt_nicht(client, db):
    _ready(client)
    _scenario(client, db)
    upload(client, ("offen.pdf", PDF_ZWEI))  # liegt im Prüfbereich (Datum 02.04.2026, Vorsteuer 22,50 €)
    q2 = auswertung.period("quartal", 2026, 2)
    assert _with_tenant(db, lambda: auswertung.ust(db, q2)).input_tax == 0  # ungeprüft -> zählt nicht
    pending = _all_docs(db)[-1]
    assert pending.pending
    post(client, f"/pruefbereich/{pending.id}", _form_of(pending, account="4930", aktion="uebernehmen"))
    assert _with_tenant(db, lambda: auswertung.ust(db, q2)).input_tax == D("22.50")  # nach Übernahme schon


# --------------------------------------------------------------- Umsatzsteuer
def test_ust_ist_versteuerung(client, db):
    _ready(client)
    _scenario(client, db)
    q1, q2 = _with_tenant(db, lambda: (auswertung.ust(db, auswertung.period("quartal", 2026, 1)),
                                       auswertung.ust(db, auswertung.period("quartal", 2026, 2))))
    assert q1.output_tax + q2.output_tax == D("27.09")  # nach Zahlungseingang aufgeteilt
    assert q1.output_tax == D("14.86")
    assert q1.input_tax == D("79.80") and q1.skonto_correction == 0   # Vorsteuer nach Rechnungsdatum (März)
    assert q2.input_tax == 0 and q2.skonto_correction == D("1.60")    # Skonto-Korrektur im Zahlungsmonat
    assert q1.payable == D("-64.94")
    kz = {k: v for k, _, v in q1.kennzahlen()}
    assert kz["81"] == D("74") and kz["86"] == D("10") and kz["66"] == D("79.80") and kz["83"] == D("-64.94")
    page = client.get("/steuern?jahr=2026&nr=1").text
    assert "1. Quartal 2026" in page and "Erstattung" in page and "-64,94 €" in page and "79,80 €" in page


def test_ust_soll_versteuerung_und_storno(client, db):
    _ready(client)
    post(client, "/einstellungen/steuer", {**VALID_SETTINGS["steuer"], "steuer.versteuerung": "soll"})
    out = _final(client, db, doc_date="10.03.2026", service_date="10.03.2026")  # unbezahlt
    q1 = _with_tenant(db, lambda: auswertung.ust(db, auswertung.period("quartal", 2026, 1)))
    assert q1.output_tax == D("27.09") and q1.outputs[D(19)] == [D("135.20"), D("25.69")]
    post(client, f"/rechnungen/{out.id}/stornieren", {"grund": "Fehler"})  # Stornorechnung mit Datum heute
    storno_q = auswertung.period_containing("quartal", date.today())
    storno = _with_tenant(db, lambda: auswertung.ust(db, storno_q))
    if storno_q == auswertung.period("quartal", 2026, 1):
        assert storno.output_tax == 0          # Rechnung und Storno im selben Quartal heben sich auf
    else:
        assert storno.output_tax == D("-27.09")  # Storno mindert die Umsatzsteuer im Quartal des Stornos


def test_reverse_charge_kennzahl(client, db):
    _ready(client)
    post(client, "/einstellungen/steuer", {**VALID_SETTINGS["steuer"], "steuer.versteuerung": "soll"})
    customer = _customer(client, db, ust_id="DE123456789")  # § 13b-E-Rechnung braucht die USt-IdNr.
    _final(client, db, _draft(client, db, customer), doc_date="10.03.2026", service_date="10.03.2026", hint=["13b"])
    q1 = _with_tenant(db, lambda: auswertung.ust(db, auswertung.period("quartal", 2026, 1)))
    assert q1.output_tax == 0 and q1.reverse_charge_base == D("155.20")
    assert ("60", "Umsätze, für die der Leistungsempfänger die Steuer schuldet (§ 13b)", D("155")) in q1.kennzahlen()


def test_kleinunternehmer_grenzen(client, db):
    _ready(client)
    post(client, "/einstellungen/steuer", {**VALID_SETTINGS["steuer"], "steuer.kleinunternehmer": "1"})
    year = date.today().year
    today = date.today().strftime("%d.%m.%Y")
    _final(client, db, **{"pos-0-menge": "220000", "pos-0-preis": "0,125", "doc_date": today, "service_date": today})
    page = client.get("/steuern").text
    assert "Kleinunternehmer (§ 19 UStG)" in page and "über 25.000 €" in page and f"ab {year + 1}" in page
    assert "Kleinunternehmer-Grenze" in client.get("/").text


def test_ku_monitor_stufen(client, db):
    _ready(client)
    _final(client, db, doc_date="10.03.2026", service_date="10.03.2026")
    m = _with_tenant(db, lambda: auswertung.ku_monitor(db, 2026))
    assert m["level"] == "ok" and m["current"] == D("155.20")
    m = _with_tenant(db, lambda: auswertung.ku_monitor(db, 2027))
    assert m["previous"] == D("155.20") and m["current"] == 0


# --------------------------------------------------------------------- Übersicht
def test_uebersicht(client, outbox, db):
    _ready(client)
    _scenario(client, db)
    page = client.get("/?zeitraum=jahr&jahr=2026").text
    for text in ("Einnahmen", "155,20 €", "Ausgaben", "411,60 €", "Offene Forderungen", "Monatsverlauf 2026",
                 "Umsatzsteuer"):
        assert text in page, text
    assert "<svg" in page

    ma = _invite(client, outbox, "max@example.org", "mitarbeiter")
    page = ma.get("/").text
    assert "Offene Angebote" in page
    for text in ("155,20", "Monatsverlauf", "Umsatzsteuer <small>", "Überschuss"):
        assert text not in page, text   # Finanzzahlen nur mit Recht „EÜR“
    stb = _invite(client, outbox, "stb@example.org", "steuerberater")
    assert "Monatsverlauf 2026" in stb.get("/?zeitraum=jahr&jahr=2026").text
    assert ma.get("/euer").status_code == 403 and ma.get("/steuern").status_code == 403


def test_mandantentrennung(client, outbox, db):
    client_b, tenant_a, tenant_b = _two_tenants(client, outbox, db)
    _scenario(client, db)
    with tenant_scope(db, tenant_b.id):
        r = auswertung.euer(db, auswertung.period("jahr", 2026))
        assert r.income_total == 0 and r.expense_total == 0
        assert auswertung.ust(db, auswertung.period("quartal", 2026, 1)).input_tax == 0
        assert auswertung.open_items(db)["receivables_count"] == 0
    assert "182,29" not in client_b.get("/euer?zeitraum=jahr&jahr=2026").text
    assert "79,80" not in client_b.get("/steuern?jahr=2026&nr=1").text
