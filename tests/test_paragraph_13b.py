"""§ 13b UStG: Eingangsrechnungen (eigene Firma schuldet die Steuer) und Ausgangsrechnungen (Kunde schuldet sie)."""
from decimal import Decimal as D

from app.services import auswertung, datev, steuerschuld
from app.services.auswertung import period
from tests.conftest import VALID_SETTINGS, post
from tests.test_auswertung import _ready, _with_tenant
from tests.test_eingangsrechnungen import _all_docs, _form_of, accepted_doc
from tests.test_rechnungen import _customer, _docs, _draft, _final

Q1 = period("quartal", 2026, 1)


class _Supplier:
    def __init__(self, reverse_charge=None, ust_id=None, country=None):
        self.reverse_charge, self.ust_id, self.country = reverse_charge, ust_id, country


# ------------------------------------------------------------------ reine Funktionen
def test_zone_und_schaetzung():
    assert steuerschuld.zone("Deutschland") == "inland" and steuerschuld.zone("") == "inland"
    assert steuerschuld.zone("Österreich") == "eu" and steuerschuld.zone("ireland") == "eu"
    assert steuerschuld.zone("Schweiz") == "drittland" and steuerschuld.zone("USA") == "drittland"
    assert steuerschuld.zone("Deutschland", "ATU12345678") == "eu"      # USt-IdNr. hat Vorrang
    assert steuerschuld.zone("Irland", "DE123456789") == "inland"
    assert steuerschuld.guess_incoming(_Supplier("drittland"), "") == "drittland"
    assert steuerschuld.guess_incoming(_Supplier(country="Niederlande"), "") == "eu"
    assert steuerschuld.guess_incoming(None, "Reverse charge VAT IE 6388047V", "DE123456789") == "eu"
    assert steuerschuld.guess_incoming(None, "Steuerschuldnerschaft des Leistungsempfängers") == "inland"


# ------------------------------------------------------------------ Eingang
def _rc_doc(client, db, kind="eu", rate="19", **extra):
    doc = accepted_doc(client, db)
    form = _form_of(doc, **{"doc_date": "10.03.2026", "zeile-0-satz": "0", "zeile-0-brutto": "",
                            "zeile-0-netto": "100,00", "reverse_charge": kind, "reverse_charge_rate": rate, **extra})
    for i in range(1, 5):
        form.pop(f"zeile-{i}-brutto", None)
    r = post(client, f"/eingangsrechnungen/{doc.id}", form)
    return doc.id, r


def test_eingang_eu_kennzahlen_46_47_67(client, db):
    _ready(client)
    doc_id, r = _rc_doc(client, db)
    assert r.status_code == 200 and "Sie schulden 19,00 € Umsatzsteuer" in r.text and "Kz 46/47" in r.text
    db.expire_all()
    doc = next(d for d in _all_docs(db) if d.id == doc_id)
    assert doc.reverse_charge == "eu" and doc.reverse_charge_rate == D(19) and doc.vat_total == 0
    q1 = _with_tenant(db, lambda: auswertung.ust(db, Q1))
    kz = {k: v for k, _, v in q1.kennzahlen()}
    assert kz["46"] == D(100) and kz["47"] == D("19.00") and kz["67"] == D("19.00")
    assert q1.rc_tax == q1.rc_input_tax == D("19.00") and q1.payable == q1.output_tax - q1.input_tax_net
    page = client.get("/steuern?jahr=2026&nr=1").text
    assert "Kz 46" in page or "<strong>46</strong>" in page


def test_eingang_inland_7_prozent_und_kleinunternehmer(client, db):
    _ready(client)
    post(client, "/einstellungen/steuer", {**VALID_SETTINGS["steuer"], "steuer.kleinunternehmer": "1"})
    _rc_doc(client, db, kind="inland", rate="7")
    q1 = _with_tenant(db, lambda: auswertung.ust(db, Q1))
    kz = {k: v for k, _, v in q1.kennzahlen()}
    assert kz["84"] == D(100) and kz["85"] == D("7.00") and "67" not in kz and "66" not in kz
    assert q1.payable == D("7.00")                                   # Kleinunternehmer: kein Vorsteuerabzug
    assert any("trotzdem eine Umsatzsteuer-Voranmeldung" in h for h in q1.hints)
    page = client.get("/steuern?jahr=2026&nr=1").text
    assert "Kleinunternehmer" in page and "<strong>85</strong>" in page


def test_eingang_mit_ausgewiesener_steuer_wird_abgelehnt(client, db):
    _ready(client)
    _doc_id, r = _rc_doc(client, db, **{"zeile-0-ust": "19,00"})
    assert "darf keine Umsatzsteuer ausweisen" in r.text


def test_lieferant_mit_13b_und_datev(client, db):
    _ready(client)
    post(client, "/einstellungen/datev", {"datev.berater": "1234567", "datev.mandant": "12345",
                                          "datev.sammeldebitor": "69999", "datev.sammelkreditor": "99999"})
    doc_id, _ = _rc_doc(client, db, kind="inland")
    post(client, f"/eingangsrechnungen/{doc_id}/festschreiben")
    export = _with_tenant(db, lambda: datev.collect(db, Q1.start, Q1.end))
    rc = [b for b in export.bookings if b.ref and b.tax_key == "94"]
    assert rc and rc[0].amount == D("100.00")
    assert any("§ 13b" in p and "BU-Schlüssel 94" in p for p in export.problems)


# ------------------------------------------------------------------ Ausgang
def test_ausgang_eu_kunde_kennzahl_21_und_zm(client, db):
    _ready(client)
    post(client, "/einstellungen/steuer", {**VALID_SETTINGS["steuer"], "steuer.ust_id": "DE123456789",
                                           "steuer.versteuerung": "soll"})
    customer = _customer(client, db, ust_id="ATU12345678", country="Österreich", reverse_charge="1")
    assert customer.reverse_charge
    draft = _draft(client, db, customer)
    assert draft.hints == ["13b"] and draft.reverse_charge == "eu"     # Häkchen beim Kunden → vorausgewählt
    doc = _final(client, db, draft, doc_date="10.03.2026", service_date="10.03.2026", hint=["13b"])
    assert doc.reverse_charge == "eu" and doc.vat_total == 0
    q1 = _with_tenant(db, lambda: auswertung.ust(db, Q1))
    kz = {k: v for k, _, v in q1.kennzahlen()}
    assert kz["21"] == D(155) and "60" not in kz
    assert q1.zm == [{"ust_id": "ATU12345678", "name": doc.recipient_name, "amount": D("155.20"),
                      "amount_whole": D(155)}]
    page = client.get("/steuern?jahr=2026&nr=1").text
    assert "Zusammenfassende Meldung" in page and "ATU12345678" in page
    pdf = client.get(f"/rechnungen/{doc.id}/pdf")
    from tests.test_rechnungen import _pdf_text

    assert "Reverse charge" in _pdf_text(pdf.content)


def test_ausgang_pflichtangaben(client, db):
    _ready(client)
    eu = _customer(client, db, country="Frankreich")               # EU-Kunde ohne USt-IdNr.
    draft = _draft(client, db, eu)
    r = post(client, f"/rechnungen/{draft.id}", {**__import__("tests.test_rechnungen", fromlist=["_form"])._form(draft),
                                                 "hint": ["13b"]})
    r = post(client, f"/rechnungen/{draft.id}/festschreiben")
    assert "eigene USt-IdNr." in r.text and "USt-IdNr. des Kunden" in r.text
    swiss = _customer(client, db, country="Schweiz")
    draft = _draft(client, db, swiss)
    post(client, f"/rechnungen/{draft.id}", {**__import__("tests.test_rechnungen", fromlist=["_form"])._form(draft),
                                             "hint": ["13b"]})
    r = post(client, f"/rechnungen/{draft.id}/festschreiben")
    assert "Kunde außerhalb der EU" in r.text
    assert all(d.state == "entwurf" for d in _docs(db))
