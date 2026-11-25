"""Alte Ausgangsrechnungen hochladen (z. B. aus einem früheren Programm).

Entschieden 02.11.2026: Archiv + Auswertungen, Nummernkreis zieht nach, Auslesen + Prüfen,
„bezahlt am“ beim Übernehmen.

Ablauf: PDF hochladen → eigener Prüfbereich (``pending``, ``source = "import"``) → Angaben prüfen →
„Übernehmen“ = festgeschrieben. Das hochgeladene Original wird unverändert als Rechnungs-PDF behalten und nie
neu erzeugt. Ab dem Übernehmen zählt die Rechnung wie eine selbst geschriebene (EÜR, Umsatzsteuer, offene
Posten, Mahnwesen, DATEV). Je Steuersatz entsteht eine Pauschal-Position („Leistungen lt. Rechnung …“).
"""
import re
from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import utcnow
from app.formatting import format_date, format_percent, parse_date
from app.models import Customer, Document, StoredFile
from app.models.belege import DIRECTION_OUT, KIND_INVOICE, STATE_DRAFT, STATE_FINAL, STATUS_OPEN
from app.services import ablage, audit, auslese, belege, einstellungen, nummernkreise, rechnungen

SOURCE_IMPORT = "import"
ALLOWED = {"pdf"}  # Rechnungs-PDFs (auch gescannt); werden unter /rechnungen/{id}/pdf angezeigt


# ---------------------------------------------------------------- Listen
def pending(db: Session) -> list[Document]:
    stmt = (select(Document).where(Document.pending.is_(True), Document.direction == DIRECTION_OUT)
            .order_by(Document.created_at.desc()).execution_options(include_pending=True))
    return list(db.scalars(stmt))


def pending_count(db: Session) -> int:
    stmt = (select(func.count()).select_from(Document)
            .where(Document.pending.is_(True), Document.direction == DIRECTION_OUT)
            .execution_options(include_pending=True))
    return db.scalar(stmt) or 0


def get_pending(db: Session, doc_id: int) -> Document | None:
    doc = belege.get(db, doc_id)
    if doc is None or doc.direction != DIRECTION_OUT or not doc.pending:
        return None
    return doc


def customers(db: Session) -> list[Customer]:
    return list(db.scalars(select(Customer).where(Customer.is_active.is_(True)).order_by(Customer.name)))


def _match_customer(db: Session, name: str | None) -> Customer | None:
    folded = (name or "").casefold().strip()
    if len(folded) < 3:
        return None
    for customer in customers(db):
        if customer.name.casefold() == folded or customer.name.casefold() in folded:
            return customer
    return None


def address_after(text: str | None, name: str | None) -> str | None:
    """Anschrift aus dem Rechnungstext: die Zeilen unter dem Kundennamen bis einschließlich der PLZ-Zeile."""
    if not text or not name:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    folded = name.casefold()
    for i, line in enumerate(lines):
        if folded not in line.casefold():
            continue
        found = []
        for follow in lines[i + 1:i + 5]:
            found.append(follow[:200])
            if re.match(r"^(?:D-)?\d{5}\s+\S", follow):
                return "\n".join(found)
        return None
    return None


# ---------------------------------------------------------------- Upload
def create_from_bytes(db: Session, data: bytes, filename: str, *, tenant_id: int, user_id: int | None,
                      ip: str | None) -> Document:
    """Datei prüfen, ablegen, auslesen und in den Prüfbereich der Ausgangsrechnungen legen.
    Wirft ablage.UploadRejected bzw. belege.DuplicateUpload."""
    stored = ablage.save_checked(db, data, filename, tenant_id=tenant_id, kind="rechnung_import",
                                 allowed=ALLOWED, user_id=user_id)
    earlier = db.scalars(select(StoredFile).where(StoredFile.sha256 == stored.sha256, StoredFile.id != stored.id))
    for other in earlier:
        doc = db.scalar(select(Document).where(Document.file_id == other.id, Document.direction == DIRECTION_OUT)
                        .execution_options(include_pending=True))
        if doc is not None:
            ablage.absolute_path(stored).unlink(missing_ok=True)
            db.delete(stored)
            db.flush()
            raise belege.DuplicateUpload(doc)

    partners = [auslese.Partner(id=c.id, name=c.name, ust_id=c.ust_id) for c in customers(db)]
    result, einvoice = belege.extract(db, stored, data, partners, belege.own_company(db))
    uncertain = set(result.uncertain)
    customer = None
    if einvoice is not None:
        # In der eigenen E-Rechnung ist der Käufer der Kunde (nicht der Verkäufer)
        result.partner_name = einvoice.buyer_name or result.partner_name
        customer = _match_customer(db, result.partner_name)
        uncertain.discard("partner")
        if not result.partner_name:
            uncertain.add("partner")
    elif result.supplier_id:  # Treffer in der Kundenliste (über die Kunden als „Partner“ gesucht)
        customer = db.get(Customer, result.supplier_id)
    else:
        customer = _match_customer(db, result.partner_name)
    if customer is None:
        uncertain.add("customer")

    if customer:
        name, address = rechnungen.recipient_of(customer)
    else:
        name, address = result.partner_name, address_after(result.text, result.partner_name)
    number = result.number
    if number and _number_taken(db, number):
        result.hints.insert(0, f"Die erkannte Rechnungsnummer {number} gibt es schon – bitte prüfen.")
        uncertain.add("number")
        number = None
    doc = Document(
        tenant_id=tenant_id, direction=DIRECTION_OUT, kind=KIND_INVOICE, pending=True, source=SOURCE_IMPORT,
        state=STATE_DRAFT, status=STATUS_OPEN, number=number, doc_date=result.doc_date,
        service_date=result.doc_date, due_date=result.due_date, customer_id=customer.id if customer else None,
        partner_name=name, recipient_name=name, recipient_address=address, file_id=stored.id,
        review_fields=sorted(uncertain), needs_review=bool(uncertain), extracted_text=result.text or None,
        notes="\n".join(result.hints) or None, created_by_id=user_id, payment_qr="keiner",
    )
    belege._set_lines(doc, result.lines)
    if not result.lines and result.gross is not None:
        doc.gross_total = result.gross
    db.add(doc)
    db.flush()
    belege._log(db, doc, "rechnung_hochgeladen", user_id, ip,
                {"datei": stored.original_name, "unsicher": sorted(uncertain)})
    return doc


# ---------------------------------------------------------------- Prüfen
def save_form(db: Session, doc: Document, form: Mapping[str, str], *, user_id: int, ip: str | None) -> dict:
    """Angaben speichern (bleibt im Prüfbereich). Gibt Fehler je Feld zurück."""
    errors: dict[str, str] = {}
    values: dict = {}
    for key, limit in (("number", 60), ("recipient_name", 200), ("recipient_address", 1000), ("notes", 4000)):
        value = (form.get(key) or "").strip()
        if len(value) > limit:
            errors[key] = f"Höchstens {limit} Zeichen."
        values[key] = value or None
    for key in ("doc_date", "service_date", "due_date"):
        try:
            values[key] = parse_date(form.get(key))
        except ValueError as exc:
            errors[key] = str(exc)
    customer_raw = (form.get("customer_id") or "").strip()
    values["customer_id"] = None
    if customer_raw:
        customer = db.get(Customer, int(customer_raw)) if customer_raw.isdigit() else None
        if customer is None:
            errors["customer_id"] = "Unbekannter Kunde."
        else:
            values["customer_id"] = customer.id
            name, address = rechnungen.recipient_of(customer)
            values["recipient_name"] = values["recipient_name"] or name
            values["recipient_address"] = values["recipient_address"] or address or None
    lines = belege._parse_lines(form, errors)
    if values["number"] and _number_taken(db, values["number"], doc.id):
        errors["number"] = f"Die Rechnungsnummer {values['number']} gibt es schon."
    if errors:
        return errors
    old = belege._snapshot(doc)
    for key, value in values.items():
        setattr(doc, key, value)
    doc.partner_name = doc.recipient_name
    doc.service_date = doc.service_date or doc.doc_date
    belege._set_lines(doc, lines)
    doc.review_fields = []
    changes = audit.diff(old, belege._snapshot(doc))
    if changes:
        belege._log(db, doc, "geaendert", user_id, ip, changes)
    return {}


def _number_taken(db: Session, number: str, own_id: int | None = None) -> bool:
    """Eigene Rechnungsnummern sind je Firma eindeutig (auch im Prüfbereich, siehe uq_documents_own_number)."""
    stmt = (select(func.count()).select_from(Document)
            .where(Document.direction == DIRECTION_OUT, Document.number == number)
            .execution_options(include_pending=True))
    if own_id is not None:
        stmt = stmt.where(Document.id != own_id)
    return bool(db.scalar(stmt))


def take_over(db: Session, doc: Document, form: Mapping[str, str], *, user_id: int, ip: str | None) -> dict:
    """Speichern, prüfen und festschreiben; optional als bezahlt buchen. Gibt Fehler je Feld zurück
    (dann muss der Aufrufer zurückrollen)."""
    errors = save_form(db, doc, form, user_id=user_id, ip=ip)
    paid_on = None
    try:
        paid_on = parse_date(form.get("paid_on"))
    except ValueError as exc:
        errors["paid_on"] = str(exc)
    if errors:
        return errors
    required = {"number": "Rechnungsnummer", "doc_date": "Rechnungsdatum", "recipient_name": "Kunde (Name)",
                "recipient_address": "Anschrift des Kunden"}
    for key, label in required.items():
        if not getattr(doc, key):
            errors[key] = f"{label} fehlt."
    if not doc.lines:
        errors["zeile-0"] = "Bitte die Beträge je Steuersatz eintragen."
    if paid_on and doc.doc_date and paid_on < doc.doc_date:
        errors["paid_on"] = "Das Zahlungsdatum liegt vor dem Rechnungsdatum."
    if paid_on and paid_on > date.today():
        errors["paid_on"] = "Das Zahlungsdatum liegt in der Zukunft."
    if errors:
        return errors

    for line in doc.lines:  # Pauschal-Position je Steuersatz (Menge 1, damit Storno/DATEV wie gewohnt laufen)
        line.title = f"Leistungen lt. Rechnung {doc.number} ({format_percent(line.vat_rate)} USt)"
        line.quantity, line.unit, line.unit_price = Decimal(1), "pauschal", line.net
    doc.pending = False
    doc.needs_review = False
    doc.state = STATE_FINAL
    doc.reviewed_at = doc.finalized_at = utcnow()
    doc.reviewed_by_id = doc.finalized_by_id = user_id
    doc.e_invoice_format = None
    if doc.due_date is None:  # für offene Posten und Mahnwesen
        doc.due_date = doc.doc_date + timedelta(days=int(einstellungen.get(db, "design.zahlungsziel_tage") or 14))
    counter = nummernkreise.register_existing(db, "rechnung", doc.number, doc.doc_date)
    db.flush()
    belege._log(db, doc, "importiert_festgeschrieben", user_id, ip, {
        "nummer": doc.number, "sha256": doc.file.sha256 if doc.file else None, "brutto": str(doc.gross_total),
        "nummernkreis": counter if counter is not None else "Nummer passt nicht zum eingestellten Format"})
    if paid_on and doc.gross_total and doc.gross_total > 0:
        payment_errors = belege.add_payment(db, doc, {"paid_on": format_date(paid_on), "amount": ""},
                                            user_id=user_id, ip=ip)
        if payment_errors:
            return {"paid_on": "; ".join(payment_errors.values())}
    return {}
