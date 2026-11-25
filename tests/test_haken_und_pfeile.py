"""Häkchen „Bezahlt“ bei Eingangsrechnungen und Pfeile vorherige/nächste in den Prüfbereichen."""
from datetime import date

from tests.conftest import post
from tests.test_auslese import RECHNUNG_EINFACH
from tests.test_eingangsrechnungen import _all_docs, _ready, accepted_doc, make_pdf, upload
from tests.test_rechnungs_import import ALTE_RECHNUNG, _pending
from tests.test_rechnungs_import import _upload as upload_out
from tests.test_rechte import _invite

HEUTE = date.today().strftime("%d.%m.%Y")


# ------------------------------------------------------------ Häkchen Eingangsrechnungen
def test_haken_in_der_uebersicht_schreibt_fest_und_bucht(client, db):
    _ready(client)
    doc = accepted_doc(client, db)                       # Entwurf mit Konto
    assert doc.is_draft
    page = client.get("/eingangsrechnungen?status=offen").text
    assert "<th>Bezahlt</th>" in page and f'id="bezahlt-{doc.id}"' in page
    r = post(client, f"/eingangsrechnungen/{doc.id}/bezahlt",
             {"paid_on": HEUTE, "zurueck": "/eingangsrechnungen?status=offen"}, follow_redirects=False)
    assert r.headers["location"] == "/eingangsrechnungen?status=offen"
    db.expire_all()
    doc = next(d for d in _all_docs(db) if d.id == doc.id)
    assert doc.is_final and doc.status == "bezahlt" and doc.payments[0].amount == doc.gross_total
    assert 'class="paid-box on"' in client.get("/eingangsrechnungen").text

    r = post(client, f"/eingangsrechnungen/{doc.id}/unbezahlt")
    assert "Häkchen entfernt" in r.text
    db.expire_all()
    doc = next(d for d in _all_docs(db) if d.id == doc.id)
    assert doc.status == "offen" and not doc.payments and doc.is_final     # bleibt festgeschrieben (GoBD)


def test_haken_bei_unvollstaendigem_beleg_mit_hinweis(client, db):
    _ready(client)
    doc = accepted_doc(client, db, account="")           # ohne Konto
    r = post(client, f"/eingangsrechnungen/{doc.id}/bezahlt", {"paid_on": HEUTE})
    assert "Es fehlt: Konto" in r.text
    db.expire_all()
    assert next(d for d in _all_docs(db) if d.id == doc.id).status == "offen"


def test_haken_rechte_und_ruecksprung(client, outbox, db):
    _ready(client)
    doc = accepted_doc(client, db)
    stb = _invite(client, outbox, "stb@example.org", "steuerberater")      # nur lesen
    assert post(stb, f"/eingangsrechnungen/{doc.id}/bezahlt", {"paid_on": HEUTE}).status_code == 403
    assert f'id="bezahlt-{doc.id}"' in stb.get("/eingangsrechnungen").text   # Formular da, aber kein Kästchen
    assert 'aria-label="Rechnung' not in stb.get("/eingangsrechnungen").text.split("<tbody>")[1].split("</tbody>")[0] \
        or "paid-box" not in stb.get("/eingangsrechnungen").text.split("<tbody>")[1].split("</tbody>")[0]
    r = post(client, f"/eingangsrechnungen/{doc.id}/bezahlt", {"paid_on": HEUTE, "zurueck": "https://boese.example"},
             follow_redirects=False)
    assert r.headers["location"] == f"/eingangsrechnungen/{doc.id}"


# ------------------------------------------------------------ Pfeile im Prüfbereich
def test_pfeile_im_pruefbereich(client, db):
    _ready(client)
    for i in range(3):
        upload(client, (f"r{i}.pdf", make_pdf(RECHNUNG_EINFACH.replace("0815", f"08{i}"))))
    ids = [d.id for d in sorted(_all_docs(db), key=lambda d: d.created_at, reverse=True)]   # Reihenfolge der Liste
    first, middle, last = (client.get(f"/pruefbereich/{i}").text for i in ids)
    assert "Beleg 1 von 3" in first and f'href="/pruefbereich/{ids[1]}"' in first and "Vorheriger Beleg" not in first
    assert "Beleg 2 von 3" in middle
    assert f'class="step-arrow left" href="/pruefbereich/{ids[0]}"' in middle
    assert f'class="step-arrow right" href="/pruefbereich/{ids[2]}"' in middle
    assert "Beleg 3 von 3" in last and "Nächster Beleg" not in last
    # übernommene Belege haben keine Pfeile
    post(client, f"/pruefbereich/{ids[0]}", {"aktion": "uebernehmen", "partner_name": "X", "doc_date": "01.03.2026",
                                              "zeile-0-satz": "19", "zeile-0-brutto": "11,90"})
    assert "step-arrow" not in client.get(f"/eingangsrechnungen/{ids[0]}").text


def test_einzelner_beleg_ohne_pfeile_und_pfeile_bei_alten_rechnungen(client, db):
    _ready(client)
    upload(client, ("r.pdf", make_pdf(RECHNUNG_EINFACH)))
    assert "step-arrow" not in client.get(f"/pruefbereich/{_all_docs(db)[0].id}").text
    upload_out(client, ("a.pdf", make_pdf(ALTE_RECHNUNG)), ("b.pdf", make_pdf(ALTE_RECHNUNG.replace("2026-016", "2026-015"))))
    ids = [d.id for d in _pending(db) if d.direction == "ausgang"]
    page = client.get(f"/rechnungen/import/{ids[0]}").text
    assert "Beleg 1 von 2" in page or "Beleg 2 von 2" in page
    assert 'class="step-arrow' in page and any(f'href="/rechnungen/import/{i}"' in page for i in ids)
