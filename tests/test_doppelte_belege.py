"""Doppelte Eingangsrechnungen: gleicher Absender + gleiche Nummer bzw. gleiches Datum + gleicher Betrag."""
from app.database import tenant_scope
from app.services import belege
from tests.conftest import post
from tests.test_auslese import RECHNUNG_EINFACH
from tests.test_eingangsrechnungen import _all_docs, _form_of, _ready, accepted_doc, make_pdf, upload


def test_nummer_ohne_trennzeichen():
    assert belege._number_key("RE-2026 / 0815") == belege._number_key("re20260815")
    assert belege._number_key(None) == ""


def test_hinweis_beim_upload_in_listen_und_auf_dem_beleg(client, db):
    _ready(client)
    upload(client, ("original.pdf", make_pdf(RECHNUNG_EINFACH)))
    # Dieselbe Rechnung als andere Datei (z. B. Scan statt Mail) – keine identische Datei, aber gleiche Werte
    r = upload(client, ("nochmal.pdf", make_pdf(RECHNUNG_EINFACH + "\nKopie")))
    assert "Achtung, doppelt: 1 Beleg(e) – Doppelte Werte erkannt, bitte prüfen!" in r.text
    first, second = _all_docs(db)
    assert "doppelt?" in client.get("/pruefbereich").text
    page = client.get(f"/pruefbereich/{second.id}").text
    assert "Doppelte Werte erkannt, bitte prüfen!" in page and f'href="/pruefbereich/{first.id}"' in page

    # Beim Speichern noch einmal
    r = post(client, f"/pruefbereich/{second.id}", _form_of(second))
    assert "Gespeichert. Doppelte Werte erkannt, bitte prüfen!" in r.text and "Bürobedarf Schulz GmbH" in r.text
    # … auch beim Übernehmen (der erste ist inzwischen in den Eingangsrechnungen)
    r = post(client, f"/pruefbereich/{first.id}", _form_of(first, account="4930", aktion="uebernehmen"))
    assert "übernommen. Doppelte Werte erkannt" in r.text
    assert "doppelt?" in client.get("/eingangsrechnungen").text

    # Verwerfen beendet den Hinweis
    post(client, f"/pruefbereich/{second.id}/verwerfen")
    assert "doppelt?" not in client.get("/eingangsrechnungen").text
    r = post(client, f"/eingangsrechnungen/{first.id}", _form_of(_all_docs(db)[0]))
    assert "Gespeichert." in r.text and "Doppelte Werte" not in r.text


def test_gleiches_datum_und_betrag_ohne_nummer(client, db):
    _ready(client)
    first = accepted_doc(client, db)
    upload(client, ("scan.pdf", make_pdf(RECHNUNG_EINFACH.replace("RE-2026-0815", "XY"))))
    second = _all_docs(db)[-1]
    post(client, f"/pruefbereich/{second.id}", _form_of(second, number=""))
    db.expire_all()
    second = _all_docs(db)[-1]
    assert second.number is None
    with tenant_scope(db, second.tenant_id):
        assert [d.id for d in belege.duplicates_of(db, second)] == [first.id]


def test_kein_hinweis_bei_anderem_absender_oder_storno(client, db):
    _ready(client)
    first = accepted_doc(client, db)
    upload(client, ("andere.pdf", make_pdf(RECHNUNG_EINFACH.replace("Bürobedarf Schulz GmbH", "Papier Meyer KG"))))
    other = _all_docs(db)[-1]
    post(client, f"/pruefbereich/{other.id}", _form_of(other, partner_name="Papier Meyer KG"))
    assert "doppelt?" not in client.get("/pruefbereich").text

    # Stornierter Beleg zählt nicht mehr: dieselbe Rechnung darf neu erfasst werden
    post(client, f"/eingangsrechnungen/{first.id}/festschreiben", _form_of(first))
    post(client, f"/eingangsrechnungen/{first.id}/stornieren", {"grund": "falsch erfasst"})
    r = upload(client, ("neu.pdf", make_pdf(RECHNUNG_EINFACH + "\nneu")))
    assert "Achtung, doppelt" not in r.text
