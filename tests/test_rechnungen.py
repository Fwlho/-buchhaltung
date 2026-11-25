"""Ausgangsrechnungen: Entwurf, Positionen, Pflichtangaben, Festschreiben, Nummernkreis, PDF, Storno."""
import io
import zipfile
from datetime import date
from decimal import Decimal as D

import pdfplumber
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import AuditLog, CatalogItem, Customer, Document, Membership, NumberRange, User
from app.services.pdf_rechnung import girocode_payload
from tests.conftest import VALID_SETTINGS, complete_wizard, ersteinrichtung, post, read_stored
from tests.test_mandantentrennung import _two_tenants
from tests.test_rechte import _invite
from tests.test_stammdaten import KUNDE

YEAR = date.today().year
TODAY = date.today().strftime("%d.%m.%Y")


def _ready(client):
    ersteinrichtung(client)
    complete_wizard(client)


def _docs(db) -> list[Document]:
    stmt = (select(Document).options(selectinload(Document.lines), selectinload(Document.payments),
                                     selectinload(Document.file))
            .where(Document.direction == "ausgang").execution_options(all_tenants=True).order_by(Document.id))
    db.expire_all()
    return list(db.scalars(stmt))


def _customer(client, db, **overrides) -> Customer:
    post(client, "/kunden/neu", {**KUNDE, **overrides})
    db.expire_all()
    return db.scalars(select(Customer).execution_options(all_tenants=True).order_by(Customer.id.desc())).first()


def _draft(client, db, customer=None) -> Document:
    customer = customer or _customer(client, db)
    r = post(client, "/rechnungen/neu", {"customer_id": str(customer.id)})
    assert "Entwurf angelegt" in r.text, r.text
    return _docs(db)[-1]


POSITIONEN = {
    "pos-0-titel": "Unterhaltsreinigung", "pos-0-beschreibung": "Büroflächen EG", "pos-0-menge": "250",
    "pos-0-einheit": "m²", "pos-0-preis": "0,125", "pos-0-satz": "19",
    "pos-1-titel": "Fensterreinigung", "pos-1-menge": "3", "pos-1-einheit": "Std.", "pos-1-preis": "38,50",
    "pos-1-rabatt": "10", "pos-1-satz": "19",
    "pos-2-titel": "Fachbuch", "pos-2-menge": "1", "pos-2-einheit": "Stück", "pos-2-preis": "20", "pos-2-satz": "7",
}


def _form(doc: Document, **extra) -> dict:
    data = {"recipient_name": doc.recipient_name, "recipient_address": doc.recipient_address,
            "doc_date": TODAY, "service_date": TODAY, "payment_terms_days": "14", "payment_qr": "girocode",
            "intro_text": doc.intro_text or "", "closing_text": doc.closing_text or ""}
    data.update(POSITIONEN)
    data.update(extra)
    return data


def _final(client, db, doc=None, **extra) -> Document:
    doc = doc or _draft(client, db)
    r = post(client, f"/rechnungen/{doc.id}", _form(doc, **extra))
    assert "Gespeichert" in r.text, r.text
    r = post(client, f"/rechnungen/{doc.id}/festschreiben")
    assert "festgeschrieben" in r.text, r.text
    return next(d for d in _docs(db) if d.id == doc.id)


def _pdf_text(data: bytes) -> str:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)


# ------------------------------------------------------------------- Entwurf
def test_entwurf_uebernimmt_kundendaten(client, db):
    _ready(client)
    customer = _customer(client, db, payment_terms_days="30", billing_name="Krume Buchhaltung",
                         billing_street="Postfach 1", billing_zip="10001", billing_city="Berlin")
    doc = _draft(client, db, customer)
    assert doc.number is None and doc.is_draft and not doc.pending
    assert doc.recipient_name == "Krume Buchhaltung" and "Postfach 1" in doc.recipient_address
    assert doc.payment_terms_days == 30 and doc.doc_date == date.today()
    assert doc.intro_text == VALID_SETTINGS["design"]["rechnung.einleitung"]
    assert doc.closing_text == "Danke!" and doc.payment_qr == "girocode"


def test_positionen_berechnung(client, db):
    _ready(client)
    doc = _draft(client, db)
    r = post(client, f"/rechnungen/{doc.id}", _form(doc))
    assert "Gespeichert" in r.text
    doc = _docs(db)[0]
    nets = [(l.title, l.net) for l in doc.lines]
    # 250 × 0,125 = 31,25; 3 × 38,50 − 10 % = 103,95; 1 × 20 = 20,00
    assert nets == [("Unterhaltsreinigung", D("31.25")), ("Fensterreinigung", D("103.95")), ("Fachbuch", D("20.00"))]
    # USt je Satz auf die Netto-Summe: 19 % auf 135,20 = 25,69; 7 % auf 20,00 = 1,40
    assert (doc.net_total, doc.vat_total, doc.gross_total) == (D("155.20"), D("27.09"), D("182.29"))
    assert doc.due_date == date.fromordinal(date.today().toordinal() + 14)
    page =client.get(f"/rechnungen/{doc.id}").text
    assert "zzgl. 19 % USt auf 135,20 €" in page and "182,29 €" in page


def test_katalog_entfernen_und_fehler(client, db):
    _ready(client)
    post(client, "/katalog/neu", {"kind": "leistung", "title": "Grundreinigung", "unit": "pauschal",
                                  "billing_type": "pauschal", "price_net": "450", "vat_rate": "19"})
    item = db.scalars(select(CatalogItem).execution_options(all_tenants=True)).one()
    doc = _draft(client, db)
    data = _form(doc, **{"pos-3-titel": "", "pos-3-katalog": str(item.id), "pos-1-entfernen": "1"})
    post(client, f"/rechnungen/{doc.id}", data)
    doc = _docs(db)[0]
    assert [l.title for l in doc.lines] == ["Unterhaltsreinigung", "Fachbuch", "Grundreinigung"]
    assert doc.lines[-1].unit == "pauschal" and doc.lines[-1].net == D("450.00")

    r = post(client, f"/rechnungen/{doc.id}", _form(doc, **{"pos-0-preis": "", "pos-2-rabatt": "120",
                                                            "doc_date": "32.13.2026", "recipient_name": ""}))
    assert r.status_code == 400
    for text in ("Einzelpreis", "Rabatt zwischen", "kein gültiges Datum", "Empfänger"):
        assert text in r.text
    assert 'value="Unterhaltsreinigung"' in r.text  # Eingaben bleiben erhalten


def test_pflichtangaben_vor_festschreiben(client, db):
    _ready(client)
    doc = _draft(client, db)
    r = post(client, f"/rechnungen/{doc.id}/festschreiben")
    assert "Leistungsdatum" in r.text and "mindestens eine Position" in r.text
    assert _docs(db)[0].number is None
    assert db.scalars(select(NumberRange).execution_options(all_tenants=True)).all() == []  # keine Nummer verbraucht


def test_festschreiben_vergibt_nummer_und_speichert_pdf(client, db):
    _ready(client)
    doc = _final(client, db)
    assert doc.number == f"{YEAR}-001" and doc.is_final
    assert doc.file is not None and len(doc.file.sha256) == 64
    r = client.get(f"/rechnungen/{doc.id}/pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    text = _pdf_text(r.content)
    for expected in (f"Rechnung Nr. {YEAR}-001", "Bäckerei Krume GmbH", "Unterhaltsreinigung", "182,29 €",
                     "zzgl. 19 % USt auf 135,20 €", "DE89 3704 0044 0532 0130 00", "Muster Service", "St.-Nr. 12/345/67890"):
        assert expected in text, expected
    with pdfplumber.open(io.BytesIO(r.content)) as pdf:  # kein Entwurfs-Aufdruck mehr
        assert not any(c["size"] > 60 for c in pdf.pages[0].chars)
    log =db.scalars(select(AuditLog).where(AuditLog.action == "festgeschrieben")).one()
    assert log.changes["nummer"] == f"{YEAR}-001" and log.changes["sha256"] == doc.file.sha256

    # Danach unveränderbar, nicht löschbar, PDF wird nicht neu erzeugt
    r = post(client, f"/rechnungen/{doc.id}", _form(doc, recipient_name="Anders"))
    assert "nicht mehr geändert" in r.text
    r = post(client, f"/rechnungen/{doc.id}/loeschen")
    assert "nicht gelöscht" in r.text
    stored = read_stored(doc.file)
    assert client.get(f"/rechnungen/{doc.id}/pdf").content == stored  # immer das gespeicherte Original


def test_vorschau_mit_entwurfs_aufdruck(client, db):
    _ready(client)
    doc = _draft(client, db)
    r = post(client, f"/rechnungen/{doc.id}", _form(doc, aktion="vorschau"))
    assert r.headers["content-type"] == "application/pdf"
    text = _pdf_text(r.content)
    assert "Rechnung (Entwurf)" in text and "wird vergeben" in text
    with pdfplumber.open(io.BytesIO(r.content)) as pdf:  # großer, schräger Aufdruck „ENTWURF“
        assert any(c["size"] > 60 for c in pdf.pages[0].chars)
    assert _pdf_text(client.get(f"/rechnungen/{doc.id}/vorschau").content).count("Unterhaltsreinigung") == 1


def test_nummernkreis_lueckenlos_je_jahr_und_startwert(client, db):
    _ready(client)
    first = _final(client, db)
    deleted = _draft(client, db)
    post(client, f"/rechnungen/{deleted.id}/loeschen")  # gelöschter Entwurf verbraucht keine Nummer
    second = _final(client, db)
    last_year = _final(client, db, doc_date=f"15.12.{YEAR - 1}", service_date=f"15.12.{YEAR - 1}")
    assert (first.number, second.number, last_year.number) == (f"{YEAR}-001", f"{YEAR}-002", f"{YEAR - 1}-001")

    nummern = {**VALID_SETTINGS["nummern"], "nummern.rechnung.format": "RE{JJ}-{NNNN}", "nummern.rechnung.start": "500"}
    post(client, "/einstellungen/nummern", nummern)
    third = _final(client, db)
    assert third.number == f"RE{YEAR % 100:02d}-0500"


def test_storno(client, outbox, db):
    _ready(client)
    doc = _final(client, db)
    r = post(client, f"/rechnungen/{doc.id}/stornieren", {"grund": ""})
    assert "Grund" in r.text
    r = post(client, f"/rechnungen/{doc.id}/stornieren", {"grund": "Falsche Menge"})
    assert f"Stornorechnung S-{YEAR}-001 erstellt" in r.text
    original, storno = _docs(db)
    assert original.status == "storniert" and original.cancel_reason == "Falsche Menge"
    assert storno.kind == "storno" and storno.storno_of_id == original.id and storno.is_final
    assert storno.gross_total == -original.gross_total and storno.status == "erledigt"
    text = _pdf_text(client.get(f"/rechnungen/{storno.id}/pdf").content)
    assert f"Stornorechnung Nr. S-{YEAR}-001" in text and f"Rechnung Nr. {YEAR}-001" in text and "-182,29 €" in text
    # Kein zweites Storno, Storno selbst nicht stornierbar
    assert "bereits storniert" in post(client, f"/rechnungen/{doc.id}/stornieren", {"grund": "x"}).text
    assert "Nur festgeschriebene Rechnungen" in post(client, f"/rechnungen/{storno.id}/stornieren", {"grund": "x"}).text
    # Korrektur: kopieren -> neuer Entwurf ohne Nummer
    r = post(client, f"/rechnungen/{doc.id}/duplizieren")
    copy = _docs(db)[-1]
    assert copy.is_draft and copy.number is None and copy.service_date is None and len(copy.lines) == 3


def test_kleinunternehmer(client, db):
    _ready(client)
    steuer = {**VALID_SETTINGS["steuer"], "steuer.kleinunternehmer": "1"}
    post(client, "/einstellungen/steuer", steuer)
    doc = _final(client, db)
    assert all(l.vat_rate == 0 for l in doc.lines) and doc.vat_total == 0 and doc.gross_total == D("155.20")
    text = _pdf_text(client.get(f"/rechnungen/{doc.id}/pdf").content)
    assert "§ 19 UStG" in text and "USt auf" not in text


def test_hinweise_13b_und_35a(client, db):
    _ready(client)
    doc = _draft(client, db, _customer(client, db, ust_id="DE123456789"))  # § 13b braucht die USt-IdNr.
    r = post(client, f"/rechnungen/{doc.id}", _form(doc, hint=["13b", "35a"]))
    assert "Gespeichert" in r.text
    r = post(client, f"/rechnungen/{doc.id}/festschreiben")
    assert "Lohnanteil" in r.text
    doc = _final(client, db, doc, hint=["13b", "35a", "14b"], labor_amount="100,00")
    assert doc.vat_total == 0
    text = _pdf_text(client.get(f"/rechnungen/{doc.id}/pdf").content)
    assert "§ 13b UStG" in text and "100,00 €" in text and "§ 35a" in text and "zwei Jahre" in text


def test_zahlungseingang(client, db):
    _ready(client)
    doc = _draft(client, db)
    post(client, f"/rechnungen/{doc.id}", _form(doc))
    r = post(client, f"/rechnungen/{doc.id}/zahlung", {"paid_on": TODAY})
    assert r.status_code == 400 and "erst nach dem Festschreiben" in r.text
    doc = _final(client, db, doc)
    post(client, f"/rechnungen/{doc.id}/zahlung", {"paid_on": TODAY, "amount": "100,00"})
    assert _docs(db)[0].open_amount == D("82.29")
    post(client, f"/rechnungen/{doc.id}/zahlung", {"paid_on": TODAY})
    assert _docs(db)[0].status == "bezahlt"
    assert "Bäckerei" in client.get("/rechnungen?status=bezahlt").text


def test_ueberfaellig_filter_und_kundenseite(client, db):
    _ready(client)
    customer = _customer(client, db)
    old = _final(client, db, _draft(client, db, customer), doc_date="01.01." + str(YEAR), service_date="01.01." + str(YEAR))
    page = client.get("/rechnungen?ueberfaellig=1").text
    assert old.number in page
    page = client.get(f"/kunden/{customer.id}").text
    assert "155,20 €" in page and old.number in page and "Neue Rechnung" in page


def test_ablage_ordner_kopie(client, db, tmp_path):
    _ready(client)
    design = {**VALID_SETTINGS["design"], "rechnung.ablage_ordner": str(tmp_path)}
    r = post(client, "/einstellungen/design", design)
    assert "gespeichert" in r.text
    doc = _final(client, db)
    months = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
              "November", "Dezember")
    folder = tmp_path / str(YEAR) / f"{date.today().month:02d} {months[date.today().month - 1]}" / "Bäckerei Krume GmbH"
    files = list(folder.glob("*.pdf"))
    assert len(files) == 1 and files[0].name.startswith(f"{YEAR}-001-Rechnung")
    assert files[0].read_bytes() == read_stored(doc.file)


def test_zip_und_drucken(client, db):
    _ready(client)
    a, b = _final(client, db), _final(client, db)
    draft = _draft(client, db)
    r = post(client, "/rechnungen/auswahl", {"ids": [str(a.id), str(b.id), str(draft.id)], "aktion": "zip"})
    assert len(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == 2
    r = post(client, "/rechnungen/auswahl", {"ids": [str(a.id)], "aktion": "drucken"})
    assert r.headers["content-type"] == "application/pdf"


def test_girocode_inhalt():
    payload = girocode_payload(name="Erika Muster", iban="DE89 3704 0044 0532 0130 00", bic="COBADEFFXXX",
                               amount=D("182.29"), reference="Rechnung 2026-001")
    assert payload.split("\n") == ["BCD", "002", "1", "SCT", "COBADEFFXXX", "Erika Muster",
                                   "DE89370400440532013000", "EUR182.29", "", "", "Rechnung 2026-001"]


# -------------------------------------------------------------------- Rechte
def test_rechte(client, outbox, db):
    _ready(client)
    doc = _draft(client, db)
    ma = _invite(client, outbox, "max@example.org", "mitarbeiter")
    r = post(ma, f"/rechnungen/{doc.id}", _form(doc))
    assert "Gespeichert" in r.text  # Mitarbeiter darf Entwürfe bearbeiten …
    assert post(ma, f"/rechnungen/{doc.id}/festschreiben").status_code == 403  # … aber nicht festschreiben
    assert "Sonderrecht" in ma.get(f"/rechnungen/{doc.id}").text
    stb = _invite(client, outbox, "stb@example.org", "steuerberater")
    assert stb.get("/rechnungen").status_code == 200
    assert post(stb, "/rechnungen/neu", {"customer_id": "1"}).status_code == 403
    assert post(stb, f"/rechnungen/{doc.id}", _form(doc)).status_code == 403
    # Ohne Rechnungsrecht keine Umsätze auf der Kundenseite
    user = db.scalar(select(User).where(User.email == "max@example.org"))
    m = db.scalar(select(Membership).where(Membership.user_id == user.id))
    perms = {**m.permissions, "rechnungen_ausgang": "keine"}
    post(client, f"/benutzer/{m.id}", {"role": "mitarbeiter", "is_active": "1", "aktion": "speichern",
                                       **{f"perm_{k}": v for k, v in perms.items()}})
    assert "Umsatz" not in ma.get(f"/kunden/{doc.customer_id}").text


# ---------------------------------------------------------- Mandantentrennung
def test_rechnungen_anderer_firma_unerreichbar(client, outbox, db):
    client_b, tenant_a, tenant_b = _two_tenants(client, outbox, db)
    doc = _final(client, db)
    draft = _draft(client, db)
    for url in (f"/rechnungen/{doc.id}", f"/rechnungen/{doc.id}/pdf", f"/rechnungen/{draft.id}/vorschau"):
        assert client_b.get(url).status_code == 404, url
    for url, data in ((f"/rechnungen/{draft.id}", _form(draft)), (f"/rechnungen/{draft.id}/festschreiben", {}),
                      (f"/rechnungen/{doc.id}/stornieren", {"grund": "x"}), (f"/rechnungen/{doc.id}/zahlung", {}),
                      (f"/rechnungen/{doc.id}/duplizieren", {}), (f"/rechnungen/{draft.id}/loeschen", {})):
        assert post(client_b, url, data).status_code == 404, url
    a_customer = draft.customer_id
    assert "Bitte einen Kunden" in post(client_b, "/rechnungen/neu", {"customer_id": str(a_customer)}).text
    r = post(client_b, "/rechnungen/auswahl", {"ids": [str(doc.id)], "aktion": "zip"})
    assert "festgeschriebene Rechnungen auswählen" in r.text
    assert "Bäckerei" not in client_b.get("/rechnungen").text

    # Nummernkreis je Firma: B beginnt ebenfalls bei 001
    b_doc = _final(client_b, db, _draft(client_b, db, _customer(client_b, db, name="Kunde B")))
    assert b_doc.number == f"{YEAR}-001" and b_doc.tenant_id == tenant_b.id
