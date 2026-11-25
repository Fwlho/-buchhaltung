"""Eingangsrechnungen: Upload -> Prüfbereich -> Übernehmen -> Festschreiben -> Zahlungen."""
import io
import re
import zipfile
from datetime import date
from decimal import Decimal as D

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.models import AuditLog, Document, Payment, StoredFile, Supplier
from app.services import belege
from tests.conftest import complete_wizard, ersteinrichtung, post
from tests.test_auslese import RECHNUNG_EINFACH, RECHNUNG_ZWEI_SAETZE
from tests.test_mandantentrennung import _two_tenants
from tests.test_rechte import _invite


def make_pdf(text: str) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for line in text.splitlines():
        c.drawString(50, y, line)
        y -= 16
    c.save()
    return buf.getvalue()


PDF_EINFACH = make_pdf(RECHNUNG_EINFACH)
PDF_ZWEI = make_pdf(RECHNUNG_ZWEI_SAETZE)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _ready(client):
    ersteinrichtung(client)
    complete_wizard(client)


def upload(client, *files):
    payload = [("dateien", (name, data, "application/octet-stream")) for name, data in files]
    return post(client, "/eingangsrechnungen/hochladen", files=payload)


def _all_docs(db) -> list[Document]:
    """Alle Belege aller Firmen inkl. Prüfbereich – Positionen, Zahlungen und Datei gleich mitladen,
    weil späteres Nachladen ohne aktive Firma (zu Recht) gesperrt ist."""
    stmt = (select(Document).options(selectinload(Document.lines), selectinload(Document.payments),
                                     selectinload(Document.file))
            .execution_options(all_tenants=True, include_pending=True).order_by(Document.id))
    return list(db.scalars(stmt))


def _form_of(doc: Document, **overrides) -> dict:
    def d(value):
        return value.strftime("%d.%m.%Y") if value else ""

    data = {"partner_name": doc.partner_name or "", "number": doc.number or "", "doc_date": d(doc.doc_date),
            "due_date": d(doc.due_date), "skonto_until": d(doc.skonto_until),
            "skonto_percent": f"{doc.skonto_percent.normalize():f}".replace(".", ",") if doc.skonto_percent else "",
            "account": doc.account or "", "supplier_id": str(doc.supplier_id or ""), "notes": doc.notes or ""}
    for i, line in enumerate(doc.lines):
        data[f"zeile-{i}-satz"] = f"{line.vat_rate.normalize():f}"
        data[f"zeile-{i}-brutto"] = f"{line.gross:.2f}".replace(".", ",")
    data.update(overrides)
    return data


def accepted_doc(client, db, pdf=PDF_EINFACH, name="r.pdf", **overrides) -> Document:
    """Hochladen und im Prüfbereich mit Konto übernehmen."""
    upload(client, (name, pdf))
    doc = _all_docs(db)[-1]
    r = post(client, f"/pruefbereich/{doc.id}", _form_of(doc, **{"account": "4930", "aktion": "uebernehmen",
                                                                 **overrides}))
    assert r.status_code == 200, r.text
    db.expire_all()
    return _all_docs(db)[-1]


# ------------------------------------------------------------- Upload/Prüfbereich
def test_upload_landet_im_pruefbereich(client, db):
    _ready(client)
    r = upload(client, ("rechnung.pdf", PDF_EINFACH))
    assert "im Prüfbereich prüfen" in r.text
    doc = _all_docs(db)[0]
    assert doc.pending and doc.is_draft
    assert doc.number == "RE-2026-0815" and doc.gross_total == D("499.80")
    assert doc.skonto_percent == D("2") and doc.due_date == date(2026, 3, 28)
    assert "partner" in doc.review_fields
    assert 'class="field unsure' in r.text  # Absender nur geraten -> gelb markiert

    # Zählt nirgends mit: nicht in der Liste, aber auf der Übersicht als Hinweis
    assert "Bürobedarf Schulz" not in client.get("/eingangsrechnungen").text
    assert "1</strong> Beleg(e)" in client.get("/").text
    with_tenant = db.scalars(select(Document).execution_options(all_tenants=True)).all()
    assert with_tenant == []  # ohne include_pending unsichtbar


def test_vorschau_datei(client, db):
    _ready(client)
    upload(client, ("rechnung.pdf", PDF_EINFACH))
    doc = _all_docs(db)[0]
    page = client.get(f"/pruefbereich/{doc.id}").text
    assert f'src="/pruefbereich/{doc.id}/datei"' in page
    r = client.get(f"/pruefbereich/{doc.id}/datei")
    assert r.status_code == 200 and r.content == PDF_EINFACH
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert r.headers["content-disposition"].startswith("inline")
    # Über den falschen Bereich nicht erreichbar
    assert client.get(f"/eingangsrechnungen/{doc.id}").status_code == 404


def test_mehrere_dateien_doppelt_und_falscher_typ(client, db):
    _ready(client)
    r = upload(client, ("a.pdf", PDF_EINFACH), ("b.pdf", PDF_ZWEI), ("bild.png", PNG))
    assert "3 Beleg(e) hochgeladen" in r.text
    r = upload(client, ("nochmal.pdf", PDF_EINFACH), ("virus.exe", b"MZ\x90\x00"))
    assert r.status_code == 400
    assert "bereits hochgeladen" in r.text and "Dateityp nicht erlaubt" in r.text
    assert len(_all_docs(db)) == 3
    files = db.scalars(select(StoredFile).execution_options(all_tenants=True)).all()
    assert len(files) == 3 and all(len(f.sha256) == 64 for f in files)
    bild = next(d for d in _all_docs(db) if d.file.content_type == "image/png")
    assert "OCR" in (bild.notes or "")


def test_uebernehmen_braucht_pflichtangaben(client, db):
    _ready(client)
    upload(client, ("bild.png", PNG))
    doc = _all_docs(db)[0]
    r = post(client, f"/pruefbereich/{doc.id}", {"aktion": "uebernehmen"})
    assert "noch nicht übernommen" in r.text and "Absender" in r.text and "Beträge" in r.text
    db.expire_all()
    assert _all_docs(db)[0].pending

    r = post(client, f"/pruefbereich/{doc.id}", {"partner_name": "Tankstelle", "doc_date": "01.04.2026",
                                                "zeile-0-satz": "19", "zeile-0-brutto": "59,50",
                                                "aktion": "uebernehmen"})
    assert "zu den Eingangsrechnungen übernommen" in r.text
    db.expire_all()
    doc = _all_docs(db)[0]
    assert not doc.pending and doc.reviewed_at is not None
    assert (doc.net_total, doc.vat_total, doc.gross_total) == (D("50.00"), D("9.50"), D("59.50"))
    assert "Tankstelle" in client.get("/eingangsrechnungen").text


def test_betraege_berechnen_und_pruefen(client, db):
    _ready(client)
    doc = accepted_doc(client, db)
    data = _form_of(doc, **{"zeile-0-satz": "19", "zeile-0-netto": "100,00", "zeile-0-brutto": "",
                            "zeile-1-satz": "7", "zeile-1-brutto": "10,70"})
    r = post(client, f"/eingangsrechnungen/{doc.id}", data)
    assert "Gespeichert" in r.text
    db.expire_all()
    doc = _all_docs(db)[0]
    assert [(l.vat_rate, l.net, l.vat, l.gross) for l in doc.lines] == [
        (D("19"), D("100.00"), D("19.00"), D("119.00")), (D("7"), D("10.00"), D("0.70"), D("10.70"))]
    assert doc.gross_total == D("129.70")

    r = post(client, f"/eingangsrechnungen/{doc.id}", _form_of(doc, **{
        "zeile-0-netto": "100,00", "zeile-0-ust": "5,00", "zeile-0-brutto": ""}))
    assert r.status_code == 400
    assert "passt nicht zum Satz" in r.text
    r = post(client, f"/eingangsrechnungen/{doc.id}", _form_of(doc, doc_date="31.02.2026", account="12"))
    assert "kein gültiges Datum" in r.text and "4 bis 8 Ziffern" in r.text


def test_festschreiben(client, db):
    _ready(client)
    doc = accepted_doc(client, db, account="")
    r = post(client, f"/eingangsrechnungen/{doc.id}/festschreiben")
    assert "Es fehlt: Konto" in r.text
    post(client, f"/eingangsrechnungen/{doc.id}", _form_of(doc, account="4930"))
    r = post(client, f"/eingangsrechnungen/{doc.id}/festschreiben")
    assert "festgeschrieben" in r.text
    db.expire_all()
    doc = _all_docs(db)[0]
    assert doc.is_final and doc.finalized_at

    # Danach unveränderbar und nicht löschbar
    r = post(client, f"/eingangsrechnungen/{doc.id}", _form_of(doc, number="GEÄNDERT"))
    assert "nicht mehr geändert" in r.text
    r = post(client, f"/eingangsrechnungen/{doc.id}/loeschen")
    assert "nicht gelöscht" in r.text
    db.expire_all()
    assert _all_docs(db)[0].number == "RE-2026-0815"


def test_zahlung_schreibt_fest_und_skonto(client, db):
    _ready(client)
    doc = accepted_doc(client, db)
    assert doc.is_draft and doc.skonto_percent == D("2")
    page = client.get(f"/eingangsrechnungen/{doc.id}").text
    assert "Abzug 10,00 €" in page  # 2 % von 499,80 € = 9,996 -> 10,00 €

    r = post(client, f"/eingangsrechnungen/{doc.id}/zahlung", {"paid_on": "20.03.2026", "mit_skonto": "1"})
    assert "vollständig bezahlt" in r.text
    db.expire_all()
    doc = _all_docs(db)[0]
    assert doc.is_final  # automatisch festgeschrieben
    assert doc.status == "bezahlt"
    p = doc.payments[0]
    assert (p.amount, p.skonto, p.paid_on) == (D("489.80"), D("10.00"), date(2026, 3, 20))
    log = db.scalars(select(AuditLog).where(AuditLog.action == "festgeschrieben")).one()
    assert log.changes == {"anlass": "erste Zahlung"}


def test_teilzahlungen_und_ueberzahlung(client, db):
    _ready(client)
    doc = accepted_doc(client, db)
    r = post(client, f"/eingangsrechnungen/{doc.id}/zahlung", {"paid_on": "20.03.2026", "amount": "600,00"})
    assert r.status_code == 400 and "Mehr als der offene Betrag" in r.text
    post(client, f"/eingangsrechnungen/{doc.id}/zahlung", {"paid_on": "20.03.2026", "amount": "200,00"})
    db.expire_all()
    doc = _all_docs(db)[0]
    assert doc.status == "offen" and doc.open_amount == D("299.80")
    post(client, f"/eingangsrechnungen/{doc.id}/zahlung", {"paid_on": "25.03.2026"})  # Rest
    db.expire_all()
    doc = _all_docs(db)[0]
    assert doc.status == "bezahlt" and doc.open_amount == 0
    # Zahlung entfernen -> wieder offen
    post(client, f"/eingangsrechnungen/{doc.id}/zahlung/{doc.payments[1].id}/loeschen")
    db.expire_all()
    assert _all_docs(db)[0].status == "offen"


def test_zahlung_bei_unvollstaendigem_entwurf(client, db):
    _ready(client)
    doc = accepted_doc(client, db, account="")
    r = post(client, f"/eingangsrechnungen/{doc.id}/zahlung", {"paid_on": "20.03.2026"})
    assert r.status_code == 400 and "Konto" in r.text
    assert db.scalars(select(Payment).execution_options(all_tenants=True)).all() == []


def test_stornieren(client, db):
    _ready(client)
    doc = accepted_doc(client, db)
    r = post(client, f"/eingangsrechnungen/{doc.id}/stornieren", {"grund": "falsch"})
    assert "Entwürfe können gelöscht werden" in r.text
    post(client, f"/eingangsrechnungen/{doc.id}/festschreiben")
    r = post(client, f"/eingangsrechnungen/{doc.id}/stornieren", {"grund": ""})
    assert "Grund" in r.text
    r = post(client, f"/eingangsrechnungen/{doc.id}/stornieren", {"grund": "Doppelt erhalten"})
    assert "Storniert: Doppelt erhalten" in r.text
    db.expire_all()
    doc = _all_docs(db)[0]
    assert doc.status == "storniert" and doc.open_amount == 0


def test_entwurf_loeschen_entfernt_datei(client, db):
    _ready(client)
    doc = accepted_doc(client, db)
    path = get_settings().files_dir / doc.file.path
    assert path.exists()
    r = post(client, f"/eingangsrechnungen/{doc.id}/loeschen")
    assert "Entwurf gelöscht" in r.text
    assert _all_docs(db) == [] and not path.exists()
    assert db.scalars(select(AuditLog).where(AuditLog.action == "geloescht")).one()


def test_duplizieren_zip_drucken(client, db):
    _ready(client)
    a = accepted_doc(client, db, PDF_EINFACH, "a.pdf")
    b = accepted_doc(client, db, PDF_ZWEI, "b.pdf")
    r = post(client, "/eingangsrechnungen/auswahl", {"ids": [str(a.id), str(b.id)], "aktion": "zip"})
    assert r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert len(names) == 2 and all(n.endswith(".pdf") for n in names)

    r = post(client, "/eingangsrechnungen/auswahl", {"ids": [str(a.id), str(b.id)], "aktion": "drucken"})
    assert r.headers["content-type"] == "application/pdf"
    from pypdf import PdfReader
    assert len(PdfReader(io.BytesIO(r.content)).pages) == 2

    r = post(client, "/eingangsrechnungen/auswahl", {"ids": [str(a.id)], "aktion": "duplizieren"})
    assert "Kopien liegen im Prüfbereich" in r.text
    copy = _all_docs(db)[-1]
    assert copy.pending and copy.number == "RE-2026-0815 (Kopie)" and copy.file_id == a.file_id
    # Kopie verwerfen löscht die gemeinsame Datei NICHT
    post(client, f"/pruefbereich/{copy.id}/verwerfen")
    assert (get_settings().files_dir / a.file.path).exists()


def test_lieferant_anlegen_und_kontovorschlag(client, db):
    _ready(client)
    upload(client, ("a.pdf", PDF_EINFACH))
    doc = _all_docs(db)[0]
    r = post(client, f"/belege/{doc.id}/lieferant-anlegen")
    assert "Lieferant 70000" in r.text
    db.expire_all()
    supplier = db.scalars(select(Supplier).execution_options(all_tenants=True)).one()
    doc = _all_docs(db)[0]
    assert doc.supplier_id == supplier.id
    post(client, f"/pruefbereich/{doc.id}", _form_of(doc, account="4930", aktion="uebernehmen"))

    # USt-IdNr. beim Lieferanten hinterlegen -> nächster Beleg wird erkannt, Konto vorgeschlagen
    post(client, f"/lieferanten/{supplier.id}", {"name": supplier.name, "ust_id": "DE987654321",
                                                 "country": "Deutschland"})
    text2 = RECHNUNG_EINFACH.replace("RE-2026-0815", "RE-2026-0900")
    upload(client, ("b.pdf", make_pdf(text2)))
    db.expire_all()
    second = _all_docs(db)[-1]
    assert second.supplier_id == supplier.id and second.account == "4930"
    assert "partner" not in second.review_fields


def test_liste_filter_und_summen(client, db):
    _ready(client)
    a = accepted_doc(client, db, PDF_EINFACH, "a.pdf")
    accepted_doc(client, db, PDF_ZWEI, "b.pdf")
    page = client.get("/eingangsrechnungen").text
    assert "672,30 €" in page  # 499,80 + 172,50
    post(client, f"/eingangsrechnungen/{a.id}/zahlung", {"paid_on": "20.03.2026"})
    page = client.get("/eingangsrechnungen?status=offen").text
    assert "Großhandel Nord" in page and "Bürobedarf Schulz" not in page
    assert "Großhandel Nord" in client.get("/eingangsrechnungen?q=nord").text
    assert "Großhandel" not in client.get("/eingangsrechnungen?jahr=2025").text


# -------------------------------------------------------------------- Rechte
def test_rechte(client, outbox, db):
    _ready(client)
    upload(client, ("a.pdf", PDF_EINFACH))
    doc = _all_docs(db)[0]
    ma = _invite(client, outbox, "max@example.org", "mitarbeiter")
    assert ma.get("/eingangsrechnungen").status_code == 403
    assert ma.get("/pruefbereich").status_code == 403
    assert ma.get(f"/pruefbereich/{doc.id}/datei").status_code == 403

    stb = _invite(client, outbox, "stb@example.org", "steuerberater")
    assert stb.get("/pruefbereich").status_code == 200
    assert post(stb, f"/pruefbereich/{doc.id}", {"aktion": "uebernehmen"}).status_code == 403
    assert post(stb, "/eingangsrechnungen/hochladen", files=[("dateien", ("x.pdf", PDF_ZWEI, "application/pdf"))]
                ).status_code == 403

    # Buchhaltung darf festschreiben
    bh = _invite(client, outbox, "bea@example.org", "buchhaltung")
    post(client, f"/pruefbereich/{doc.id}", _form_of(doc, account="4930", aktion="uebernehmen"))
    r = post(bh, f"/eingangsrechnungen/{doc.id}/festschreiben")
    assert "festgeschrieben" in r.text


# ---------------------------------------------------------- Mandantentrennung
def test_belege_anderer_firma_unerreichbar(client, outbox, db):
    client_b, tenant_a, tenant_b = _two_tenants(client, outbox, db)
    a = accepted_doc(client, db, PDF_EINFACH, "a.pdf")
    upload(client, ("b.pdf", PDF_ZWEI))
    pending = _all_docs(db)[-1]
    assert a.tenant_id == pending.tenant_id == tenant_a.id

    assert "Bürobedarf" not in client_b.get("/eingangsrechnungen").text
    assert "Großhandel" not in client_b.get("/pruefbereich").text
    assert "Prüfbereich warten" not in client_b.get("/").text
    for url in (f"/eingangsrechnungen/{a.id}", f"/eingangsrechnungen/{a.id}/datei", f"/pruefbereich/{pending.id}",
                f"/pruefbereich/{pending.id}/datei"):
        assert client_b.get(url).status_code == 404, url
    for url, data in ((f"/eingangsrechnungen/{a.id}", {"number": "X"}),
                      (f"/eingangsrechnungen/{a.id}/zahlung", {"paid_on": "01.04.2026"}),
                      (f"/eingangsrechnungen/{a.id}/loeschen", {}),
                      (f"/pruefbereich/{pending.id}/verwerfen", {}),
                      (f"/belege/{a.id}/lieferant-anlegen", {})):
        assert post(client_b, url, data).status_code == 404, url
    r = post(client_b, "/eingangsrechnungen/auswahl", {"ids": [str(a.id)], "aktion": "zip"})
    assert "mindestens einen Beleg" in r.text

    # Gleiche Datei darf in einer anderen Firma hochgeladen werden (Duplikat-Prüfung je Firma)
    r = upload(client_b, ("a.pdf", PDF_EINFACH))
    assert "1 Beleg(e) hochgeladen" in r.text
    assert len(_all_docs(db)) == 3


def test_service_pending_count_ohne_web(client, db):
    _ready(client)
    upload(client, ("a.pdf", PDF_EINFACH))
    tenant_id = _all_docs(db)[0].tenant_id
    from app.database import tenant_scope
    with tenant_scope(db, tenant_id):
        assert belege.pending_count(db) == 1
        assert belege.incoming(db) == []
    assert re.search(r"\d", "1")


def test_konto_ohne_speichern_bei_zahlung_und_festschreiben(client, db):
    """Konto oben eintragen und direkt „Zahlung erfassen“ bzw. „Festschreiben“ drücken (ohne „Speichern“):
    die Angaben kommen mit demselben Formular und werden zuerst gespeichert."""
    _ready(client)
    for action in ("zahlung", "festschreiben"):
        doc = accepted_doc(client, db, make_pdf(RECHNUNG_EINFACH.replace("0815", action)), f"{action}.pdf",
                           account="")
        assert doc.account is None and doc.is_draft
        page = client.get(f"/eingangsrechnungen/{doc.id}").text
        assert 'id="beleg-form"' in page and f'formaction="/eingangsrechnungen/{doc.id}/{action}"' in page
        data = {**_form_of(doc, account="4930"), "paid_on": "20.03.2026", "amount": ""}
        r = post(client, f"/eingangsrechnungen/{doc.id}/{action}", data)
        assert r.status_code == 200 and "Es fehlt" not in r.text, r.text[:500]
        db.expire_all()
        doc = next(d for d in _all_docs(db) if d.id == doc.id)
        assert doc.account == "4930" and doc.is_final
        assert bool(doc.payments) == (action == "zahlung")

    # Fehlerhafte Angaben oben: nichts wird bezahlt, Fehler wird am Feld angezeigt
    doc = accepted_doc(client, db, make_pdf(RECHNUNG_EINFACH.replace("0815", "x1")), "x1.pdf", account="")
    r = post(client, f"/eingangsrechnungen/{doc.id}/zahlung", {**_form_of(doc, account="49x"), "paid_on": "20.03.2026"})
    assert r.status_code == 400 and "Kontonummer mit 4 bis 8 Ziffern" in r.text
    db.expire_all()
    assert not next(d for d in _all_docs(db) if d.id == doc.id).payments
