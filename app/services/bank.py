"""Bankabgleich (KONZEPT.md 6.11): Kontoauszug importieren, Umsätze offenen Rechnungen zuordnen.

- Import: CSV (Spalten-Zuordnung wird je Bank-Format gespeichert) oder CAMT.053. Bereits importierte
  Umsätze (überlappende Auszüge) werden erkannt und übersprungen.
- Vorschläge: Rechnungsnummer im Verwendungszweck, gleicher Betrag (auch abzüglich Skonto), Name, IBAN.
- Bestätigen legt eine normale Zahlung an (``belege.add_payment``) – mit Verweis auf den Umsatz.
  Teilzahlungen und Sammelüberweisungen (ein Umsatz, mehrere Rechnungen) sind möglich.
- Umsätze ohne Rechnung: Umsatzsteuer an/vom Finanzamt (zählt in der EÜR), privat, nicht relevant.
Alle Funktionen schreiben ins Änderungsprotokoll und rufen KEIN commit auf.
"""
import re
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.formatting import format_date, format_decimal, format_money
from app.models import BankImport, BankTransaction, Document
from app.models.bank import CATEGORIES, TX_DONE, TX_OPEN
from app.models.belege import DIRECTION_IN, DIRECTION_OUT, KIND_INVOICE, STATE_FINAL, STATUS_OPEN, STATUS_PAID
from app.services import ablage, audit, belege, einstellungen, kontoauszug
from app.services.kontoauszug import StatementError

MAX_BYTES = 10 * 1024 * 1024
MAPPING_KEY = "bank.csv_zuordnungen"
SURE_SCORE = 90     # Rechnungsnummer und Betrag stimmen -> „sicherer“ Vorschlag
SUGGEST_SCORE = 50
_STOP_WORDS = {"gmbh", "mbh", "co", "kg", "ug", "ag", "ohg", "gbr", "und", "der", "die", "das", "von", "fuer",
               "für", "service", "services", "haftungsbeschränkt", "inh", "e.k."}


def _log(db: Session, action: str, entity_id, tenant_id: int, user_id: int | None, ip: str | None, changes=None):
    audit.log(db, action=action, entity="bank", entity_id=entity_id, tenant_id=tenant_id, user_id=user_id,
              changes=changes, ip=ip)


# =================================================================== Import
def upload(db: Session, data: bytes, filename: str, *, tenant_id: int, user_id: int, ip: str | None) -> BankImport:
    """Datei speichern. CAMT wird gleich eingelesen, CSV wartet auf die Spalten-Zuordnung."""
    if not data:
        raise StatementError("Die Datei ist leer.")
    if len(data) > MAX_BYTES:
        raise StatementError("Die Datei ist zu groß (höchstens 10 MB).")
    fmt = kontoauszug.detect_format(data)
    content_type = "text/csv" if fmt == "csv" else ("application/zip" if data.startswith(b"PK") else "application/xml")
    if fmt == "camt":
        rows = kontoauszug.parse_camt(data)  # vor dem Speichern prüfen
    else:
        rows = None
        parsed = kontoauszug.csv_rows(data)
        if len(parsed) < 2:
            raise StatementError("Die CSV-Datei enthält keine Umsätze.")
    stored = ablage.save_bytes(db, data, tenant_id=tenant_id, kind="kontoauszug", original_name=filename or "auszug",
                               content_type=content_type, user_id=user_id)
    imp = BankImport(format=fmt, status="zuordnung" if rows is None else "fertig", file_id=stored.id,
                     created_by_id=user_id)
    db.add(imp)
    db.flush()
    if rows is not None:
        _store(db, imp, rows)
        _log(db, "importiert", imp.id, tenant_id, user_id, ip,
             {"datei": stored.original_name, "neu": imp.count_new, "doppelt": imp.count_duplicate})
    else:
        preview = csv_preview(db, imp)
        if preview["known"]:  # Zuordnung für diese Bank schon gespeichert -> gleich einlesen
            apply_mapping(db, imp, preview["mapping"], user_id=user_id, ip=ip)
    return imp


def _store(db: Session, imp: BankImport, rows: list[kontoauszug.RawTx]) -> None:
    existing = set(db.scalars(select(BankTransaction.fingerprint)))
    new = dup = 0
    for raw, fp in zip(rows, kontoauszug.fingerprints(rows), strict=True):
        if fp in existing:
            dup += 1
            continue
        existing.add(fp)
        db.add(BankTransaction(import_id=imp.id, booking_date=raw.booking_date, value_date=raw.value_date,
                               amount=raw.amount, name=raw.name or None, iban=raw.iban or None,
                               purpose=raw.purpose or None, fingerprint=fp, status=TX_OPEN))
        new += 1
    imp.count_new, imp.count_duplicate = new, dup
    db.flush()


def file_bytes(imp: BankImport) -> bytes:
    return ablage.read_bytes(imp.file)


def csv_preview(db: Session, imp: BankImport) -> dict:
    """Spaltennamen, gespeicherte bzw. geratene Zuordnung und die ersten Zeilen."""
    rows = kontoauszug.csv_rows(file_bytes(imp))
    start = kontoauszug.header_index(rows)
    header = list(rows[start]) if rows else []
    saved = (einstellungen.get(db, MAPPING_KEY) or {}).get(_signature(header))
    mapping = saved or kontoauszug.guess_mapping(header)
    return {"header": header, "mapping": mapping, "known": bool(saved),
            "sample": [r for r in rows[start + 1:start + 6] if any(r)]}


def _signature(header: list[str]) -> str:
    return "|".join(h.strip().lower() for h in header)[:500]


def apply_mapping(db: Session, imp: BankImport, mapping: dict[str, str], *, user_id: int,
                  ip: str | None) -> str | None:
    """CSV mit der gewählten Zuordnung einlesen. Gibt eine Fehlermeldung zurück (None = erledigt)."""
    if imp.status != "zuordnung":
        return "Dieser Kontoauszug wurde bereits eingelesen."
    mapping = {k: v for k, v in mapping.items() if v}
    problem = kontoauszug.mapping_problem(mapping)
    if problem:
        return problem
    data = file_bytes(imp)
    rows, skipped = kontoauszug.parse_csv(data, mapping)
    if not rows:
        return "Mit dieser Zuordnung wurde kein Umsatz erkannt – bitte Datums- und Betrags-Spalte prüfen."
    _store(db, imp, rows)
    imp.status = "fertig"
    all_rows = kontoauszug.csv_rows(data)
    header = all_rows[kontoauszug.header_index(all_rows)]
    saved = dict(einstellungen.get(db, MAPPING_KEY) or {})
    saved[_signature(header)] = mapping
    einstellungen.set_values(db, {MAPPING_KEY: saved})  # beim nächsten Auszug derselben Bank nicht mehr fragen
    _log(db, "importiert", imp.id, imp.tenant_id, user_id, ip,
         {"datei": imp.file.original_name, "neu": imp.count_new, "doppelt": imp.count_duplicate,
          "uebersprungen": skipped})
    return None


def delete_import(db: Session, imp: BankImport, *, user_id: int, ip: str | None) -> str | None:
    """Nur möglich, solange keiner seiner Umsätze zugeordnet ist."""
    txs = list(db.scalars(select(BankTransaction).where(BankTransaction.import_id == imp.id)))
    if any(tx.payments or tx.category for tx in txs):
        return "Es sind bereits Umsätze aus diesem Auszug zugeordnet – bitte erst die Zuordnungen aufheben."
    for tx in txs:
        db.delete(tx)
    stored = imp.file
    _log(db, "import_geloescht", imp.id, imp.tenant_id, user_id, ip,
         {"datei": stored.original_name, "umsaetze": len(txs)})
    db.delete(imp)
    db.flush()
    ablage.absolute_path(stored).unlink(missing_ok=True)
    db.delete(stored)
    return None


# =================================================================== Listen
def transactions(db: Session, *, show: str = "offen", q: str = "", limit: int = 500) -> list[BankTransaction]:
    stmt = select(BankTransaction)
    if show == "offen":
        stmt = stmt.where(BankTransaction.status == TX_OPEN)
    elif show == "erledigt":
        stmt = stmt.where(BankTransaction.status == TX_DONE)
    if q.strip():
        pattern = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(BankTransaction.name).like(pattern)
                          | func.lower(BankTransaction.purpose).like(pattern))
    stmt = stmt.order_by(BankTransaction.booking_date.desc(), BankTransaction.id.desc()).limit(limit)
    return list(db.scalars(stmt))


def open_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(BankTransaction)
                     .where(BankTransaction.status == TX_OPEN)) or 0


def unbacked_payments(doc: Document) -> list:
    """Zahlungen ohne Bankbeleg (Häkchen „Bezahlt“ oder von Hand) einer vollständig bezahlten Rechnung, die noch
    keinem Bankumsatz zugeordnet ist. Ein passender Bankumsatz darf sie ersetzen – wichtig bei Ist-Versteuerung,
    denn dann zählt das echte Zahlungsdatum (das Häkchen schlägt „heute“ vor)."""
    if doc.status != STATUS_PAID or any(p.bank_transaction_id for p in doc.payments):
        return []
    return list(doc.payments)


def _target(doc: Document) -> Decimal:
    """Betrag, den ein Bankumsatz treffen soll: offener Rest bzw. die ersetzbaren Zahlungen."""
    if doc.status == STATUS_PAID:
        return sum((p.amount for p in unbacked_payments(doc)), Decimal("0.00"))
    return doc.open_amount


def open_documents(db: Session, incoming_money: bool) -> list[Document]:
    """Offene Belege (und bezahlte ohne Bankbeleg), zu denen ein Geldeingang (bzw. -ausgang) passen kann.
    Prüfbereich zählt nie mit."""
    status = Document.status.in_((STATUS_OPEN, STATUS_PAID))
    if incoming_money:
        stmt = select(Document).where(
            status,
            ((Document.direction == DIRECTION_OUT) & (Document.state == STATE_FINAL)
             & (Document.kind == KIND_INVOICE))
            | ((Document.direction == DIRECTION_IN) & (Document.kind == "gutschrift")))
    else:
        stmt = select(Document).where(status, Document.direction == DIRECTION_IN, Document.kind != "gutschrift")
    return [d for d in db.scalars(stmt.order_by(Document.due_date.nulls_last(), Document.id)) if _target(d) > 0]


# =============================================================== Vorschläge
@dataclass
class Suggestion:
    doc: Document
    score: int
    amount: Decimal
    skonto: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def sure(self) -> bool:
        return self.score >= SURE_SCORE


def _number_pattern(number: str) -> re.Pattern | None:
    chars = [c for c in number.lower() if c.isalnum()]
    if len(chars) < 3:
        return None
    # Trennzeichen dürfen fehlen oder anders sein: „RE 2026-001“ findet „RE2026/001“
    body = r"[\s\-/._]?".join(re.escape(c) for c in chars)
    return re.compile(r"(?<![0-9a-z])" + body + r"(?![0-9a-z])")


def _name_tokens(name: str | None) -> list[str]:
    words = re.findall(r"[\wäöüß]{4,}", (name or "").lower())
    return [w for w in words if w not in _STOP_WORDS]


def suggestions(db: Session, tx: BankTransaction, docs: list[Document] | None = None) -> list[Suggestion]:
    if tx.remaining <= 0:
        return []
    docs = docs if docs is not None else open_documents(db, tx.amount > 0)
    money = tx.remaining
    text = f"{tx.purpose or ''} {tx.name or ''}".lower()
    iban = (tx.iban or "").replace(" ", "").upper()
    result = []
    for doc in docs:
        score, reasons, skonto = 0, [], False
        pattern = _number_pattern(doc.number or "")
        if pattern is not None and pattern.search(text):
            score += 60
            reasons.append("Rechnungsnummer im Verwendungszweck")
        offer = belege.skonto_offer(doc) if doc.direction == DIRECTION_IN else None
        target = _target(doc)
        replaced = unbacked_payments(doc)
        if replaced and doc.doc_date and tx.booking_date < doc.doc_date:
            continue   # eine schon bezahlte Rechnung kann nicht vor ihrem Rechnungsdatum bezahlt worden sein
        if money == target:
            score += 30
            reasons.append("Betrag stimmt")
        elif offer and money == target - offer:
            score += 30
            skonto = True
            reasons.append("Betrag abzüglich Skonto")
        elif money < target and score and not replaced:
            reasons.append("Teilzahlung")
        elif replaced:
            continue   # bezahlte Rechnung: nur ein Umsatz über den ganzen Betrag ersetzt die Zahlung
        if replaced:
            days = ", ".join(sorted({format_date(p.paid_on) for p in replaced}))
            reasons.append(f"ersetzt die Zahlung vom {days} ohne Bankbeleg")
        if doc.supplier is not None and doc.supplier.iban and iban and doc.supplier.iban.replace(" ", "") == iban:
            score += 40
            reasons.append("IBAN des Lieferanten")
        partner = doc.partner_name or doc.recipient_name
        if any(t in text for t in _name_tokens(partner)):
            score += 20
            reasons.append("Name passt")
        if score >= 20:
            amount = min(money, target - (offer if skonto else 0))
            result.append(Suggestion(doc, score, amount, skonto, reasons))
    def distance(sug: Suggestion) -> int:
        """Gleich gute Vorschläge (z. B. mehrere Rechnungen mit gleichem Betrag): die zeitlich nächste zuerst."""
        # schon bezahlt (Häkchen): ab Rechnungsdatum, sonst um die Fälligkeit herum
        day = sug.doc.doc_date if sug.doc.status == STATUS_PAID else (sug.doc.due_date or sug.doc.doc_date)
        return abs((tx.booking_date - day).days) if day and tx.booking_date else 99999

    result.sort(key=lambda s: (-s.score, distance(s)))
    return result[:5]


def best(db: Session, tx: BankTransaction, docs: list[Document] | None = None) -> Suggestion | None:
    found = suggestions(db, tx, docs)
    return found[0] if found and found[0].score >= SUGGEST_SCORE else None


# ================================================================ Zuordnen
def _refresh_status(tx: BankTransaction) -> None:
    tx.status = TX_DONE if (tx.category or tx.remaining <= 0) else TX_OPEN


def assign(db: Session, tx: BankTransaction, doc: Document, *, amount: Decimal | None, skonto: bool,
           user_id: int, ip: str | None) -> str | None:
    """Umsatz (ganz oder teilweise) einer Rechnung zuordnen = Zahlung erfassen. Fehlermeldung oder None."""
    if tx.category:
        return "Dieser Umsatz ist bereits als „" + tx.category_label + "“ markiert."
    expected_in = doc.direction == DIRECTION_OUT or doc.kind == "gutschrift"
    if (tx.amount > 0) != expected_in:
        return "Geldeingänge passen nur zu Ausgangsrechnungen, Geldausgänge nur zu Eingangsrechnungen."
    amount = tx.remaining if amount is None else amount
    if amount <= 0 or amount > tx.remaining:
        return f"Höchstens der noch freie Betrag des Umsatzes ({format_money(tx.remaining)})."
    replaced = unbacked_payments(doc)
    if replaced:
        if amount != _target(doc):
            return ("Die Rechnung ist schon bezahlt (ohne Bankbeleg). Ein Umsatz ersetzt diese Zahlung nur über den "
                    "ganzen Betrag.")
        for payment in replaced:   # Häkchen-/Handzahlung durch die Bankzahlung mit echtem Datum ersetzen
            belege.delete_payment(db, doc, payment.id, user_id=user_id, ip=ip)
    offer = belege.skonto_offer(doc) if skonto else None
    skonto = bool(offer)
    amount = min(amount, doc.open_amount - (offer or 0))
    note = f"Bankabgleich: {tx.name or ''} {format_date(tx.booking_date)}".strip()[:300]
    form = {"paid_on": format_date(tx.booking_date), "amount": format_decimal(amount), "note": note,
            "mit_skonto": "1" if skonto else ""}
    errors = belege.add_payment(db, doc, form, user_id=user_id, ip=ip, bank_transaction_id=tx.id)
    if errors:
        return " ".join(errors.values())
    db.refresh(tx)
    _refresh_status(tx)
    _log(db, "zugeordnet", tx.id, tx.tenant_id, user_id, ip,
         {"beleg": doc.number or doc.id, "betrag": str(amount), "skonto": skonto})
    return None


def assign_sure(db: Session, *, user_id: int, ip: str | None) -> int:
    """Alle sicheren Vorschläge (Rechnungsnummer + Betrag) übernehmen. Gibt die Anzahl zurück."""
    done = 0
    for tx in transactions(db, show="offen"):
        suggestion = best(db, tx)
        if suggestion is not None and suggestion.sure and suggestion.amount == tx.remaining and                 all(p.note == belege.CHECK_NOTE for p in unbacked_payments(suggestion.doc)):
            if assign(db, tx, suggestion.doc, amount=suggestion.amount, skonto=suggestion.skonto,
                      user_id=user_id, ip=ip) is None:
                done += 1
    return done


def set_category(db: Session, tx: BankTransaction, category: str, *, user_id: int, ip: str | None) -> str | None:
    if category not in CATEGORIES:
        return "Unbekannte Kategorie."
    if tx.payments:
        return "Der Umsatz ist schon Rechnungen zugeordnet – bitte erst diese Zuordnung aufheben."
    tx.category = category
    _refresh_status(tx)
    _log(db, "markiert", tx.id, tx.tenant_id, user_id, ip, {"kategorie": CATEGORIES[category]})
    return None


def reset(db: Session, tx: BankTransaction, *, user_id: int, ip: str | None) -> None:
    """Markierung bzw. Zuordnungen aufheben (die zugehörigen Zahlungen werden gelöscht)."""
    if tx.category:
        _log(db, "markierung_aufgehoben", tx.id, tx.tenant_id, user_id, ip, {"kategorie": tx.category_label})
        tx.category = None
    for payment in list(tx.payments):
        doc = belege.get(db, payment.document_id)
        belege.delete_payment(db, doc, payment.id, user_id=user_id, ip=ip)
    db.flush()
    db.refresh(tx)
    _refresh_status(tx)
    _log(db, "zuordnung_aufgehoben", tx.id, tx.tenant_id, user_id, ip)

