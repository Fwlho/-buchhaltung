"""Alte Ausgangsrechnungen hochladen: Prüfbereich, Übernehmen (= festschreiben), Nummernkreis, bezahlt am."""
from datetime import date

from sqlalchemy import select

from app.database import tenant_scope
from app.models import AuditLog, NumberRange
from app.services import belege, rechnungs_import
from tests.conftest import post
from tests.test_eingangsrechnungen import make_pdf
from tests.test_mandantentrennung import _two_tenants
from tests.test_rechnungen import _customer, _docs, _final, _ready
from tests.test_rechte import _invite

ALTE_RECHNUNG = """Muster Service · Teststraße 1 · 12345 Teststadt
Bäckerei Krume GmbH
Hauptstr. 5
10115 Berlin
Rechnung
Rechnungsnummer: 2026-016
Rechnungsdatum: 15.02.2026
Unterhaltsreinigung Februar
Nettobetrag 1.000,00 €
zzgl. 19 % MwSt. 190,00 €
Gesamtbetrag 1.190,00 €
"""


def _upload(client, *files):
    payload = [("dateien", (name, data, "application/pdf")) for name, data in files]
    return post(client, "/rechnungen/import/hochladen", files=payload)


def _pending(db):
    return [d for d in _docs_all(db) if d.pending]


def _docs_all(db):
    from sqlalchemy.orm import selectinload

    from app.models import Document

    db.expire_all()
    stmt = (select(Document).options(selectinload(Document.lines), selectinload(Document.payments),
                                     selectinload(Document.file))
            .where(Document.direction == "ausgang").execution_options(all_tenants=True, include_pending=True)
            .order_by(Document.id))
    return list(db.scalars(stmt))


def _form(doc, **extra) -> dict:
    data = {"number": doc.number or "", "doc_date": doc.doc_date.strftime("%d.%m.%Y") if doc.doc_date else "",
            "service_date": "", "due_date": "", "customer_id": str(doc.customer_id or ""),
            "recipient_name": doc.recipient_name or "", "recipient_address": doc.recipient_address or "",
            "notes": "", "paid_on": ""}
    for i, line in enumerate(doc.lines):
        data[f"zeile-{i}-satz"] = f"{line.vat_rate.normalize():f}"
        data[f"zeile-{i}-brutto"] = f"{line.gross:.2f}".replace(".", ",")
    data.update(extra)
    return data


def test_hochladen_auslesen_und_nirgends_mitzaehlen(client, db):
    _ready(client)
    customer = _customer(client, db)          # Bäckerei Krume GmbH
    r = _upload(client, ("alt.pdf", make_pdf(ALTE_RECHNUNG)))
    assert "hochgeladen und ausgelesen" in r.text
    doc = _pending(db)[0]
    assert doc.number == "2026-016" and doc.doc_date == date(2026, 2, 15)
    assert doc.gross_total == 1190 and doc.source == "import" and doc.is_draft
    assert doc.customer_id == customer.id and "Hauptstr. 5" in doc.recipient_address
    # Solange nicht übernommen: weder in der Rechnungsliste noch im Prüfbereich der Eingangsrechnungen
    assert "2026-016" not in client.get("/rechnungen").text.split("Alte Rechnungen hochladen")[1]
    assert "(1 zu prüfen)" in client.get("/rechnungen").text
    assert "Krume" not in client.get("/pruefbereich").text
    with tenant_scope(db, doc.tenant_id):
        assert belege.pending_count(db) == 0
    page = client.get(f"/rechnungen/import/{doc.id}").text
    assert "Übernehmen (festschreiben)" in page and f"/rechnungen/import/{doc.id}/datei" in page
    assert client.get(f"/rechnungen/import/{doc.id}/datei").headers["content-type"] == "application/pdf"


def test_uebernehmen_mit_zahlung_und_nummernkreis(client, db):
    _ready(client)
    _customer(client, db)
    original = make_pdf(ALTE_RECHNUNG)
    _upload(client, ("alt.pdf", original))
    doc = _pending(db)[0]
    r = post(client, f"/rechnungen/import/{doc.id}", _form(doc, aktion="uebernehmen", paid_on="01.03.2026"))
    assert "Rechnung 2026-016 übernommen und als bezahlt gebucht" in r.text, r.text[:400]
    assert "importiert" in r.text and "Original-PDF wurde unverändert übernommen" in r.text
    doc = next(d for d in _docs_all(db) if d.id == doc.id)
    assert doc.is_final and not doc.pending and doc.status == "bezahlt"
    assert doc.payments[0].paid_on == date(2026, 3, 1) and doc.payments[0].amount == 1190
    assert doc.lines[0].title == "Leistungen lt. Rechnung 2026-016 (19 % USt)" and doc.lines[0].unit == "pauschal"
    assert doc.due_date is not None and doc.service_date == doc.doc_date
    with tenant_scope(db, doc.tenant_id):
        row = db.scalar(select(NumberRange).where(NumberRange.kind == "rechnung", NumberRange.year == 2026))
        assert row.last_number == 16
        log = db.scalar(select(AuditLog).where(AuditLog.action == "importiert_festgeschrieben"))
        assert log.changes["sha256"] == doc.file.sha256

    # Nächste selbst geschriebene Rechnung 2026 zählt dahinter weiter (heute ist 2026)
    if date.today().year == 2026:
        assert _final(client, db).number == "2026-017"
    # Festgeschrieben: Prüfseite gibt es nicht mehr, das Original bleibt
    assert client.get(f"/rechnungen/import/{doc.id}").status_code == 404
    assert client.get(f"/rechnungen/{doc.id}/pdf").content == original


def test_pflichtangaben_und_doppelte(client, db):
    _ready(client)
    pdf = make_pdf(ALTE_RECHNUNG)
    _upload(client, ("alt.pdf", pdf))
    doc = _pending(db)[0]
    r = post(client, f"/rechnungen/import/{doc.id}", _form(doc, aktion="uebernehmen", number="", recipient_address="",
                                                         paid_on="01.01.2026"))
    assert r.status_code == 400
    assert "Rechnungsnummer fehlt" in r.text and "Anschrift des Kunden fehlt" in r.text
    assert "Zahlungsdatum liegt vor dem Rechnungsdatum" in r.text
    assert _pending(db)[0].is_draft

    # Gleiche Datei nochmal -> abgelehnt; andere Datei mit gleicher Nummer -> beim Übernehmen abgelehnt
    r = _upload(client, ("nochmal.pdf", pdf))
    assert "wurde bereits hochgeladen" in r.text
    post(client, f"/rechnungen/import/{doc.id}", _form(doc, aktion="uebernehmen", recipient_address="Hauptstr. 5"))
    r = _upload(client, ("zweite.pdf", make_pdf(ALTE_RECHNUNG.replace("Februar", "März"))))
    assert "Die erkannte Rechnungsnummer 2026-016 gibt es schon" in r.text
    second = _pending(db)[0]
    assert second.number is None and "number" in second.review_fields
    r = post(client, f"/rechnungen/import/{second.id}", _form(second, aktion="uebernehmen", number="2026-016",
                                                             recipient_address="Hauptstr. 5"))
    assert r.status_code == 400 and "Die Rechnungsnummer 2026-016 gibt es schon" in r.text

    # Verwerfen löscht Beleg und Datei
    post(client, f"/rechnungen/import/{second.id}/verwerfen")
    assert not _pending(db)


def test_anschrift_aus_dem_text_ohne_stammdaten(client, db):
    _ready(client)
    _upload(client, ("alt.pdf", make_pdf(ALTE_RECHNUNG)))   # Kunde noch nicht angelegt
    doc = _pending(db)[0]
    assert doc.customer_id is None and "customer" in doc.review_fields
    assert doc.recipient_name == "Bäckerei Krume GmbH" and doc.recipient_address == "Hauptstr. 5\n10115 Berlin"
    assert rechnungs_import.address_after("A\nKunde GmbH\nWeg 1\nohne PLZ", "Kunde GmbH") is None


def test_storno_einer_importierten_rechnung(client, db):
    _ready(client)
    _customer(client, db)
    _upload(client, ("alt.pdf", make_pdf(ALTE_RECHNUNG)))
    doc = _pending(db)[0]
    post(client, f"/rechnungen/import/{doc.id}", _form(doc, aktion="uebernehmen"))
    r = post(client, f"/rechnungen/{doc.id}/stornieren", {"grund": "Doppelt erfasst"})
    assert "Stornorechnung" in r.text and "ist storniert" in r.text, r.text[:400]


def test_nur_pdf_und_rechte(client, outbox, db):
    _ready(client)
    r = _upload(client, ("bild.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64))
    assert r.status_code == 400 and "Dateityp nicht erlaubt" in r.text
    _upload(client, ("alt.pdf", make_pdf(ALTE_RECHNUNG)))
    doc = _pending(db)[0]
    # Mitarbeiter: darf hochladen und speichern, aber nicht übernehmen (kein Sonderrecht „Festschreiben“)
    max_client = _invite(client, outbox, "max@example.org", "mitarbeiter")
    assert max_client.get("/rechnungen/import").status_code == 200
    assert post(max_client, f"/rechnungen/import/{doc.id}", _form(doc, aktion="uebernehmen")).status_code == 403
    # Steuerberater: nur lesen
    stb = _invite(client, outbox, "stb@example.org", "steuerberater")
    assert stb.get(f"/rechnungen/import/{doc.id}").status_code == 200
    assert post(stb, "/rechnungen/import/hochladen", files=[("dateien", ("x.pdf", make_pdf("x"), "application/pdf"))]
                ).status_code == 403


def test_andere_firma_sieht_nichts(client, outbox, db):
    client_b, _, _ = _two_tenants(client, outbox, db)
    _upload(client, ("alt.pdf", make_pdf(ALTE_RECHNUNG)))
    doc = _pending(db)[0]
    assert client_b.get(f"/rechnungen/import/{doc.id}").status_code == 404
    assert client_b.get(f"/rechnungen/import/{doc.id}/datei").status_code == 404
    assert "Krume" not in client_b.get("/rechnungen/import").text


def test_vergabe_ueberspringt_vorhandene_nummern(client, db):
    """Zähler hinter einer hochgeladenen Nummer (z. B. von Hand zurückgesetzt) – sie wird übersprungen."""
    _ready(client)
    _customer(client, db)
    year = date.today().year
    _upload(client, ("alt.pdf", make_pdf(ALTE_RECHNUNG.replace("2026-016", f"{year}-002")
                                         .replace("15.02.2026", f"15.02.{year}"))))
    doc = _pending(db)[0]
    post(client, f"/rechnungen/import/{doc.id}", _form(doc, aktion="uebernehmen"))
    with tenant_scope(db, doc.tenant_id):
        row = db.scalar(select(NumberRange).where(NumberRange.kind == "rechnung", NumberRange.year == year))
        assert row.last_number == 2
        row.last_number = 0
        db.commit()
    assert _final(client, db).number == f"{year}-001"
    assert _final(client, db).number == f"{year}-003"     # 002 gibt es schon
