"""Automatische Auslese von Rechnungsdaten aus dem Text einer PDF.

Reihenfolge laut Konzept: E-Rechnung-XML (exakt, folgt in Schritt 8) ->
PDF-Textebene (hier) -> OCR für Scans (folgt in Schritt 9).

Rechnungen sind sehr unterschiedlich aufgebaut. Die Auslese ist deshalb ein
„bester Versuch“: Jedes Feld, das nicht sicher erkannt wurde, landet in
``uncertain`` und wird in der Oberfläche mit „bitte prüfen“ markiert.
Alle Beträge sind Decimal.
"""
import io
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from app.services.betraege import AmountError, TaxLine, complete_line, totals

MAX_PAGES = 10

# ---------------------------------------------------------------- Muster
_MONEY_RE = re.compile(r"(?<![\d,.])-?(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2}(?!\d)")
_RATE_RE = re.compile(r"(?<![\d,.])(\d{1,2}(?:,\d{1,2})?)\s*%")
_VAT_LABEL = re.compile(r"mwst|mehrwertsteuer|ust\b|ust\.|umsatzsteuer|vat\b|steuer\s*\d", re.I)
_DATE_NUM_RE = re.compile(r"(?<!\d)(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4}|\d{2})(?!\d)")
_MONTHS = {"januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6, "juli": 7,
           "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12}
_DATE_WORD_RE = re.compile(r"(?<!\d)(\d{1,2})\.\s*(" + "|".join(_MONTHS) + r")\s+(\d{4})", re.I)

GROSS_LABELS = [r"gesamtbetrag", r"rechnungsbetrag", r"endbetrag", r"zu\s*zahlen(?:der\s*betrag)?",
                r"gesamtsumme", r"summe\s*brutto", r"brutto\s*gesamt", r"gesamt\s*brutto", r"bruttobetrag",
                r"rechnungssumme", r"amount\s*due", r"total"]
NET_LABELS = [r"nettobetrag", r"summe\s*netto", r"netto\s*gesamt", r"gesamt\s*netto", r"nettosumme",
              r"zwischensumme", r"warenwert", r"netto"]
DATE_LABELS = [r"rechnungsdatum", r"belegdatum", r"ausstellungsdatum", r"invoice\s*date", r"datum"]
NUMBER_LABELS = [r"rechnungs?[\s-]*(?:nummer|nr\.?)", r"rechnung\s+nr\.?", r"rg\.?[\s-]*nr\.?",
                 r"beleg[\s-]*(?:nummer|nr\.?)", r"invoice\s*(?:no\.?|number|#)",
                 r"(?m)^\s*rechnung(?=\s)"]  # „Rechnung 17“ am Zeilenanfang
_NOT_DOC_DATE = re.compile(r"leistung|liefer|fällig|faellig|zahlbar|skonto|bis\b|zeitraum|geburt", re.I)
_NO_VAT = re.compile(r"§\s*19\s*ustg|kleinunternehmer|umsatzsteuerfrei|keine\s+(?:umsatzsteuer|mwst)|"
                     r"ohne\s+(?:mwst|ust)|nicht\s+umsatzsteuerpflichtig", re.I)
_REVERSE_CHARGE = re.compile(r"§\s*13b|steuerschuldnerschaft\s+des\s+leistungsempf|reverse\s*charge", re.I)
_LEGAL_FORM = re.compile(r"\b(gmbh|ag|ug|kg|ohg|gbr|mbh|e\.\s?k\.|e\.\s?v\.|inh\.|inhaber)\b", re.I)
_SKIP_PARTNER = re.compile(r"rechnung|invoice|seite|page|datum|nr\.|kunden|lieferschein|angebot|"
                           r"telefon|tel\.|fax|e-mail|www\.|iban|bic|ust|steuer|bank", re.I)


@dataclass
class Partner:
    """Bekannter Lieferant für die Wiedererkennung."""

    id: int
    name: str
    ust_id: str | None = None
    iban: str | None = None


@dataclass
class OwnCompany:
    """Eigene Firma – darf nicht als Absender erkannt werden (steht als Empfänger auf der Rechnung)."""

    name: str = ""
    ust_id: str | None = None
    iban: str | None = None


@dataclass
class Result:
    text: str = ""
    number: str | None = None
    doc_date: date | None = None
    due_date: date | None = None
    partner_name: str | None = None
    supplier_id: int | None = None
    gross: Decimal | None = None
    lines: list[TaxLine] = field(default_factory=list)
    skonto_percent: Decimal | None = None
    skonto_until: date | None = None
    hints: list[str] = field(default_factory=list)
    uncertain: set[str] = field(default_factory=set)


# ------------------------------------------------------------ Textebene
def pdf_text(data: bytes) -> str:
    """Text aller Seiten (höchstens MAX_PAGES). Leerer Text = Scan ohne Textebene."""
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages[:MAX_PAGES])
    except Exception:  # noqa: BLE001 – kaputte PDFs dürfen den Upload nicht verhindern
        return ""


# ---------------------------------------------------------------- Hilfen
def _money(token: str) -> Decimal:
    return Decimal(token.replace(".", "").replace(",", "."))


def _money_tokens(text: str) -> list[Decimal]:
    return [_money(t) for t in _MONEY_RE.findall(text)]


def _rate(token: str) -> Decimal:
    return Decimal(token.replace(",", "."))


def _dates_in(text: str) -> list[date]:
    found: list[tuple[int, date]] = []
    for m in _DATE_NUM_RE.finditer(text):
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year = year + 2000 if year < 100 else year
        try:
            found.append((m.start(), date(year, month, day)))
        except ValueError:
            continue
    for m in _DATE_WORD_RE.finditer(text):
        try:
            found.append((m.start(), date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))))
        except ValueError:
            continue
    return [d for _, d in sorted(found, key=lambda x: x[0])]


def _plausible(d: date) -> bool:
    return 2000 <= d.year <= date.today().year + 1


def _amount_after_label(lines: list[str], labels: list[str], skip_percent: bool = True) -> Decimal | None:
    for label in labels:
        pattern = re.compile(label, re.I)
        for i, line in enumerate(lines):
            m = pattern.search(line)
            if not m:
                continue
            if skip_percent and "%" in line:
                continue
            tokens = _money_tokens(line[m.end():])
            if tokens:
                return tokens[-1]
            if i + 1 < len(lines):
                tokens = _money_tokens(lines[i + 1])
                if tokens and "%" not in lines[i + 1]:
                    return tokens[0]
    return None


# ------------------------------------------------------------- Einzelfelder
def find_number(text: str) -> str | None:
    for label in NUMBER_LABELS:
        for m in re.finditer(label + r"[ \t:.#]*\n?[ \t]*([A-Za-z0-9][A-Za-z0-9\-/._]{0,30})", text, re.I):
            candidate = m.group(1).rstrip("./-_")
            if re.search(r"\d", candidate):
                return candidate
    return None


def find_doc_date(lines: list[str]) -> tuple[date | None, bool]:
    for label in DATE_LABELS:
        pattern = re.compile(label, re.I)
        for i, line in enumerate(lines):
            m = pattern.search(line)
            if not m:
                continue
            if label == r"datum" and _NOT_DOC_DATE.search(line):
                continue
            candidates = _dates_in(line[m.end():]) or (_dates_in(lines[i + 1]) if i + 1 < len(lines) else [])
            candidates = [d for d in candidates if _plausible(d)]
            if candidates:
                return candidates[0], True
    for line in lines:  # Ersatz: erstes plausibles Datum außerhalb von Fälligkeits-/Leistungszeilen
        if _NOT_DOC_DATE.search(line):
            continue
        candidates = [d for d in _dates_in(line) if _plausible(d)]
        if candidates:
            return candidates[0], False
    return None, False


def find_due_date(lines: list[str], doc_date: date | None) -> date | None:
    for line in lines:
        if re.search(r"fällig|faellig|zahlbar\s+bis|zahlungsziel|due\s*date", line, re.I) and \
                not re.search(r"skonto", line, re.I):
            dates = _dates_in(line)
            if dates:
                return dates[-1]
    if doc_date is None:
        return None
    for line in lines:
        m = re.search(r"(\d{1,3})\s+tage?n?\s+(?:rein\s+)?netto", line, re.I)
        if m:
            return doc_date + timedelta(days=int(m.group(1)))
    for line in lines:
        if re.search(r"skonto", line, re.I):
            continue
        m = re.search(r"(?:innerhalb|binnen)\s+(?:von\s+)?(\d{1,3})\s+tag", line, re.I)
        if m:
            return doc_date + timedelta(days=int(m.group(1)))
        if re.search(r"sofort\s+(?:zahlbar|fällig)|zahlbar\s+sofort", line, re.I):
            return doc_date
    return None


def find_skonto(lines: list[str], doc_date: date | None) -> tuple[Decimal | None, date | None]:
    for line in lines:
        if not re.search(r"skonto", line, re.I):
            continue
        rate = re.search(r"(\d{1,2}(?:,\d{1,2})?)\s*%\s*skonto", line, re.I) or _RATE_RE.search(line)
        if not rate:
            continue
        percent = _rate(rate.group(1))
        until = None
        m = re.search(r"(?:innerhalb|binnen)\s+(?:von\s+)?(\d{1,3})\s+tag", line, re.I) or \
            re.search(r"(\d{1,3})\s+tage?n?", line, re.I)
        dates = _dates_in(line)
        if dates:
            until = dates[-1]
        elif m and doc_date:
            until = doc_date + timedelta(days=int(m.group(1)))
        return percent, until
    return None, None


def _vat_entries(lines: list[str]) -> list[tuple[Decimal, Decimal | None, Decimal | None]]:
    """(Satz, Netto-Basis oder None, USt oder None) je gefundenem Steuersatz."""
    entries: dict[Decimal, tuple[Decimal, Decimal | None, Decimal | None]] = {}
    for line in lines:
        if not _VAT_LABEL.search(line):
            continue
        rate_match = _RATE_RE.search(line)
        if not rate_match:
            continue
        rate = _rate(rate_match.group(1))
        if rate in entries or rate >= 100:
            continue
        rest = line[:rate_match.start()] + " " + line[rate_match.end():]
        tokens = _money_tokens(rest)
        base = vat = None
        factor = rate / 100
        if len(tokens) >= 3 and tokens[0] + tokens[1] == tokens[2]:
            base, vat = tokens[0], tokens[1]
        elif len(tokens) >= 2:
            if abs(tokens[0] * factor - tokens[-1]) <= Decimal("0.05"):
                base, vat = tokens[0], tokens[-1]
            else:
                vat = tokens[-1]
        elif tokens:
            vat = tokens[0]
        entries[rate] = (rate, base, vat)
    return list(entries.values())


def _nearest_rate(net: Decimal, vat: Decimal) -> Decimal | None:
    if net == 0:
        return None
    for rate in (Decimal(19), Decimal(7), Decimal(0)):
        if abs(net * rate / 100 - vat) <= Decimal("0.05"):
            return rate
    return None


def find_tax_lines(text: str, lines: list[str], gross: Decimal | None, net: Decimal | None,
                   result: Result) -> list[TaxLine]:
    entries = _vat_entries(lines)
    tax_lines: list[TaxLine] = []

    if not entries and _REVERSE_CHARGE.search(text):
        result.hints.append("Reverse-Charge (§ 13b UStG): Die Umsatzsteuer schulden Sie selbst – "
                            "§ 13b ist unten vorausgewählt, bitte den Fall prüfen.")
        result.uncertain.add("lines")
        amount = gross if gross is not None else net
        return [complete_line(Decimal(0), amount, None, None)] if amount is not None else []
    if not entries and _NO_VAT.search(text):
        amount = gross if gross is not None else net
        return [complete_line(Decimal(0), amount, None, None)] if amount is not None else []

    if len(entries) == 1:
        rate, base, vat = entries[0]
        for attempt in ((base or net, vat, gross), (base or net, vat, None), (None, vat, gross), (None, None, gross)):
            if all(x is None for x in attempt):
                continue
            try:
                tax_lines = [complete_line(rate, *attempt)]
                break
            except AmountError:
                continue
        if not tax_lines or (gross is not None and tax_lines[0].gross != gross):
            result.uncertain.add("lines")
    elif entries:
        for rate, base, vat in entries:
            try:
                tax_lines.append(complete_line(rate, base, vat, None))
            except AmountError:
                result.uncertain.add("lines")
        if gross is not None and totals(tax_lines)[2] != gross:
            result.uncertain.add("lines")
    elif gross is not None and net is not None:
        rate = _nearest_rate(net, gross - net)
        if rate is not None:
            tax_lines = [complete_line(rate, net, gross - net, gross)]
        else:
            result.uncertain.add("lines")
    elif gross is not None:
        tax_lines = [complete_line(Decimal(19), None, None, gross)]  # Annahme: Regelsteuersatz
        result.uncertain.add("lines")
    else:
        result.uncertain.add("lines")
    return tax_lines


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", "", text or "").upper()


def find_partner(text: str, lines: list[str], partners: list[Partner], own: OwnCompany) -> tuple[
        str | None, int | None, bool]:
    flat = _norm(text)
    for p in partners:
        for key in (p.ust_id, p.iban):
            if key and len(_norm(key)) >= 8 and _norm(key) in flat and _norm(key) not in (
                    _norm(own.ust_id), _norm(own.iban)):
                return p.name, p.id, True
    folded = text.casefold()
    for p in partners:
        if len(p.name) >= 4 and p.name.casefold() in folded and p.name.casefold() != own.name.casefold():
            return p.name, p.id, True

    own_name = own.name.casefold().strip()
    candidates = []
    for line in lines[:25]:
        part = re.split(r"\s[·•|]\s|\s-\s|,\s", line.strip())[0].strip()
        if len(part) < 3 or _SKIP_PARTNER.search(part) or _MONEY_RE.search(part) or _dates_in(part):
            continue
        if own_name and own_name in part.casefold():
            continue
        if re.fullmatch(r"[\d\s\-/]+", part) or re.match(r"^\d{5}\s", part):
            continue
        candidates.append(part[:200])
    for part in candidates:
        if _LEGAL_FORM.search(part):
            return part, None, False
    return (candidates[0], None, False) if candidates else (None, None, False)


# ------------------------------------------------------------------ Gesamt
def parse(text: str, partners: list[Partner] | None = None, own: OwnCompany | None = None) -> Result:
    result = Result(text=text)
    if not text.strip():
        result.uncertain.update({"number", "doc_date", "partner", "lines"})
        result.hints.append("Die Datei enthält keinen lesbaren Text (Scan oder Foto). "
                            "Bitte die Angaben von Hand eintragen.")
        return result
    lines = [line for line in text.splitlines() if line.strip()]

    result.number = find_number(text)
    if not result.number:
        result.uncertain.add("number")

    result.doc_date, sure = find_doc_date(lines)
    if not sure:
        result.uncertain.add("doc_date")
    result.due_date = find_due_date(lines, result.doc_date)
    result.skonto_percent, result.skonto_until = find_skonto(lines, result.doc_date)

    gross = _amount_after_label(lines, GROSS_LABELS)
    if gross is None:
        tokens = _money_tokens(text)
        gross = max(tokens) if tokens else None  # Ersatz: größter Betrag ist oft die Endsumme
        result.uncertain.add("gross")
    net = _amount_after_label(lines, NET_LABELS)
    result.lines = find_tax_lines(text, lines, gross, net, result)
    result.gross = totals(result.lines)[2] if result.lines else gross

    result.partner_name, result.supplier_id, sure = find_partner(text, lines, partners or [], own or OwnCompany())
    if not sure:
        result.uncertain.add("partner")
    return result
