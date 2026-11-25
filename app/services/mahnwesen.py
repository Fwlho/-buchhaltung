"""Mahnwesen (KONZEPT.md 6.12): überfällige Ausgangsrechnungen, Mahnstufen, Mahnung als PDF.

- Stufen, Fristen, Gebühren und Texte kommen aus den Firmen-Einstellungen (Gruppe „mahnwesen“).
- Gebühren summieren sich über die Stufen (die 2. Mahnung enthält die Gebühr der 1.).
- Verzugszinsen (optional): offener Betrag × Zinssatz × Tage seit Fälligkeit / 365.
- Jede Mahnung bekommt eine Nummer (Nummernkreis „mahnung“); ihr PDF wird einmal erzeugt und
  unverändert gespeichert. Versendet wird nie automatisch – nur E-Mail-Entwurf bzw. .eml.
Alle Funktionen schreiben ins Änderungsprotokoll und rufen KEIN commit auf.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.formatting import format_date, format_money
from app.models import Document, Dunning
from app.models.belege import DIRECTION_OUT, KIND_INVOICE, STATE_FINAL, STATUS_OPEN
from app.services import ablage, audit, einstellungen, nummernkreise
from app.services.einstellungen import DUNNING_LEVELS

ZERO = Decimal("0.00")
MAX_LEVEL = len(DUNNING_LEVELS)


@dataclass(frozen=True)
class Level:
    nr: int
    name: str
    days: int
    fee: Decimal
    text: str


def _decimal(value) -> Decimal:
    if value in (None, ""):
        return ZERO
    return Decimal(str(value)).quantize(Decimal("0.01"))


def levels(db: Session) -> list[Level]:
    v = einstellungen.load(db)
    return [Level(nr, v.get(f"mahnung.stufe{nr}.name") or name, int(v.get(f"mahnung.stufe{nr}.frist") or days),
                  _decimal(v.get(f"mahnung.stufe{nr}.gebuehr")), v.get(f"mahnung.stufe{nr}.text") or text)
            for nr, name, days, _fee, text in DUNNING_LEVELS]


def dunnings_of(db: Session, doc: Document) -> list[Dunning]:
    return list(db.scalars(select(Dunning).where(Dunning.document_id == doc.id).order_by(Dunning.level)))


def get(db: Session, dunning_id: int) -> Dunning | None:
    return db.get(Dunning, dunning_id)


# ================================================================== Liste
@dataclass
class Candidate:
    doc: Document
    last: Dunning | None
    next_level: Level | None
    due_since: date            # ab wann die nächste Stufe vorgeschlagen wird
    days_overdue: int

    @property
    def ready(self) -> bool:
        return self.next_level is not None and self.due_since <= date.today()


def overdue(db: Session, today: date | None = None) -> list[Candidate]:
    """Festgeschriebene, offene Ausgangsrechnungen mit überschrittener Fälligkeit."""
    today = today or date.today()
    v = einstellungen.load(db)
    grace = int(v.get("mahnung.karenz_tage") or 0)
    lv = levels(db)
    stmt = select(Document).where(Document.direction == DIRECTION_OUT, Document.state == STATE_FINAL,
                                  Document.kind == KIND_INVOICE, Document.status == STATUS_OPEN,
                                  Document.due_date < today).order_by(Document.due_date)
    docs = [d for d in db.scalars(stmt) if d.open_amount > 0]
    last_by_doc: dict[int, Dunning] = {}
    if docs:
        rows = db.scalars(select(Dunning).where(Dunning.document_id.in_([d.id for d in docs]))
                          .order_by(Dunning.level))
        for d in rows:
            last_by_doc[d.document_id] = d
    result = []
    for doc in docs:
        last = last_by_doc.get(doc.id)
        next_nr = (last.level + 1) if last else 1
        since = (last.pay_until if last else doc.due_date) + timedelta(days=grace + (0 if last else 1))
        result.append(Candidate(doc, last, lv[next_nr - 1] if next_nr <= MAX_LEVEL else None, since,
                                (today - doc.due_date).days))
    return result


# ================================================================ Berechnung
@dataclass
class Calculation:
    level: Level
    dunning_date: date
    pay_until: date
    open_amount: Decimal
    fees: Decimal
    interest: Decimal
    interest_days: int
    rate: Decimal | None

    @property
    def total(self) -> Decimal:
        return self.open_amount + self.fees + self.interest


def calculate(db: Session, doc: Document, level: Level, on: date) -> Calculation:
    previous = [d for d in dunnings_of(db, doc) if d.level < level.nr]
    fees = (previous[-1].fees if previous else ZERO) + level.fee
    raw_rate = einstellungen.get(db, "mahnung.zinssatz")
    rate = _decimal(raw_rate) if raw_rate not in (None, "") else None
    days = max((on - doc.due_date).days, 0) if doc.due_date else 0
    interest = ZERO
    if rate:
        interest = (doc.open_amount * rate / 100 * days / 365).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return Calculation(level, on, on + timedelta(days=level.days), doc.open_amount, fees, interest, days, rate)


def placeholders(db: Session, doc: Document, calc: Calculation) -> dict[str, str]:
    return {"nummer": doc.number or "", "datum": format_date(doc.doc_date), "faellig": format_date(doc.due_date),
            "betrag": format_money(calc.open_amount), "gesamt": format_money(calc.total),
            "frist": format_date(calc.pay_until), "kunde": doc.recipient_name or "",
            "firma": einstellungen.get(db, "firma.name") or "", "stufe": calc.level.name}


# =================================================================== Anlegen
def create(db: Session, doc: Document, *, user_id: int, ip: str | None, on: date | None = None) -> tuple[
        Dunning | None, str | None]:
    """Nächste Mahnstufe erstellen (Nummer, PDF). Gibt (Mahnung, Fehlermeldung) zurück."""
    from app.services import pdf_mahnung

    on = on or date.today()
    if not (doc.direction == DIRECTION_OUT and doc.is_final and doc.kind == KIND_INVOICE):
        return None, "Nur festgeschriebene Ausgangsrechnungen können gemahnt werden."
    if doc.status != STATUS_OPEN or doc.open_amount <= 0:
        return None, "Diese Rechnung ist nicht mehr offen."
    if not doc.due_date or doc.due_date >= on:
        return None, "Die Rechnung ist noch nicht fällig."
    existing = dunnings_of(db, doc)
    nr = (existing[-1].level + 1) if existing else 1
    if nr > MAX_LEVEL:
        return None, "Die letzte Mahnstufe ist bereits erreicht – bitte ggf. ein Inkasso oder einen Mahnbescheid prüfen."
    calc = calculate(db, doc, levels(db)[nr - 1], on)
    number = nummernkreise.allocate(db, "mahnung", on)
    dunning = Dunning(document_id=doc.id, level=nr, level_name=calc.level.name, number=number, dunning_date=on,
                      pay_until=calc.pay_until, open_amount=calc.open_amount, fees=calc.fees, interest=calc.interest,
                      total=calc.total, created_by_id=user_id)
    db.add(dunning)
    db.flush()
    pdf = pdf_mahnung.render(db, doc, dunning, calc)
    stored = ablage.save_bytes(db, pdf, tenant_id=doc.tenant_id, kind="mahnung",
                               original_name=f"{calc.level.name}_{number}_zu_{doc.number}.pdf",
                               content_type="application/pdf", user_id=user_id)
    dunning.file_id = stored.id
    audit.log(db, action="mahnung", entity="ausgangsrechnung", entity_id=doc.id, tenant_id=doc.tenant_id,
              user_id=user_id, ip=ip, changes={"stufe": calc.level.name, "nummer": number,
                                               "gesamt": str(calc.total), "frist": format_date(calc.pay_until)})
    return dunning, None
