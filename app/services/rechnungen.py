"""Ausgangsrechnungen (eigene Rechnungen): Entwurf -> Festschreiben (Nummer + PDF) -> Zahlungen / Storno.

Regeln (KONZEPT.md 6.5/6.6):
- Die Rechnungsnummer wird erst beim Festschreiben vergeben (lückenlos, je Jahr).
- Beim Festschreiben wird das PDF einmal erzeugt und unverändert mit SHA-256 gespeichert.
- Vorher werden die Pflichtangaben nach § 14 UStG geprüft.
- Korrektur festgeschriebener Rechnungen nur per Stornorechnung (eigene Nummer, Bezug auf das Original).
- Kleinunternehmer: keine Umsatzsteuer, Hinweis § 19 UStG automatisch.
Alle Funktionen schreiben ins Änderungsprotokoll und rufen KEIN commit auf.
"""
from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import utcnow
from app.formatting import format_date, format_money, format_price, parse_date, parse_decimal, round_money
from app.models import CatalogItem, Customer, CustomerAddress, Document, DocumentLine
from app.models.belege import (
    DIRECTION_OUT,
    KIND_INVOICE,
    KIND_STORNO,
    STATE_DRAFT,
    STATE_FINAL,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_OPEN,
)
from app.models.stammdaten import UNITS
from app.services import ablage, audit, einstellungen, nummernkreise, steuerschuld
from app.services.belege import _log, _snapshot
from app.services.betraege import group_by_rate, position_net, totals

VAT_RATES = ("19", "7", "0")
EMPTY_ROWS = 3
MAX_ROWS = 100
MONTHS = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
          "November", "Dezember")

KU_HINT = "Gemäß § 19 UStG wird keine Umsatzsteuer berechnet."
HINTS: dict[str, tuple[str, str]] = {
    # Schlüssel: (Bezeichnung im Formular, Text auf der Rechnung)
    "13b": ("Reverse-Charge (§ 13b UStG) – Kunde schuldet die Umsatzsteuer",
            "Steuerschuldnerschaft des Leistungsempfängers (§ 13b UStG)."),
    "35a": ("Lohnanteil für haushaltsnahe Dienstleistungen ausweisen (§ 35a EStG)",
            "Im Rechnungsbetrag sind Arbeitskosten (Lohnanteil) in Höhe von {lohn} enthalten (§ 35a EStG)."),
    "14b": ("Aufbewahrungshinweis für Privatkunden (§ 14b UStG)",
            "Sie sind gesetzlich verpflichtet, diese Rechnung zwei Jahre lang aufzubewahren (§ 14b UStG)."),
}


# ---------------------------------------------------------------- Laden
def get(db: Session, doc_id: int) -> Document | None:
    doc = db.get(Document, doc_id)
    return doc if doc is not None and doc.direction == DIRECTION_OUT else None


def outgoing(db: Session, *, q: str = "", status: str = "", year: int | None = None, overdue: bool = False,
             customer_id: int | None = None, limit: int = 500) -> list[Document]:
    stmt = select(Document).where(Document.direction == DIRECTION_OUT)
    if status == "entwurf":
        stmt = stmt.where(Document.state == STATE_DRAFT)
    elif status:
        stmt = stmt.where(Document.status == status, Document.state == STATE_FINAL)
    if overdue:
        stmt = stmt.where(Document.status == STATUS_OPEN, Document.state == STATE_FINAL,
                          Document.due_date < date.today())
    if year:
        stmt = stmt.where(Document.doc_date >= date(year, 1, 1), Document.doc_date <= date(year, 12, 31))
    if customer_id:
        stmt = stmt.where(Document.customer_id == customer_id)
    if q.strip():
        pattern = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(Document.recipient_name).like(pattern) | func.lower(Document.number).like(pattern))
    stmt = stmt.order_by(Document.state.desc(), Document.doc_date.desc().nulls_last(), Document.id.desc()).limit(limit)
    return list(db.scalars(stmt))


def years(db: Session) -> list[int]:
    dates = db.scalars(select(Document.doc_date).where(Document.direction == DIRECTION_OUT,
                                                       Document.doc_date.is_not(None)))
    return sorted({d.year for d in dates}, reverse=True)


def customer_summary(db: Session, customer_id: int) -> dict:
    """Für die Kunden-Detailseite (nur geprüfte Belege – der Prüfbereich ist automatisch ausgefiltert)."""
    docs = outgoing(db, customer_id=customer_id, limit=100)
    final = [d for d in docs if d.is_final and d.kind == KIND_INVOICE and d.status != STATUS_CANCELLED]
    return {
        "docs": docs,
        "revenue_net": sum((d.net_total or Decimal(0) for d in final), Decimal("0.00")),
        "open": sum((d.open_amount for d in final), Decimal("0.00")),
    }


def is_small_business(db: Session) -> bool:
    return bool(einstellungen.get(db, "steuer.kleinunternehmer"))


def hint_texts(db: Session, doc: Document) -> list[str]:
    texts = []
    if is_small_business(db):
        texts.append(KU_HINT)
    for key in doc.hints or []:
        if key == "13b":
            texts.append(steuerschuld.hint_text(doc))
        elif key in HINTS:
            text = HINTS[key][1]
            texts.append(text.replace("{lohn}", format_money(doc.labor_amount or Decimal(0))))
    return texts


# ------------------------------------------------------------- Entwurf
def recipient_of(customer: Customer) -> tuple[str, str]:
    """Empfänger (Name, Adresse) – die abweichende Rechnungsadresse hat Vorrang."""
    if customer.has_billing_address:
        name = customer.billing_name or customer.name
        lines = [customer.billing_street, f"{customer.billing_zip or ''} {customer.billing_city or ''}".strip(),
                 customer.billing_country]
    else:
        name = customer.name
        lines = [customer.contact_person, customer.street,
                 f"{customer.zip or ''} {customer.city or ''}".strip(), customer.country]
    lines = [x for x in lines if x and x != "Deutschland"]
    return name, "\n".join(lines)


def create_draft(db: Session, customer: Customer, *, user_id: int, ip: str | None) -> Document:
    values = einstellungen.load(db)
    name, address = recipient_of(customer)
    terms = customer.payment_terms_days
    if terms is None:
        terms = int(values.get("design.zahlungsziel_tage") or 14)
    doc = Document(
        direction=DIRECTION_OUT, kind=KIND_INVOICE, pending=False, source="manuell", state=STATE_DRAFT,
        status=STATUS_OPEN, customer_id=customer.id, partner_name=customer.name, recipient_name=name,
        recipient_address=address, doc_date=date.today(), payment_terms_days=terms,
        intro_text=values.get("rechnung.einleitung"), closing_text=values.get("design.danke_text"),
        payment_qr=values.get("rechnung.zahlungs_qr") or "girocode",
        hints=["13b"] if customer.reverse_charge else [], created_by_id=user_id,
    )
    doc.customer = customer
    set_reverse_charge(doc)
    db.add(doc)
    db.flush()
    _log(db, doc, "angelegt", user_id, ip, {"kunde": customer.name})
    return doc


def add_lines(db: Session, doc: Document, rows: list[dict]) -> None:
    """Zeilen (title, description, quantity, unit, unit_price, vat_rate) an einen Entwurf anhängen.
    Kleinunternehmer: immer 0 %. Danach ``recalc`` aufrufen."""
    zero = is_small_business(db)
    start = max((line.position for line in doc.lines), default=0)
    for i, row in enumerate(rows, start=start + 1):
        rate = Decimal(0) if zero else row["vat_rate"]
        net = round_money(row["quantity"] * row["unit_price"])
        vat = round_money(net * rate / 100)
        doc.lines.append(DocumentLine(tenant_id=doc.tenant_id, position=i, title=row["title"][:300],
                                      description=row.get("description"), quantity=row["quantity"],
                                      unit=row.get("unit"), unit_price=row["unit_price"], vat_rate=rate,
                                      net=net, vat=vat, gross=net + vat))


def duplicate(db: Session, doc: Document, *, user_id: int, ip: str | None) -> Document:
    """Neuer Entwurf mit gleichen Positionen (Datum heute, Leistungsdatum neu eintragen)."""
    copy = Document(
        direction=DIRECTION_OUT, kind=KIND_INVOICE, pending=False, source="duplikat", state=STATE_DRAFT,
        status=STATUS_OPEN, customer_id=doc.customer_id, partner_name=doc.partner_name,
        recipient_name=doc.recipient_name, recipient_address=doc.recipient_address, object_address=doc.object_address,
        doc_date=date.today(), payment_terms_days=doc.payment_terms_days, intro_text=doc.intro_text,
        closing_text=doc.closing_text, payment_qr=doc.payment_qr, hints=list(doc.hints or []),
        labor_amount=doc.labor_amount, notes=doc.notes, created_by_id=user_id,
    )
    db.add(copy)
    db.flush()
    for line in doc.lines:
        copy.lines.append(DocumentLine(
            tenant_id=copy.tenant_id, position=line.position, title=line.title, description=line.description,
            quantity=line.quantity, unit=line.unit, unit_price=line.unit_price, discount_percent=line.discount_percent,
            vat_rate=line.vat_rate, net=line.net, vat=line.vat, gross=line.gross))
    copy.net_total, copy.vat_total, copy.gross_total = doc.net_total, doc.vat_total, doc.gross_total
    if doc.kind == KIND_STORNO:  # Kopie einer Stornorechnung wäre eine negative Rechnung – Vorzeichen drehen
        _negate(copy)
    db.flush()
    _log(db, copy, "dupliziert", user_id, ip, {"von": doc.number or doc.id})
    return copy


# ------------------------------------------------------------- Formular
def form_rows(doc: Document, extra: int = EMPTY_ROWS) -> list[dict]:
    def num(value, places):
        if value is None:
            return ""
        text = f"{Decimal(value):.{places}f}".replace(".", ",")
        return text.rstrip("0").rstrip(",") if "," in text else text

    rows = [{"titel": line.title, "beschreibung": line.description or "", "menge": num(line.quantity, 4),
             "einheit": line.unit or "Stück",
             "preis": format_price(line.unit_price) if line.unit_price is not None else "",
             "rabatt": num(line.discount_percent, 2), "satz": f"{line.vat_rate.normalize():f}", "katalog": ""}
            for line in doc.lines]
    return rows + empty_rows(extra)


def empty_rows(count: int) -> list[dict]:
    return [{"titel": "", "beschreibung": "", "menge": "", "einheit": "Stück", "preis": "", "rabatt": "",
             "satz": "", "katalog": ""} for _ in range(count)]


def _parse_rows(db: Session, form: Mapping[str, str], errors: dict, force_zero: bool,
                default_rate: str) -> list[dict]:
    rows = []
    for i in range(MAX_ROWS):
        prefix = f"pos-{i}-"
        if prefix + "titel" not in form:
            break
        raw = {k: (form.get(prefix + k) or "").strip() for k in
               ("titel", "beschreibung", "menge", "einheit", "preis", "rabatt", "satz", "katalog")}
        if form.get(prefix + "entfernen"):
            continue
        if raw["katalog"].isdigit() and not raw["titel"]:
            item = db.get(CatalogItem, int(raw["katalog"]))
            if item is not None:
                raw.update(titel=item.title, beschreibung=raw["beschreibung"] or (item.description or ""),
                           einheit=item.unit, preis=f"{item.price_net}".replace(".", ","),
                           satz=f"{item.vat_rate.normalize():f}")
        if not (raw["titel"] or raw["preis"] or raw["menge"]):
            continue
        key = f"pos-{i}"
        try:
            if not raw["titel"]:
                raise ValueError("Bitte eine Bezeichnung angeben.")
            if len(raw["titel"]) > 300:
                raise ValueError("Bezeichnung höchstens 300 Zeichen.")
            quantity = parse_decimal(raw["menge"]) if raw["menge"] else Decimal(1)
            price = parse_decimal(raw["preis"])
            if price is None:
                raise ValueError("Bitte einen Einzelpreis angeben.")
            discount = parse_decimal(raw["rabatt"]) if raw["rabatt"] else None
            if discount is not None and not (0 <= discount <= 100):
                raise ValueError("Rabatt zwischen 0 und 100 %.")
            rate = Decimal(0) if force_zero else Decimal(raw["satz"] or default_rate)
            if f"{rate.normalize():f}" not in VAT_RATES:
                raise ValueError("Ungültiger Steuersatz.")
            unit = raw["einheit"] if raw["einheit"] in UNITS else "Stück"
            quantity = quantity.quantize(Decimal("0.0001"))
            price = price.quantize(Decimal("0.0001"))
        except (ValueError, ArithmeticError) as exc:
            errors[key] = str(exc)
            continue
        net = position_net(quantity, price, discount)
        vat = round_money(net * rate / 100)
        rows.append({"title": raw["titel"], "description": raw["beschreibung"] or None, "quantity": quantity,
                     "unit": unit, "unit_price": price, "discount_percent": discount, "vat_rate": rate,
                     "net": net, "vat": vat, "gross": net + vat})
    return rows


def save_form(db: Session, doc: Document, form: Mapping[str, str], *, user_id: int, ip: str | None) -> dict:
    """Entwurf speichern. Gibt Fehler je Feld zurück (leer = gespeichert)."""
    if not doc.is_draft:
        return {"_": "Festgeschriebene Rechnungen können nicht mehr geändert werden – bitte stornieren."}
    errors: dict[str, str] = {}
    values: dict = {}

    def a_date(key):
        try:
            values[key] = parse_date(form.get(key))
        except ValueError as exc:
            errors[key] = str(exc)

    for key in ("doc_date", "service_date", "service_until"):
        a_date(key)
    for key, limit in (("recipient_name", 200), ("recipient_address", 1000), ("object_address", 1000),
                       ("intro_text", 4000), ("closing_text", 4000), ("notes", 4000)):
        value = (form.get(key) or "").strip()
        if len(value) > limit:
            errors[key] = f"Höchstens {limit} Zeichen."
        values[key] = value or None
    if not values["recipient_name"]:
        errors["recipient_name"] = "Bitte den Empfänger angeben."
    address_id = (form.get("object_address_id") or "").strip()
    if address_id.isdigit():  # Objektadresse aus den Kundendaten übernehmen
        address = db.get(CustomerAddress, int(address_id))
        if address is None or address.customer_id != doc.customer_id:
            errors["object_address"] = "Unbekannte Objektadresse."
        else:
            values["object_address"] = "\n".join(
                x for x in (address.label, address.street, f"{address.zip or ''} {address.city or ''}".strip()) if x)

    terms_raw = (form.get("payment_terms_days") or "").strip()
    try:
        values["payment_terms_days"] = int(terms_raw) if terms_raw else 0
        if not 0 <= values["payment_terms_days"] <= 365:
            raise ValueError
    except ValueError:
        errors["payment_terms_days"] = "Zahlungsziel in Tagen (0 bis 365)."

    values["hints"] = [k for k in HINTS if k in form.getlist("hint")] if hasattr(form, "getlist") else []
    try:
        labor = parse_decimal(form.get("labor_amount"))
        values["labor_amount"] = round_money(labor) if labor is not None else None
    except ValueError as exc:
        errors["labor_amount"] = str(exc)
    qr = form.get("payment_qr") or "girocode"
    values["payment_qr"] = qr if qr in ("girocode", "keiner") else "girocode"

    if values.get("service_date") and values.get("service_until") and values["service_until"] < values["service_date"]:
        errors["service_until"] = "Das Ende liegt vor dem Beginn des Leistungszeitraums."

    force_zero = is_small_business(db) or "13b" in values["hints"]
    default_rate = "0" if force_zero else (einstellungen.get(db, "design.ust_satz") or "19")
    rows = _parse_rows(db, form, errors, force_zero, default_rate)
    if errors:
        return errors

    old = _snapshot(doc)
    for key, value in values.items():
        setattr(doc, key, value)
    doc.lines.clear()
    for i, row in enumerate(rows, start=1):
        doc.lines.append(DocumentLine(tenant_id=doc.tenant_id, position=i, **row))
    recalc(doc)
    set_reverse_charge(doc)
    if doc.labor_amount and doc.gross_total is not None and doc.labor_amount > doc.gross_total:
        return {"labor_amount": "Der Lohnanteil ist größer als der Rechnungsbetrag."}
    changes = audit.diff(old, _snapshot(doc))
    if changes:
        _log(db, doc, "geaendert", user_id, ip, changes)
    return {}


def recalc(doc: Document) -> None:
    if doc.lines:
        groups = group_by_rate((line.vat_rate, line.net) for line in doc.lines)
        doc.net_total, doc.vat_total, doc.gross_total = totals(groups)
    else:
        doc.net_total = doc.vat_total = doc.gross_total = None
    if doc.doc_date is not None and doc.payment_terms_days is not None:
        doc.due_date = doc.doc_date + timedelta(days=doc.payment_terms_days)


def set_reverse_charge(doc: Document) -> None:
    """§ 13b-Fall aus Land/USt-IdNr. des Kunden festhalten (inland → Kz 60, eu → Kz 21 + ZM)."""
    if "13b" in (doc.hints or []):
        doc.reverse_charge = steuerschuld.customer_zone(doc.customer)
    else:
        doc.reverse_charge = None


def tax_groups(doc: Document):
    return group_by_rate((line.vat_rate, line.net) for line in doc.lines)


# ------------------------------------------------------- Pflichtangaben
def missing_for_finalize(db: Session, doc: Document) -> list[str]:
    """Pflichtangaben nach § 14 Abs. 4 UStG (und was das Programm sonst zwingend braucht)."""
    missing: list[str] = []
    values = einstellungen.load(db)
    if not all(values.get(k) for k in ("firma.name", "firma.strasse", "firma.plz", "firma.ort")):
        missing.append("eigene Firmenanschrift (Einstellungen)")
    if not (values.get("steuer.steuernummer") or values.get("steuer.ust_id")):
        missing.append("Steuernummer oder USt-IdNr. (Einstellungen)")
    if not doc.recipient_name or not doc.recipient_address:
        missing.append("Name und Anschrift des Empfängers")
    if not doc.doc_date:
        missing.append("Rechnungsdatum")
    if not doc.service_date:
        missing.append("Leistungsdatum bzw. Leistungszeitraum")
    if not doc.lines:
        missing.append("mindestens eine Position")
    elif any(line.quantity is None or not line.unit or not line.title for line in doc.lines):
        missing.append("Menge, Einheit und Bezeichnung je Position")
    if "35a" in (doc.hints or []) and not doc.labor_amount:
        missing.append("Lohnanteil (§ 35a)")
    if "13b" in (doc.hints or []):
        if is_small_business(db):
            missing.append("§ 13b ist für Kleinunternehmer nicht möglich (Hinweis bitte abwählen)")
        elif doc.kind != KIND_STORNO and doc.customer is not None and                 steuerschuld.customer_zone(doc.customer) == steuerschuld.DRITTLAND:
            missing.append("§ 13b gilt nur für Kunden in Deutschland oder der EU – Kunde außerhalb der EU "
                           "(bitte mit der Steuerberatung klären)")
        elif steuerschuld.outgoing_kind(doc) == steuerschuld.EU or (
                doc.kind != KIND_STORNO and steuerschuld.customer_zone(doc.customer) == steuerschuld.EU):
            if not values.get("steuer.ust_id"):
                missing.append("eigene USt-IdNr. (Einstellungen – Pflicht bei Rechnungen an EU-Kunden)")
            if doc.customer is None or not doc.customer.ust_id:
                missing.append("USt-IdNr. des Kunden (Pflicht bei Rechnungen an EU-Kunden)")
    if is_small_business(db) and any(line.vat_rate != 0 for line in doc.lines):
        missing.append("Kleinunternehmer: alle Positionen mit 0 % (bitte einmal speichern)")
    from app.services import erechnung

    missing.extend(erechnung.missing(db, doc, erechnung.format_for(db, doc)))
    return missing


# ------------------------------------------------------- Festschreiben
def finalize(db: Session, doc: Document, *, user_id: int, ip: str | None) -> list[str]:
    """Nummer vergeben, PDF (ggf. mit E-Rechnung) erzeugen und unverändert speichern, Beleg sperren.
    Gibt Probleme zurück; dann muss der Aufrufer zurückrollen (die Nummer ist sonst verbraucht)."""
    from app.services import erechnung, pdf_rechnung

    if doc.is_final:
        return []
    missing = missing_for_finalize(db, doc)
    if missing:
        return missing
    recalc(doc)
    if doc.kind != KIND_STORNO:   # die Stornorechnung behält den Fall der Originalrechnung
        set_reverse_charge(doc)
    doc.number = nummernkreise.allocate(db, "storno" if doc.kind == KIND_STORNO else "rechnung", doc.doc_date)
    doc.state = STATE_FINAL
    doc.finalized_at = utcnow()
    doc.finalized_by_id = user_id
    if doc.kind == KIND_STORNO:
        doc.status = STATUS_DONE
    fmt = erechnung.format_for(db, doc)
    data = pdf_rechnung.render(db, doc)
    xml = None
    if fmt != "keine":
        try:
            xml = erechnung.build_cii(db, doc, fmt)
            if fmt == "zugferd":
                data = erechnung.embed(data, xml, doc, einstellungen.get(db, "firma.name") or "")
        except erechnung.EInvoiceError as exc:
            return [f"E-Rechnung: {exc}"]
    doc.e_invoice_format = fmt
    stored = ablage.save_bytes(db, data, tenant_id=doc.tenant_id, kind="rechnung_pdf",
                               original_name=pdf_filename(doc), content_type="application/pdf", user_id=user_id)
    doc.file_id = stored.id
    changes = {"nummer": doc.number, "sha256": stored.sha256, "brutto": str(doc.gross_total), "e_rechnung": fmt}
    if fmt == "xrechnung" and xml is not None:
        xml_file = ablage.save_bytes(db, xml, tenant_id=doc.tenant_id, kind="xrechnung",
                                     original_name=pdf_filename(doc).replace(".pdf", ".xml"),
                                     content_type="application/xml", user_id=user_id)
        doc.xml_file_id = xml_file.id
        changes["sha256_xml"] = xml_file.sha256
    db.flush()
    _log(db, doc, "festgeschrieben", user_id, ip, changes)
    return []


def pdf_filename(doc: Document) -> str:
    kind = "Stornorechnung" if doc.kind == KIND_STORNO else "Rechnung"
    number = (doc.number or "Entwurf").replace("/", "-")
    customer = ablage.clean_filename(doc.recipient_name or "").replace(" ", "")[:40]
    return f"{number}-{kind}-{customer}.pdf"


def copy_to_folder(db: Session, doc: Document) -> str | None:
    """Kopie ins Ablage-Ordner-System <Ordner>/<Jahr>/<MM Monat>/<Kunde>/. Fehler blockieren nichts,
    sie werden nur als Hinweis zurückgegeben."""
    from app.config import get_settings

    folder = einstellungen.get(db, "rechnung.ablage_ordner")
    if not folder or not get_settings().is_local or doc.file is None or doc.doc_date is None:
        return None
    try:
        customer = ablage.clean_filename(doc.recipient_name or "Kunde")
        target = (Path(folder) / str(doc.doc_date.year) / f"{doc.doc_date.month:02d} {MONTHS[doc.doc_date.month - 1]}"
                  / customer)
        target.mkdir(parents=True, exist_ok=True)
        (target / pdf_filename(doc)).write_bytes(ablage.read_bytes(doc.file, db))
        if doc.xml_file is not None:
            (target / doc.xml_file.original_name).write_bytes(ablage.read_bytes(doc.xml_file, db))
    except OSError as exc:
        return f"Die Kopie in den Ablage-Ordner ist fehlgeschlagen ({exc}). Die Rechnung selbst ist gespeichert."
    return None


# ------------------------------------------------------------------ Storno
def _negate(doc: Document) -> None:
    for line in doc.lines:
        line.quantity = -line.quantity if line.quantity is not None else None
        line.net, line.vat, line.gross = -line.net, -line.vat, -line.gross
    recalc(doc)


def cancel(db: Session, doc: Document, reason: str, *, user_id: int, ip: str | None) -> tuple[Document | None, str | None]:
    """Stornorechnung erzeugen und festschreiben; Original wird „storniert“."""
    if doc.kind != KIND_INVOICE or not doc.is_final:
        return None, "Nur festgeschriebene Rechnungen werden storniert – Entwürfe können gelöscht werden."
    if doc.status == STATUS_CANCELLED:
        return None, "Die Rechnung ist bereits storniert."
    if not reason.strip():
        return None, "Bitte einen Grund angeben."
    storno = Document(
        direction=DIRECTION_OUT, kind=KIND_STORNO, pending=False, source="storno", state=STATE_DRAFT,
        status=STATUS_OPEN, customer_id=doc.customer_id, partner_name=doc.partner_name,
        recipient_name=doc.recipient_name, recipient_address=doc.recipient_address, object_address=doc.object_address,
        doc_date=date.today(), service_date=doc.service_date, service_until=doc.service_until, payment_terms_days=0,
        intro_text=f"Hiermit stornieren wir die Rechnung Nr. {doc.number} vom {format_date(doc.doc_date)}.",
        closing_text=None, hints=list(doc.hints or []), labor_amount=-doc.labor_amount if doc.labor_amount else None,
        reverse_charge=doc.reverse_charge, payment_qr="keiner", storno_of_id=doc.id, notes=reason.strip()[:2000], created_by_id=user_id,
    )
    db.add(storno)
    db.flush()
    for line in doc.lines:
        storno.lines.append(DocumentLine(
            tenant_id=doc.tenant_id, position=line.position, title=line.title, description=line.description,
            quantity=line.quantity, unit=line.unit, unit_price=line.unit_price, discount_percent=line.discount_percent,
            vat_rate=line.vat_rate, net=line.net, vat=line.vat, gross=line.gross))
    _negate(storno)
    missing = finalize(db, storno, user_id=user_id, ip=ip)
    if missing:
        return None, "Stornorechnung nicht möglich: " + ", ".join(missing)
    doc.status = STATUS_CANCELLED
    doc.cancel_reason = reason.strip()[:2000]
    _log(db, doc, "storniert", user_id, ip, {"grund": doc.cancel_reason, "stornorechnung": storno.number})
    return storno, None


def storno_for(db: Session, doc: Document) -> Document | None:
    return db.scalar(select(Document).where(Document.storno_of_id == doc.id))
