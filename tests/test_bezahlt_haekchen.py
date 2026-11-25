"""Häkchen „Bezahlt“ bei Ausgangsrechnungen (Übersicht und Rechnungsseite)."""
from datetime import date, timedelta

from app.services import belege
from tests.conftest import post
from tests.test_rechnungen import _docs, _draft, _final, _ready
from tests.test_rechte import _invite


def _d(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def test_abhaken_in_der_uebersicht_mit_datum(client, db):
    _ready(client)
    doc = _final(client, db)
    page = client.get("/rechnungen?status=offen").text
    assert f'id="bezahlt-{doc.id}"' in page and 'name="zurueck" value="/rechnungen?status=offen"' in page

    r = post(client, f"/rechnungen/{doc.id}/bezahlt", {"paid_on": _d(date.today()), "zurueck": "/rechnungen?status=offen"},
             follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/rechnungen?status=offen"
    doc = next(d for d in _docs(db) if d.id == doc.id)
    assert doc.status == "bezahlt" and len(doc.payments) == 1
    payment = doc.payments[0]
    assert payment.amount == doc.gross_total and payment.paid_on == date.today() and payment.note == belege.CHECK_NOTE
    assert 'class="paid-box on"' in client.get("/rechnungen").text


def test_falsches_datum_und_entwurf(client, db):
    _ready(client)
    doc = _final(client, db)
    r = post(client, f"/rechnungen/{doc.id}/bezahlt", {"paid_on": _d(date.today() + timedelta(days=3))})
    assert "liegt in der Zukunft" in r.text
    r = post(client, f"/rechnungen/{doc.id}/bezahlt", {"paid_on": "31.02.2026"})
    assert "Rechnung" in r.text and next(d for d in _docs(db) if d.id == doc.id).status == "offen"
    draft = _draft(client, db)
    r = post(client, f"/rechnungen/{draft.id}/bezahlt", {"paid_on": _d(date.today())})
    assert "Nur festgeschriebene Rechnungen" in r.text
    assert 'id="bezahlt-' + str(draft.id) + '"' not in client.get("/rechnungen").text


def test_auf_der_rechnungsseite_und_zuruecknehmen(client, db):
    _ready(client)
    doc = _final(client, db)
    page = client.get(f"/rechnungen/{doc.id}").text
    assert "Bezahlt am" in page and f'action="/rechnungen/{doc.id}/bezahlt"' in page
    r = post(client, f"/rechnungen/{doc.id}/bezahlt", {"paid_on": _d(date.today())})
    assert "als bezahlt markiert" in r.text and f'action="/rechnungen/{doc.id}/unbezahlt"' in r.text

    r = post(client, f"/rechnungen/{doc.id}/unbezahlt")
    assert "Häkchen entfernt" in r.text
    doc = next(d for d in _docs(db) if d.id == doc.id)
    assert doc.status == "offen" and not doc.payments


def test_teilzahlung_bleibt_beim_zuruecknehmen(client, db):
    _ready(client)
    doc = _final(client, db)
    post(client, f"/rechnungen/{doc.id}/zahlung", {"paid_on": _d(date.today()), "amount": "10,00"})
    post(client, f"/rechnungen/{doc.id}/bezahlt", {"paid_on": _d(date.today())})   # Rest abhaken
    doc = next(d for d in _docs(db) if d.id == doc.id)
    assert doc.status == "bezahlt" and len(doc.payments) == 2
    post(client, f"/rechnungen/{doc.id}/unbezahlt")
    doc = next(d for d in _docs(db) if d.id == doc.id)
    assert doc.status == "offen" and [p.amount for p in doc.payments] == [10]
    # Ohne Häkchen-Zahlung gibt es nichts zurückzunehmen
    post(client, f"/rechnungen/{doc.id}/zahlung", {"paid_on": _d(date.today())})
    r = post(client, f"/rechnungen/{doc.id}/unbezahlt")
    assert "nicht per Häkchen gebucht" in r.text


def test_rechte_und_weiterleitung(client, outbox, db):
    _ready(client)
    doc = _final(client, db)
    max_client = _invite(client, outbox, "max@example.org", "mitarbeiter")   # darf abhaken, nicht zurücknehmen
    post(max_client, f"/rechnungen/{doc.id}/bezahlt", {"paid_on": _d(date.today())})
    assert next(d for d in _docs(db) if d.id == doc.id).status == "bezahlt"
    assert 'span class="paid-box on"' in max_client.get("/rechnungen").text
    assert post(max_client, f"/rechnungen/{doc.id}/unbezahlt").status_code == 403
    # Rücksprung nur innerhalb von /rechnungen
    r = post(client, f"/rechnungen/{doc.id}/unbezahlt", {"zurueck": "https://boese.example"}, follow_redirects=False)
    assert r.headers["location"] == f"/rechnungen/{doc.id}"
