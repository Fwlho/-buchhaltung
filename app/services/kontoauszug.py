"""Kontoauszüge einlesen: CSV (gängige Banken, Spalten-Zuordnung) und CAMT.053 (XML, auch als ZIP).

Reine Funktionen ohne Datenbank – das Speichern und Zuordnen erledigt app/services/bank.py.
Beträge sind Decimal: positiv = Geldeingang, negativ = Ausgang.
"""
import csv
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

MAX_ROWS = 20000
MAX_ZIP_FILES = 50
MAX_ZIP_MEMBER = 20 * 1024 * 1024


class StatementError(ValueError):
    pass


@dataclass
class RawTx:
    booking_date: date
    amount: Decimal
    name: str = ""
    iban: str = ""
    purpose: str = ""
    value_date: date | None = None


def detect_format(data: bytes) -> str:
    """„camt“ (XML oder ZIP mit XML) bzw. „csv“."""
    if data.startswith(b"PK\x03\x04"):
        return "camt"
    head = data[:300].lstrip(b"\xef\xbb\xbf").lstrip()
    return "camt" if head.startswith(b"<") else "csv"


def fingerprints(rows: list[RawTx]) -> list[str]:
    """Erkennungsmerkmal je Umsatz. Gleiche Umsätze in einer Datei werden durchgezählt, damit zwei
    echte, gleiche Buchungen nicht als Duplikat gelten – überlappende Auszüge aber schon."""
    seen: dict[str, int] = {}
    result = []
    for r in rows:
        base = "|".join([r.booking_date.isoformat(), f"{r.amount:.2f}", r.name.strip().lower(),
                         r.iban.replace(" ", "").upper(), re.sub(r"\s+", " ", r.purpose).strip().lower()])
        seen[base] = seen.get(base, 0) + 1
        result.append(hashlib.sha256(f"{base}|#{seen[base]}".encode()).hexdigest())
    return result


# ====================================================================== CSV
CSV_FIELDS: list[tuple[str, str]] = [
    ("datum", "Buchungsdatum (Pflicht)"),
    ("betrag", "Betrag (+/−)"),
    ("soll", "oder: Betrag Ausgang (Soll)"),
    ("haben", "und: Betrag Eingang (Haben)"),
    ("name", "Auftraggeber / Empfänger"),
    ("iban", "IBAN"),
    ("zweck", "Verwendungszweck"),
    ("valuta", "Wertstellung (optional)"),
]
_GUESS: dict[str, tuple[str, ...]] = {
    # Reihenfolge = Vorrang; Vergleich mit kleingeschriebenem Spaltennamen ohne Umlaute
    "datum": ("buchungstag", "buchungsdatum", "buchung", "datum", "date"),
    "valuta": ("valuta", "wertstellung", "wertst"),
    "betrag": ("betrag (eur)", "betrag", "umsatz", "amount"),
    "soll": ("soll", "ausgang", "belastung"),
    "haben": ("haben", "eingang", "gutschrift"),
    "name": ("beguenstigter/zahlungspflichtiger", "zahlungspflichtige", "beguenstigter", "auftraggeber",
             "empfaenger", "name", "gegenkonto inhaber"),
    "iban": ("kontonummer/iban", "iban", "kontonummer"),
    "zweck": ("verwendungszweck", "buchungstext", "zweck", "beschreibung", "purpose"),
}


def decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _fold(text: str) -> str:
    text = (text or "").strip().lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(a, b)
    return text


def csv_rows(data: bytes) -> list[list[str]]:
    text = decode(data)
    sample = "\n".join(text.splitlines()[:30])
    delimiter = max((";", ",", "\t"), key=sample.count)
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if len(rows) > MAX_ROWS:
        raise StatementError(f"Die Datei hat mehr als {MAX_ROWS} Zeilen.")
    return [[cell.strip() for cell in row] for row in rows]


def header_index(rows: list[list[str]]) -> int:
    """Zeile mit den Spaltennamen (manche Banken schreiben vorher Kontodaten in die Datei)."""
    for i, row in enumerate(rows[:40]):
        cells = [_fold(c) for c in row if c.strip()]
        if len(cells) < 3:
            continue
        has_date = any(any(k in c for k in ("buchung", "datum", "date", "valuta")) for c in cells)
        has_amount = any(any(k in c for k in ("betrag", "umsatz", "amount", "soll", "haben")) for c in cells)
        if has_date and has_amount:
            return i
    return 0


def guess_mapping(header: list[str]) -> dict[str, str]:
    """Vorschlag Feld -> Spaltenname."""
    folded = [_fold(h) for h in header]
    mapping: dict[str, str] = {}
    used: set[int] = set()
    for key, words in _GUESS.items():
        for word in words:
            idx = next((i for i, h in enumerate(folded) if i not in used and (h == word or h.startswith(word))), None)
            if idx is None:
                idx = next((i for i, h in enumerate(folded) if i not in used and word in h), None)
            if idx is not None:
                mapping[key] = header[idx]
                used.add(idx)
                break
    if "betrag" in mapping:  # ein Betragsfeld genügt; Soll/Haben nur, wenn es keins gibt
        mapping.pop("soll", None)
        mapping.pop("haben", None)
    return mapping


def mapping_problem(mapping: dict[str, str]) -> str | None:
    if not mapping.get("datum"):
        return "Bitte die Spalte mit dem Buchungsdatum auswählen."
    if not mapping.get("betrag") and not (mapping.get("soll") or mapping.get("haben")):
        return "Bitte die Betrags-Spalte auswählen (oder die Spalten für Soll und Haben)."
    return None


def parse_date(text: str) -> date | None:
    text = (text or "").strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_amount(text: str) -> Decimal | None:
    """Deutsches Format (1.234,56 / -1.234,56 / 1234,56 S) und englisches (1234.56)."""
    text = (text or "").replace("EUR", "").replace("€", "").replace("\xa0", "").replace(" ", "").strip()
    if not text:
        return None
    sign = 1
    if text[-1] in "SH" and len(text) > 1:  # Soll/Haben-Kennzeichen hinter dem Betrag
        sign = -1 if text[-1] == "S" else 1
        text = text[:-1]
    if text.startswith("+"):
        text = text[1:]
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif text.count(".") > 1:
        text = text.replace(".", "")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return (sign * value).quantize(Decimal("0.01"))


def parse_csv(data: bytes, mapping: dict[str, str]) -> tuple[list[RawTx], int]:
    """Umsätze aus der CSV. Gibt (Umsätze, übersprungene Zeilen) zurück."""
    rows = csv_rows(data)
    start = header_index(rows)
    header = rows[start] if rows else []
    pos = {key: header.index(col) for key, col in mapping.items() if col and col in header}

    def cell(row, key):
        i = pos.get(key)
        return row[i] if i is not None and i < len(row) else ""

    result, skipped = [], 0
    for row in rows[start + 1:]:
        if not any(row):
            continue
        booking = parse_date(cell(row, "datum"))
        if "betrag" in pos:
            amount = parse_amount(cell(row, "betrag"))
        else:
            debit, credit = parse_amount(cell(row, "soll")), parse_amount(cell(row, "haben"))
            amount = None if debit is None and credit is None else (credit or 0) - abs(debit or 0)
        if booking is None or amount is None:
            skipped += 1  # z. B. Summen- oder Kontostandszeilen
            continue
        result.append(RawTx(booking_date=booking, amount=Decimal(amount).quantize(Decimal("0.01")),
                            name=cell(row, "name")[:200], iban=cell(row, "iban").replace(" ", "")[:40],
                            purpose=cell(row, "zweck")[:2000], value_date=parse_date(cell(row, "valuta"))))
    return result, skipped


# ===================================================================== CAMT
def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(el, *names):
    """Kind-Elemente entlang der lokalen Namen (Namensräume der CAMT-Versionen egal)."""
    for name in names:
        if el is None:
            return None
        el = next((c for c in el if _local(c.tag) == name), None)
    return el


def _text(el, *names) -> str:
    found = _child(el, *names)
    return (found.text or "").strip() if found is not None and found.text else ""


def _first_desc(el, name: str) -> str:
    if el is None:
        return ""
    found = next((d for d in el.iter() if _local(d.tag) == name and d.text and d.text.strip()), None)
    return found.text.strip() if found is not None else ""


def _camt_date(el) -> date | None:
    if el is None:
        return None
    raw = _text(el, "Dt") or _text(el, "DtTm")[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _parse_camt_xml(data: bytes) -> list[RawTx]:
    from defusedxml import ElementTree

    try:
        root = ElementTree.fromstring(data)
    except Exception as exc:  # noqa: BLE001 – defusedxml/ParseError
        raise StatementError("Die XML-Datei ist kein lesbarer Kontoauszug (CAMT).") from exc
    if not any(_local(el.tag) in ("BkToCstmrStmt", "BkToCstmrAcctRpt", "BkToCstmrDbtCdtNtfctn")
               for el in root.iter()):
        raise StatementError("Die XML-Datei ist kein CAMT-Kontoauszug (camt.052/053/054).")
    result = []
    for entry in (el for el in root.iter() if _local(el.tag) == "Ntry"):
        status = _text(entry, "Sts") or _text(entry, "Sts", "Cd")
        if status.upper() in ("PDNG", "INFO"):
            continue  # vorgemerkte, noch nicht gebuchte Umsätze
        try:
            amount = Decimal(_text(entry, "Amt"))
        except InvalidOperation:
            continue
        if _text(entry, "CdtDbtInd") == "DBIT":
            amount = -amount
        booking = _camt_date(_child(entry, "BookgDt")) or _camt_date(_child(entry, "ValDt"))
        if booking is None:
            continue
        details = _child(entry, "NtryDtls", "TxDtls")
        parties = _child(details, "RltdPties")
        side = "Dbtr" if amount > 0 else "Cdtr"
        name = _first_desc(_child(parties, side), "Nm") or _first_desc(_child(parties, "Ultmt" + side), "Nm")
        iban = _first_desc(_child(parties, side + "Acct"), "IBAN")
        purposes = [d.text.strip() for d in entry.iter() if _local(d.tag) == "Ustrd" and d.text and d.text.strip()]
        purpose = " ".join(purposes) or _text(entry, "AddtlNtryInf")
        result.append(RawTx(booking_date=booking, amount=amount.quantize(Decimal("0.01")), name=name[:200],
                            iban=iban[:40], purpose=purpose[:2000], value_date=_camt_date(_child(entry, "ValDt"))))
    return result


def parse_camt(data: bytes) -> list[RawTx]:
    """CAMT-XML oder ZIP-Archiv mit mehreren CAMT-Dateien (so liefern es viele Banken)."""
    if not data.startswith(b"PK\x03\x04"):
        return _parse_camt_xml(data)
    result = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            members = [m for m in zf.infolist() if m.filename.lower().endswith(".xml") and not m.is_dir()]
            if not members:
                raise StatementError("Im ZIP-Archiv ist keine XML-Datei (CAMT).")
            for member in members[:MAX_ZIP_FILES]:
                if member.file_size > MAX_ZIP_MEMBER:
                    raise StatementError(f"„{member.filename}“ ist zu groß.")
                result += _parse_camt_xml(zf.read(member))
    except zipfile.BadZipFile as exc:
        raise StatementError("Das ZIP-Archiv ist beschädigt.") from exc
    return result
