"""Nummernformate für Rechnungen, Angebote, Mahnungen, Stornos, Aufträge.

Platzhalter im Format:
  {JJJJ} Jahr vierstellig, {JJ} Jahr zweistellig, {MM} Monat,
  {N…}   laufende Nummer, so viele Stellen wie N (z. B. {NNN} -> 007)

Vergabe (``allocate``): lückenlos je Firma, Belegart und Jahr über die Tabelle
number_ranges. Die Nummer wird in derselben Transaktion wie das Festschreiben
vergeben – schlägt etwas fehl, wird alles zurückgerollt und es entsteht keine Lücke.
"""
import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

_COUNTER_RE = re.compile(r"\{(N+)\}")
_ALLOWED_RE = re.compile(r"\{(JJJJ|JJ|MM|N+)\}")

NUMBER_TYPES: list[tuple[str, str, str]] = [
    # (Schlüssel, Bezeichnung, Standardformat)
    ("rechnung", "Rechnung", "{JJJJ}-{NNN}"),
    ("angebot", "Angebot", "A-{JJJJ}-{NNN}"),
    ("mahnung", "Mahnung", "M-{JJJJ}-{NNN}"),
    ("storno", "Stornorechnung", "S-{JJJJ}-{NNN}"),
    ("auftrag", "Auftrag", "AU-{JJJJ}-{NNN}"),
]


def format_problem(fmt: str) -> str | None:
    """Fehlermeldung, wenn das Format nicht taugt, sonst None."""
    fmt = (fmt or "").strip()
    if not fmt:
        return "Bitte ein Format angeben, z. B. {JJJJ}-{NNN}."
    if len(_COUNTER_RE.findall(fmt)) != 1:
        return "Das Format braucht genau einen Platzhalter für die laufende Nummer, z. B. {NNN}."
    leftover = _ALLOWED_RE.sub("", fmt)
    if "{" in leftover or "}" in leftover:
        return "Unbekannter Platzhalter. Erlaubt sind {JJJJ}, {JJ}, {MM} und {NNN}."
    if not re.fullmatch(r"[A-Za-z0-9\-_/.]*", leftover):
        return "Außer Platzhaltern sind nur Buchstaben, Ziffern und - _ / . erlaubt."
    return None


def format_number(fmt: str, year: int, month: int, counter: int) -> str:
    def counter_repl(m: re.Match) -> str:
        return str(counter).zfill(len(m.group(1)))

    text = _COUNTER_RE.sub(counter_repl, fmt)
    return (
        text.replace("{JJJJ}", f"{year:04d}")
        .replace("{JJ}", f"{year % 100:02d}")
        .replace("{MM}", f"{month:02d}")
    )


def allocate(db: Session, kind: str, on: date) -> str:
    """Nächste Nummer für ``kind`` (rechnung, storno, angebot, mahnung) im Jahr von ``on``.
    Sperrt die Zeile (PostgreSQL: SELECT … FOR UPDATE; SQLite sperrt beim Schreiben ohnehin)."""
    from app.models import NumberRange
    from app.services import einstellungen

    values = einstellungen.load(db)
    fmt = values[f"nummern.{kind}.format"]
    start = int(values[f"nummern.{kind}.start"] or 1)
    row = db.scalar(select(NumberRange).where(NumberRange.kind == kind, NumberRange.year == on.year)
                    .with_for_update())
    if row is None:
        row = NumberRange(kind=kind, year=on.year, last_number=start - 1)
        db.add(row)
    # Nummern, die es schon gibt (z. B. hochgeladene alte Rechnungen wie 2026-100), werden übersprungen
    for _ in range(100_000):
        row.last_number = max(row.last_number + 1, start)
        number = format_number(fmt, on.year, on.month, row.last_number)
        if not _taken(db, kind, number):
            break
    db.flush()
    return number


def _taken(db: Session, kind: str, number: str) -> bool:
    """Ist die Nummer in der aktiven Firma schon vergeben (auch im Prüfbereich)?"""
    from sqlalchemy import func

    from app.models import Document, Dunning, Order, Quote
    from app.models.belege import DIRECTION_OUT

    if kind == "angebot":
        stmt = select(func.count()).select_from(Quote).where(Quote.number == number)
    elif kind == "auftrag":
        stmt = select(func.count()).select_from(Order).where(Order.number == number)
    elif kind == "mahnung":
        stmt = select(func.count()).select_from(Dunning).where(Dunning.number == number)
    else:
        stmt = (select(func.count()).select_from(Document)
                .where(Document.direction == DIRECTION_OUT, Document.number == number)
                .execution_options(include_pending=True))
    return bool(db.scalar(stmt))


def counter_of(fmt: str, number: str, year: int) -> int | None:
    """Laufende Nummer aus einer vorhandenen Nummer lesen – nur wenn sie zum Format und zum Jahr passt."""
    pattern, pos, used = "", 0, set()
    for m in _ALLOWED_RE.finditer(fmt):
        pattern += re.escape(fmt[pos:m.start()])
        token = m.group(1)
        name = {"JJJJ": "y4", "JJ": "y2", "MM": "mm"}.get(token, "n")
        part = {"JJJJ": r"\d{4}", "JJ": r"\d{2}", "MM": r"(?:0[1-9]|1[0-2])"}.get(token, r"\d+")
        pattern += f"(?P<{name}>{part})" if name not in used else f"(?:{part})"
        used.add(name)
        pos = m.end()
    pattern += re.escape(fmt[pos:])
    m = re.fullmatch(pattern, (number or "").strip())
    if m is None or "n" not in used:
        return None
    found = m.groupdict()
    if found.get("y4") and int(found["y4"]) != year:
        return None
    if found.get("y2") and int(found["y2"]) != year % 100:
        return None
    return int(found["n"])


def register_existing(db: Session, kind: str, number: str, on: date) -> int | None:
    """Vorhandene Nummer (z. B. einer hochgeladenen alten Rechnung) im Zähler berücksichtigen, damit
    sie für neue Belege nicht noch einmal vergeben wird. Gibt die erkannte laufende Nummer zurück."""
    from app.models import NumberRange
    from app.services import einstellungen

    counter = counter_of(einstellungen.load(db)[f"nummern.{kind}.format"], number, on.year)
    if counter is None:
        return None
    row = db.scalar(select(NumberRange).where(NumberRange.kind == kind, NumberRange.year == on.year)
                    .with_for_update())
    if row is None:
        db.add(NumberRange(kind=kind, year=on.year, last_number=counter))
    else:
        row.last_number = max(row.last_number, counter)
    db.flush()
    return counter
