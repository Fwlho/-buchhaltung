"""Eingangsrechnungen: Upload -> Prüfbereich -> Übernehmen -> Festschreiben -> Zahlungen.

Regeln (siehe KONZEPT.md 6.2/6.3/6.6):
- Jeder Upload landet zuerst im Prüfbereich (pending = true) und zählt nirgends mit.
- Entwürfe sind änderbar und löschbar; festgeschriebene Belege nicht mehr (nur Zahlungen/Storno).
- Spätestens bei der ersten Zahlung wird automatisch festgeschrieben.
- Skonto wird erst beim Bezahlen abgezogen.
Alle Funktionen schreiben ins Änderungsprotokoll und rufen KEIN commit auf.
"""
import io
import re
import zipfile
from collections.abc import Mapping
from datetime import date
from decimal import Decimal

from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import utcnow
from app.formatting import format_date, format_money, format_percent, parse_date, parse_decimal, round_money
from app.models import Document, DocumentLine, Payment, StoredFile, Supplier
from app.models.belege import (
    DIRECTION_IN,
    KIND_INVOICE,
    STATE_DRAFT,
    STATE_FINAL,
    STATUS_CANCELLED,
    STATUS_OPEN,
    STATUS_PAID,
)
from app.services import ablage, audit, auslese, einstellungen, kontierung, ocr, steuerschuld
from app.services.betraege import AmountError, TaxLine, complete_line, skonto_amount, totals
from app.services.konten import account_valid

ALLOWED_UPLOADS = {"pdf", "png", "jpg", "xml"}
LINE_SLOTS = 2          # leere Zusatzzeilen im Formular
MAX_LINES = 10
VAT_RATES = ("19", "7", "0", "16", "5")


class DuplicateUpload(ValueError):
    def __init__(self, document: Document | None):
        super().__init__("Diese Datei wurde bereits hochgeladen.")
        self.document = document


# ---------------------------------------------------------------- Laden
def get(db: Session, doc_id: int) -> Document | None:
    """Beleg laden – auch aus dem Prüfbereich (die Route prüft, welcher Bereich passt)."""
    return db.get(Document, doc_id, execution_options={"include_pending": True})


def pending_documents(db: Session) -> list[Document]:
    """Prüfbereich der Eingangsrechnungen (hochgeladene alte Ausgangsrechnungen haben einen eigenen)."""
    stmt = (select(Document).where(Document.pending.is_(True), Document.direction == DIRECTION_IN)
            .order_by(Document.created_at.desc()).execution_options(include_pending=True))
    return list(db.scalars(stmt))


def pending_count(db: Session) -> int:
    stmt = (select(func.count()).select_from(Document)
            .where(Document.pending.is_(True), Document.direction == DIRECTION_IN)
            .execution_options(include_pending=True))
    return db.scalar(stmt) or 0


def incoming(db: Session, *, q: str = "", status: str = "", year: int | None = None, review: bool = False,
             limit: int = 500) -> list[Document]:
    """Geprüfte Eingangsrechnungen (Prüfbereich wird automatisch ausgefiltert)."""
    stmt = select(Document).where(Document.direction == DIRECTION_IN)
    if status in (STATUS_OPEN, STATUS_PAID, STATUS_CANCELLED):
        stmt = stmt.where(Document.status == status)
    if year:
        stmt = stmt.where(Document.doc_date >= date(year, 1, 1), Document.doc_date <= date(year, 12, 31))
    if review:
        stmt = stmt.where(Document.needs_review.is_(True))
    if q.strip():
        pattern = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(Document.partner_name).like(pattern) | func.lower(Document.number).like(pattern))
    stmt = stmt.order_by(Document.doc_date.desc().nulls_last(), Document.id.desc()).limit(limit)
    return list(db.scalars(stmt))


def years(db: Session) -> list[int]:
    dates = db.scalars(select(Document.doc_date).where(Document.direction == DIRECTION_IN,
                                                       Document.doc_date.is_not(None)))
    return sorted({d.year for d in dates}, reverse=True)


# ------------------------------------------------- doppelte Rechnungen
DUPLICATE_MESSAGE = "Doppelte Werte erkannt, bitte prüfen!"


def _number_key(number: str | None) -> str:
    """Rechnungsnummer ohne Leer- und Trennzeichen, z. B. „RE-2026 / 815“ = „re2026815“."""
    return re.sub(r"[\s\-_/.#]", "", (number or "").casefold())


def _same_partner(a: Document, b: Document) -> bool:
    if a.supplier_id and b.supplier_id:
        return a.supplier_id == b.supplier_id
    name_a, name_b = (a.partner_name or "").casefold().strip(), (b.partner_name or "").casefold().strip()
    return bool(name_a) and name_a == name_b


def duplicate_map(db: Session) -> dict[int, list[Document]]:
    """Eingangsrechnungen (auch im Prüfbereich), die wahrscheinlich doppelt erfasst sind: je Beleg-ID die anderen.

    Doppelt = gleicher Lieferant und gleiche Rechnungsnummer – oder (falls die Nummer fehlt bzw. falsch gelesen
    wurde) gleicher Lieferant, gleiches Rechnungsdatum und gleicher Bruttobetrag. Stornierte Belege und bewusst
    angelegte Kopien („Duplizieren“) zählen nicht.
    """
    stmt = (select(Document).where(Document.direction == DIRECTION_IN, Document.status != STATUS_CANCELLED,
                                   Document.source != "duplikat").execution_options(include_pending=True))
    docs = list(db.scalars(stmt))
    by_number: dict[str, list[Document]] = {}
    by_date_amount: dict[tuple, list[Document]] = {}
    for doc in docs:
        key = _number_key(doc.number)
        if len(key) >= 3:
            by_number.setdefault(key, []).append(doc)
        if doc.doc_date and doc.gross_total:
            by_date_amount.setdefault((doc.doc_date, doc.gross_total), []).append(doc)
    found: dict[int, dict[int, Document]] = {}
    for group in (*by_number.values(), *by_date_amount.values()):
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if _same_partner(a, b):
                    found.setdefault(a.id, {})[b.id] = b
                    found.setdefault(b.id, {})[a.id] = a
    return {doc_id: sorted(others.values(), key=lambda d: d.id) for doc_id, others in found.items()}


def duplicates_of(db: Session, doc: Document) -> list[Document]:
    if doc.direction != DIRECTION_IN:
        return []
    return duplicate_map(db).get(doc.id, [])


# ------------------------------------------------------------- Hilfen
def _snapshot(doc: Document) -> dict:
    def s(v):
        if isinstance(v, Decimal):
            return str(v)
        if isinstance(v, date):
            return format_date(v)
        return v

    keys = ["number", "doc_date", "due_date", "partner_name", "account", "notes", "net_total", "vat_total",
            "gross_total", "state", "status"]
    if doc.direction == DIRECTION_IN:
        keys += ["supplier_id", "skonto_percent", "skonto_until", "reverse_charge", "reverse_charge_rate"]
    else:
        keys += ["recipient_name", "recipient_address", "object_address", "service_date", "service_until",
                 "payment_terms_days", "intro_text", "closing_text", "hints", "labor_amount", "payment_qr",
                 "reverse_charge"]
    data = {k: s(getattr(doc, k)) for k in keys}
    data["zeilen"] = [
        (f"{line.title}: {s(line.quantity)} {line.unit or ''} × {s(line.unit_price)} " if line.unit_price is not None
         else "") + f"{s(line.vat_rate)} %: {s(line.net)} + {s(line.vat)} = {s(line.gross)}"
        for line in doc.lines]
    return data


def audit_entity(doc: Document) -> str:
    return "eingangsrechnung" if doc.direction == DIRECTION_IN else "ausgangsrechnung"


def _log(db: Session, doc: Document, action: str, user_id: int | None, ip: str | None, changes=None) -> None:
    audit.log(db, action=action, entity=audit_entity(doc), entity_id=doc.id, tenant_id=doc.tenant_id,
              user_id=user_id, changes=changes, ip=ip)


def _set_lines(doc: Document, tax_lines: list[TaxLine]) -> None:
    doc.lines.clear()
    for i, t in enumerate(tax_lines, start=1):
        doc.lines.append(DocumentLine(
            tenant_id=doc.tenant_id, position=i, title=f"Netto {format_percent(t.rate)}",
            vat_rate=t.rate, net=t.net, vat=t.vat, gross=t.gross))
    if tax_lines:
        doc.net_total, doc.vat_total, doc.gross_total = totals(tax_lines)
    else:
        doc.net_total = doc.vat_total = doc.gross_total = None


def tax_lines_of(doc: Document) -> list[TaxLine]:
    return [TaxLine(line.vat_rate, line.net, line.vat, line.gross) for line in doc.lines]


def suggest_account(db: Session, supplier_id: int | None, partner_name: str | None) -> str | None:
    """Konto der letzten geprüften Rechnung desselben Lieferanten."""
    stmt = select(Document.account).where(Document.direction == DIRECTION_IN, Document.account.is_not(None))
    if supplier_id:
        stmt = stmt.where(Document.supplier_id == supplier_id)
    elif partner_name:
        stmt = stmt.where(func.lower(Document.partner_name) == partner_name.lower())
    else:
        return None
    return db.scalar(stmt.order_by(Document.doc_date.desc().nulls_last(), Document.id.desc()).limit(1))


def own_company(db: Session) -> auslese.OwnCompany:
    values = einstellungen.load(db)
    return auslese.OwnCompany(name=values.get("firma.name") or "", ust_id=values.get("steuer.ust_id"),
                              iban=values.get("bank.iban"))


def known_partners(db: Session) -> list[auslese.Partner]:
    rows = db.scalars(select(Supplier).where(Supplier.is_active.is_(True)))
    return [auslese.Partner(id=s.id, name=s.name, ust_id=s.ust_id, iban=s.iban) for s in rows]


# --------------------------------------------------------------- Upload
def _ocr_result(db: Session, recognize, label: str) -> auslese.Result:
    """Texterkennung für Scans/Fotos. Ohne Tesseract oder bei Fehlern: leeres Ergebnis zur Handeingabe."""
    if not ocr.available():
        result = auslese.parse("")
        result.hints = [f"{label}: {ocr.status_text()}"]
        return result
    try:
        text = recognize()
    except ocr.OcrError as exc:
        result = auslese.parse("")
        result.hints = [f"{label}: {exc} Bitte die Angaben von Hand eintragen."]
        return result
    result = auslese.parse(text, known_partners(db), own_company(db))
    if text.strip():
        # Ziffern werden bei Scans leicht verlesen (z. B. 8 statt 3) – Beträge daher immer prüfen lassen
        result.uncertain.add("lines")
        result.hints.insert(0, f"{label}: per Texterkennung (OCR) gelesen – bitte alle Werte sorgfältig prüfen.")
    return result


def extract(db: Session, stored: StoredFile, data: bytes, partners: list[auslese.Partner],
            own: auslese.OwnCompany) -> tuple[auslese.Result, object | None]:
    """Angaben aus einer Belegdatei lesen – Reihenfolge laut Konzept: E-Rechnungs-XML (exakt) ->
    PDF-Textebene -> OCR (Scans, Fotos). Gibt (Ergebnis, E-Rechnung oder None) zurück."""
    from app.services import erechnung

    einvoice = None
    if stored.content_type in ("application/pdf", "application/xml"):
        xml = data if stored.content_type == "application/xml" else erechnung.xml_from_pdf(data)
        if xml is not None:
            try:
                einvoice = erechnung.parse(xml, partners, own)
            except erechnung.EInvoiceError as exc:
                if stored.content_type == "application/xml":
                    result = auslese.parse("")
                    result.hints = [f"{exc} Bitte die Angaben von Hand eintragen."]
    if einvoice is not None:
        result = einvoice.result
        label = "ZUGFeRD/Factur-X" if stored.content_type == "application/pdf" else f"XRechnung ({einvoice.format})"
        result.hints.insert(0, f"E-Rechnung ({label}): Werte exakt aus den XML-Daten übernommen.")
    elif stored.content_type == "application/pdf":
        text = auslese.pdf_text(data)
        if text.strip():
            result = auslese.parse(text, partners, own)
        else:
            result = _ocr_result(db, lambda: ocr.pdf_text(data), "Gescanntes PDF")
    elif stored.content_type == "application/xml":
        pass  # Fehlermeldung oben gesetzt
    else:
        result = _ocr_result(db, lambda: ocr.image_text(data), "Foto/Bild")

    return result, einvoice


async def create_from_upload(db: Session, upload: UploadFile, *, tenant_id: int, user_id: int,
                             ip: str | None) -> Document:
    """Hochgeladene Datei speichern, auslesen und als neuen Beleg in den Prüfbereich legen."""
    data = await upload.read(ablage.MAX_UPLOAD_BYTES + 1)
    return create_from_bytes(db, data, upload.filename or "", tenant_id=tenant_id, user_id=user_id, ip=ip)


def create_from_bytes(db: Session, data: bytes, filename: str, *, tenant_id: int, user_id: int | None,
                      ip: str | None, source: str = "upload", allowed: set[str] = ALLOWED_UPLOADS,
                      mail: dict | None = None) -> Document:
    """Gemeinsamer Weg für Uploads und E-Mail-Anhänge. ``mail``: Absender/Betreff/Datum als
    Ersatzwerte, falls die Auslese nichts findet. Wirft UploadRejected bzw. DuplicateUpload."""
    stored = ablage.save_checked(db, data, filename, tenant_id=tenant_id, kind="beleg", allowed=allowed,
                                 user_id=user_id)
    earlier = db.scalars(select(StoredFile).where(StoredFile.sha256 == stored.sha256, StoredFile.id != stored.id))
    for other in earlier:
        # Nur unter Eingangsbelegen suchen (das PDF einer eigenen Ausgangsrechnung ist kein Duplikat)
        doc = db.scalar(select(Document).where(Document.file_id == other.id, Document.direction == DIRECTION_IN)
                        .execution_options(include_pending=True))
        if doc is not None:
            ablage.absolute_path(stored).unlink(missing_ok=True)
            db.delete(stored)  # Datei-Eintrag wieder entfernen – sonst bliebe er beim nächsten commit stehen
            db.flush()
            raise DuplicateUpload(doc)

    result, einvoice = extract(db, stored, data, known_partners(db), own_company(db))

    notes = list(result.hints)
    if mail:
        notes.insert(0, f"Per E-Mail von {mail.get('sender') or 'unbekannt'}: „{mail.get('subject') or ''}“")
        if not result.partner_name and mail.get("sender_name"):
            result.partner_name = mail["sender_name"][:200]
            result.uncertain.add("partner")
        if not result.doc_date and mail.get("date"):
            result.doc_date = mail["date"]
            result.uncertain.add("doc_date")

    doc = Document(
        kind="gutschrift" if einvoice is not None and einvoice.credit_note else "rechnung",
        tenant_id=tenant_id, direction=DIRECTION_IN, pending=True, source=source, state=STATE_DRAFT,
        status=STATUS_OPEN, number=result.number, doc_date=result.doc_date, due_date=result.due_date,
        partner_name=result.partner_name, supplier_id=result.supplier_id,
        skonto_percent=result.skonto_percent, skonto_until=result.skonto_until,
        review_fields=sorted(result.uncertain), needs_review=bool(result.uncertain),
        extracted_text=(result.text or None), file_id=stored.id, created_by_id=user_id,
        notes="\n".join(notes) or None,
        e_invoice_format=einvoice.format if einvoice is not None else None,  # exakt – keine KI-Auslese nötig
    )
    _set_lines(doc, result.lines)
    _preselect_reverse_charge(db, doc, result, einvoice)
    # Konto: Lieferant oder aus früheren Buchungen gelernt (ohne Internet); die KI fragt ki_nachtragen danach
    suggestion = kontierung.suggest_local(db, doc)
    if suggestion:
        kontierung.apply(db, doc, suggestion, user_id=user_id, ip=ip)
    db.add(doc)
    if not result.lines and result.gross is not None:
        doc.gross_total = result.gross
    db.flush()
    _log(db, doc, "per_email_importiert" if source == "email" else "hochgeladen", user_id, ip,
         {"datei": stored.original_name, "unsicher": sorted(doc.review_fields or []),
          **({"konto": doc.account, "konto_herkunft": doc.account_source} if doc.account else {})})
    return doc


def _preselect_reverse_charge(db: Session, doc: Document, result: auslese.Result, einvoice) -> None:
    """§ 13b vorauswählen: Lieferant mit gesetztem Fall (Stammdaten) oder § 13b-Hinweis auf einem Beleg ohne
    ausgewiesene Umsatzsteuer. Erkannt ist nur geschätzt → gelb markiert."""
    supplier = db.get(Supplier, doc.supplier_id) if doc.supplier_id else None
    no_vat = all(line.vat == 0 for line in doc.lines)
    detected = no_vat and (any(h.startswith("Reverse-Charge") for h in result.hints)
                           or bool(auslese._REVERSE_CHARGE.search(result.text or "")))
    if supplier is not None and supplier.reverse_charge in steuerschuld.KINDS_IN and no_vat:
        doc.reverse_charge = supplier.reverse_charge
    elif detected:
        doc.reverse_charge = steuerschuld.guess_incoming(supplier, result.text, own_company(db).ust_id)
        doc.review_fields = sorted(set(doc.review_fields or []) | {"reverse_charge"})
        doc.needs_review = True
    if doc.reverse_charge:
        doc.reverse_charge_rate = steuerschuld.DEFAULT_RATE


# --------------------------------------------------------------- Formular
def line_rows(doc: Document) -> list[dict]:
    """Zeilen für das Formular: vorhandene + LINE_SLOTS leere."""
    default = "0" if doc.reverse_charge else "19"   # § 13b: die Rechnung weist keine Umsatzsteuer aus
    rows = [{"rate": f"{line.vat_rate.normalize():f}", "net": line.net, "vat": line.vat, "gross": line.gross}
            for line in doc.lines]
    if not rows and doc.gross_total is not None:
        rows.append({"rate": default, "net": None, "vat": None, "gross": doc.gross_total})
    for _ in range(LINE_SLOTS):
        rows.append({"rate": default, "net": None, "vat": None, "gross": None})
    return rows[:MAX_LINES]


def _parse_lines(form: Mapping[str, str], errors: dict, zero: bool = False) -> list[TaxLine]:
    result: list[TaxLine] = []
    for i in range(MAX_LINES):
        prefix = f"zeile-{i}-"
        raw = {k: (form.get(prefix + k) or "").strip() for k in ("satz", "netto", "ust", "brutto")}
        if not (raw["netto"] or raw["ust"] or raw["brutto"]):
            continue
        try:
            rate = Decimal(0) if zero else parse_decimal(raw["satz"] or "19")
            values = [parse_decimal(raw[k]) for k in ("netto", "ust", "brutto")]
            values = [round_money(v) if v is not None else None for v in values]
            if zero and values[1]:
                raise AmountError("§ 13b: Die Rechnung darf keine Umsatzsteuer ausweisen – bitte nur Netto "
                                  "eintragen (die Steuer berechnet das Programm).")
            if zero and values[0] is None and values[2] is not None:
                values[0] = values[2]
            result.append(complete_line(rate, *values))
        except (ValueError, AmountError) as exc:
            errors[f"zeile-{i}"] = str(exc)
    return result


def _parse_header(db: Session, form: Mapping[str, str], errors: dict) -> dict:
    values: dict = {}

    def text(key, name, limit=200):
        value = (form.get(key) or "").strip()
        if len(value) > limit:
            errors[key] = f"Höchstens {limit} Zeichen."
        values[name] = value or None

    def a_date(key, name):
        try:
            values[name] = parse_date(form.get(key))
        except ValueError as exc:
            errors[key] = str(exc)

    text("number", "number", 60)
    text("partner_name", "partner_name")
    text("notes", "notes", 4000)
    a_date("doc_date", "doc_date")
    a_date("due_date", "due_date")
    a_date("skonto_until", "skonto_until")

    supplier_raw = (form.get("supplier_id") or "").strip()
    values["supplier_id"] = None
    if supplier_raw:
        supplier = db.get(Supplier, int(supplier_raw)) if supplier_raw.isdigit() else None
        if supplier is None:
            errors["supplier_id"] = "Unbekannter Lieferant."
        else:
            values["supplier_id"] = supplier.id
            if not values["partner_name"]:
                values["partner_name"] = supplier.name

    account = (form.get("account") or "").strip().split(" ")[0]
    if account and not account_valid(account):
        errors["account"] = "Kontonummer mit 4 bis 8 Ziffern, z. B. 4930."
    values["account"] = account or None

    try:
        skonto = parse_decimal(form.get("skonto_percent"))
        if skonto is not None and not (0 < skonto < 100):
            errors["skonto_percent"] = "Skonto zwischen 0 und 100 %."
        values["skonto_percent"] = skonto
    except ValueError as exc:
        errors["skonto_percent"] = str(exc)
    if values.get("due_date") and values.get("doc_date") and values["due_date"] < values["doc_date"]:
        errors["due_date"] = "Fälligkeit liegt vor dem Rechnungsdatum."

    kind = (form.get("reverse_charge") or "").strip()
    if kind and kind not in steuerschuld.KINDS_IN:
        errors["reverse_charge"] = "Unbekannter Fall."
    values["reverse_charge"] = kind or None
    rate = (form.get("reverse_charge_rate") or "19").strip()
    if kind and rate not in steuerschuld.RATES:
        errors["reverse_charge"] = "Steuersatz 19 % oder 7 %."
    values["reverse_charge_rate"] = Decimal(rate) if kind and rate in steuerschuld.RATES else None
    return values


def save_form(db: Session, doc: Document, form: Mapping[str, str], *, user_id: int, ip: str | None) -> dict:
    """Eingaben prüfen und speichern (nur Entwürfe). Gibt Fehler je Feld zurück."""
    if not doc.is_draft:
        return {"_": "Festgeschriebene Belege können nicht mehr geändert werden."}
    errors: dict[str, str] = {}
    header = _parse_header(db, form, errors)
    lines = _parse_lines(form, errors, zero=bool(header.get("reverse_charge")))
    if errors:
        return errors
    old = _snapshot(doc)
    for key, value in header.items():
        setattr(doc, key, value)
    _set_lines(doc, lines)
    doc.review_fields = []
    doc.field_states = {}    # Ampel der KI-Auslese: ein Mensch hat die Werte geprüft
    doc.account_hint = None  # Vorschlag ist von einem Menschen geprüft
    changes = audit.diff(old, _snapshot(doc))
    if changes:
        _log(db, doc, "geaendert", user_id, ip, changes)
    return {}


def _missing_for_takeover(doc: Document) -> list[str]:
    missing = []
    if not doc.partner_name:
        missing.append("Absender")
    if not doc.doc_date:
        missing.append("Rechnungsdatum")
    if not doc.lines:
        missing.append("Beträge")
    elif doc.reverse_charge and any(line.vat for line in doc.lines):
        missing.append("§ 13b: Beträge ohne Umsatzsteuer (0 %)")
    return missing


def missing_for_finalize(doc: Document) -> list[str]:
    missing = _missing_for_takeover(doc)
    if not doc.number:
        missing.append("Rechnungsnummer")
    if not doc.account:
        missing.append("Konto")
    return missing


def take_over(db: Session, doc: Document, *, user_id: int, ip: str | None) -> list[str]:
    """Aus dem Prüfbereich übernehmen. Gibt fehlende Angaben zurück (leer = übernommen)."""
    if not doc.pending:
        return []
    missing = _missing_for_takeover(doc)
    if missing:
        return missing
    doc.pending = False
    doc.needs_review = False
    doc.reviewed_at = utcnow()
    doc.reviewed_by_id = user_id
    doc.field_states = {}
    from app.services import ki_auslese

    ki_auslese.record_takeover(db, doc, user_id)   # Protokoll: KI-Vorschlag ↔ freigegeben
    _log(db, doc, "uebernommen", user_id, ip)
    return []


def finalize(db: Session, doc: Document, *, user_id: int, ip: str | None, reason: str = "") -> list[str]:
    if doc.pending:
        return ["Beleg liegt noch im Prüfbereich"]
    if doc.is_final:
        return []
    missing = missing_for_finalize(doc)
    if missing:
        return missing
    doc.state = STATE_FINAL
    doc.finalized_at = utcnow()
    doc.finalized_by_id = user_id
    _log(db, doc, "festgeschrieben", user_id, ip, {"anlass": reason} if reason else None)
    return []


# --------------------------------------------------------------- Zahlungen
def skonto_offer(doc: Document) -> Decimal | None:
    """Möglicher Skonto-Abzug (brutto), solange noch nichts bezahlt wurde."""
    if not doc.skonto_percent or doc.gross_total is None or doc.payments:
        return None
    return skonto_amount(doc.gross_total, doc.skonto_percent)


def _update_status(doc: Document) -> None:
    if doc.status not in (STATUS_OPEN, STATUS_PAID):
        return
    paid = doc.paid_total
    doc.status = STATUS_PAID if doc.gross_total is not None and paid >= doc.gross_total else STATUS_OPEN


def add_payment(db: Session, doc: Document, form: Mapping[str, str], *, user_id: int, ip: str | None,
                bank_transaction_id: int | None = None) -> dict:
    errors: dict[str, str] = {}
    if doc.pending:
        return {"_": "Beleg liegt noch im Prüfbereich."}
    if doc.status != STATUS_OPEN:
        return {"_": "Für diesen Beleg ist nichts mehr offen."}
    if doc.direction != DIRECTION_IN and doc.is_draft:
        return {"_": "Zahlungen können erst nach dem Festschreiben erfasst werden."}
    try:
        paid_on = parse_date(form.get("paid_on")) or date.today()
    except ValueError as exc:
        errors["paid_on"] = str(exc)
        paid_on = None
    skonto = skonto_offer(doc) if form.get("mit_skonto") else None
    skonto = skonto or Decimal("0.00")
    try:
        amount = parse_decimal(form.get("amount"))
        amount = round_money(amount) if amount is not None else doc.open_amount - skonto
    except ValueError as exc:
        errors["amount"] = str(exc)
        amount = None
    if amount is not None and amount <= 0:
        errors["amount"] = "Bitte einen Betrag größer 0 angeben."
    elif amount is not None and amount + skonto > doc.open_amount:
        errors["amount"] = f"Mehr als der offene Betrag ({format_money(doc.open_amount)})."
    note = (form.get("note") or "").strip()[:300] or None
    if errors:
        return errors

    if doc.is_draft:  # Entscheidung: spätestens bei der ersten Zahlung festschreiben
        missing = finalize(db, doc, user_id=user_id, ip=ip, reason="erste Zahlung")
        if missing:
            return {"_": "Vor der ersten Zahlung muss der Beleg vollständig sein. Es fehlt: " + ", ".join(missing)}
    if skonto and doc.skonto_until and paid_on > doc.skonto_until:
        note = (note + " · " if note else "") + f"Skonto-Frist ({format_date(doc.skonto_until)}) überschritten"
    payment = Payment(tenant_id=doc.tenant_id, paid_on=paid_on, amount=amount, skonto=skonto, note=note,
                      created_by_id=user_id, bank_transaction_id=bank_transaction_id)
    doc.payments.append(payment)
    db.flush()
    _update_status(doc)
    _log(db, doc, "zahlung", user_id, ip, {"datum": format_date(paid_on), "betrag": str(amount),
                                           "skonto": str(skonto)})
    return {}


def delete_payment(db: Session, doc: Document, payment_id: int, *, user_id: int, ip: str | None) -> bool:
    payment = next((p for p in doc.payments if p.id == payment_id), None)
    if payment is None:
        return False
    _log(db, doc, "zahlung_geloescht", user_id, ip, {"datum": format_date(payment.paid_on),
                                                     "betrag": str(payment.amount), "skonto": str(payment.skonto)})
    doc.payments.remove(payment)
    db.flush()
    _update_status(doc)
    return True


# ------------------------------------------------- Häkchen „Bezahlt“
CHECK_NOTE = "Als bezahlt abgehakt"


def mark_paid(db: Session, doc: Document, paid_on_text: str, *, user_id: int, ip: str | None) -> str | None:
    """Häkchen „Bezahlt“: offenen Restbetrag als eine Zahlung zum angegebenen Tag buchen.
    Gibt eine Fehlermeldung zurück oder None."""
    # Eingangsrechnungen dürfen Entwurf sein – die erste Zahlung schreibt sie fest (add_payment prüft die Angaben)
    if doc.kind != KIND_INVOICE or (doc.is_draft and doc.direction != DIRECTION_IN):
        return "Nur festgeschriebene Rechnungen können als bezahlt markiert werden."
    try:
        paid_on = parse_date(paid_on_text)
    except ValueError as exc:
        return str(exc)
    if paid_on is None:
        return "Bitte das Zahlungsdatum angeben."
    if paid_on > date.today():
        return "Das Zahlungsdatum liegt in der Zukunft."
    if doc.doc_date and paid_on < doc.doc_date:
        return "Das Zahlungsdatum liegt vor dem Rechnungsdatum."
    errors = add_payment(db, doc, {"paid_on": format_date(paid_on), "amount": "", "note": CHECK_NOTE},
                         user_id=user_id, ip=ip)
    return " ".join(errors.values()) or None


def unmark_paid(db: Session, doc: Document, *, user_id: int, ip: str | None) -> str | None:
    """Häkchen entfernen: nur die per Häkchen gebuchten Zahlungen löschen (nie Zahlungen aus dem Bankabgleich)."""
    payments = [p for p in doc.payments if p.note == CHECK_NOTE and p.bank_transaction_id is None]
    if not payments:
        return ("Die Zahlung wurde nicht per Häkchen gebucht (z. B. Bankabgleich oder Teilzahlung) – "
                "bitte in der Zahlungsliste der Rechnung entfernen.")
    for payment in payments:
        delete_payment(db, doc, payment.id, user_id=user_id, ip=ip)
    return None


# ------------------------------------------------------- Storno / Löschen
def cancel(db: Session, doc: Document, reason: str, *, user_id: int, ip: str | None) -> str | None:
    if not doc.is_final:
        return "Nur festgeschriebene Belege werden storniert – Entwürfe können gelöscht werden."
    if doc.status == STATUS_CANCELLED:
        return None
    if not reason.strip():
        return "Bitte einen Grund angeben."
    doc.status = STATUS_CANCELLED
    doc.cancel_reason = reason.strip()[:2000]
    _log(db, doc, "storniert", user_id, ip, {"grund": doc.cancel_reason})
    return None


def delete_draft(db: Session, doc: Document, *, user_id: int, ip: str | None) -> str | None:
    """Löschen ist nur für Entwürfe erlaubt (GoBD). Die Datei wird entfernt, wenn kein
    anderer Beleg sie benutzt (z. B. ein Duplikat)."""
    if not doc.is_draft:
        return "Festgeschriebene Belege können nicht gelöscht werden – bitte stornieren."
    _log(db, doc, "geloescht", user_id, ip, _snapshot(doc))
    stored = doc.file
    db.delete(doc)
    db.flush()
    if stored is not None:
        others = db.scalar(select(func.count()).select_from(Document).where(Document.file_id == stored.id)
                           .execution_options(include_pending=True))
        if not others:
            ablage.absolute_path(stored).unlink(missing_ok=True)
            db.delete(stored)
    return None


def duplicate(db: Session, doc: Document, *, user_id: int, ip: str | None) -> Document:
    """Kopie als neuer Entwurf im Prüfbereich (z. B. für monatlich gleiche Rechnungen)."""
    copy = Document(
        tenant_id=doc.tenant_id, direction=doc.direction, kind=doc.kind, pending=True, source="duplikat",
        state=STATE_DRAFT, status=STATUS_OPEN, number=f"{doc.number} (Kopie)" if doc.number else None,
        doc_date=doc.doc_date, due_date=doc.due_date, partner_name=doc.partner_name, supplier_id=doc.supplier_id,
        account=doc.account, notes=doc.notes, skonto_percent=doc.skonto_percent, skonto_until=doc.skonto_until,
        reverse_charge=doc.reverse_charge, reverse_charge_rate=doc.reverse_charge_rate, needs_review=True, review_fields=["number", "doc_date"], file_id=doc.file_id, created_by_id=user_id,
    )
    db.add(copy)
    _set_lines(copy, tax_lines_of(doc))
    db.flush()
    _log(db, copy, "dupliziert", user_id, ip, {"von": doc.id})
    return copy


# ------------------------------------------------------- Dateien bündeln
def _export_name(doc: Document, ext: str) -> str:
    parts = [doc.doc_date.isoformat() if doc.doc_date else "ohne-datum", doc.partner_name or "unbekannt",
             doc.number or f"beleg-{doc.id}"]
    return ablage.clean_filename("_".join(parts)).replace(" ", "-")[:120] + "." + ext


def zip_files(docs: list[Document]) -> bytes:
    buf = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for doc in docs:
            if doc.file is None:
                continue
            path = ablage.absolute_path(doc.file)
            if not path.exists():
                continue
            name = _export_name(doc, path.suffix.lstrip("."))
            while name in used:
                name = f"{doc.id}-{name}"
            used.add(name)
            zf.writestr(name, ablage.read_bytes(doc.file))
    return buf.getvalue()


def merged_pdf(docs: list[Document]) -> tuple[bytes, int]:
    """Alle PDF-Belege zu einer Datei zum Drucken zusammenfügen. Gibt (PDF, übersprungene) zurück."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    skipped = 0
    for doc in docs:
        if doc.file is None or doc.file.content_type != "application/pdf":
            skipped += 1
            continue
        try:
            writer.append(io.BytesIO(ablage.read_bytes(doc.file)))
        except Exception:  # noqa: BLE001 – kaputte Datei überspringen
            skipped += 1
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), skipped
