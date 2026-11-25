"""Export für die Steuerberatung (KONZEPT.md 6.13): DATEV-Buchungsstapel + Belege als ZIP.

- DATEV-Format „EXTF“ (Version 700), Buchungsstapel; Zeichensatz Windows-1252, Trennzeichen „;“.
- Nur geprüfte (Prüfbereich zählt nie mit), festgeschriebene Belege mit Belegdatum im Zeitraum.
  Stornierte Eingangsrechnungen fehlen; bei Ausgangsrechnungen stehen Rechnung und Stornorechnung drin.
- Rechnungen werden über Personenkonten gebucht: Kunde (Debitor) an Erlöskonto, Aufwandskonto an
  Lieferant (Kreditor). Vorsteuer über BU-Schlüssel (9 = 19 %, 8 = 7 %), außer bei Automatikkonten.
  Zahlungen nur auf Wunsch (Einstellung), damit nichts doppelt gebucht wird.
- Der Zeitraum muss in einem Kalenderjahr liegen (Wirtschaftsjahr = Kalenderjahr).
Das Ergebnis ist ein Vorschlag – bitte den ersten Import mit der Steuerberatung abstimmen.
"""
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.formatting import format_date, format_decimal
from app.models import Document, Payment
from app.models.belege import DIRECTION_IN, DIRECTION_OUT, KIND_STORNO, STATE_FINAL, STATUS_CANCELLED
from app.services import ablage, auswertung, einstellungen, konten, steuerschuld
from app.services.betraege import split_by_rate

ZERO = Decimal("0.00")
COLUMNS = ["Umsatz (ohne Soll/Haben-Kz)", "Soll/Haben-Kennzeichen", "WKZ Umsatz", "Kurs", "Basis-Umsatz",
           "WKZ Basis-Umsatz", "Konto", "Gegenkonto (ohne BU-Schlüssel)", "BU-Schlüssel", "Belegdatum",
           "Belegfeld 1", "Belegfeld 2", "Skonto", "Buchungstext"]
# Konten, die die Steuer selbst errechnen – dort darf kein BU-Schlüssel stehen
AUTOMATIC = {"SKR03": {"3300", "3400", "8300", "8400", "3731", "3736"},
             "SKR04": {"5300", "5400", "4300", "4400", "5731", "5736"}}
INPUT_TAX_KEYS = {Decimal(19): "9", Decimal(7): "8"}
# § 13b (Leistungsempfänger schuldet die Steuer): Umsatzsteuer und Vorsteuer in einem Schlüssel
REVERSE_CHARGE_KEYS = {Decimal(19): "94", Decimal(7): "91"}
BANK_ACCOUNT = {"SKR03": "1200", "SKR04": "1800"}
SKONTO_RECEIVED = {"SKR03": {Decimal(19): "3736", Decimal(7): "3731", Decimal(0): "3730"},
                   "SKR04": {Decimal(19): "5736", Decimal(7): "5731", Decimal(0): "5730"}}


class ExportError(ValueError):
    pass


@dataclass
class Booking:
    amount: Decimal          # immer positiv
    side: str                # S oder H (bezogen auf „Konto“)
    account: str
    contra: str
    tax_key: str
    day: date
    ref: str                 # Belegfeld 1 (Rechnungsnummer)
    text: str


@dataclass
class Export:
    start: date
    end: date
    bookings: list[Booking] = field(default_factory=list)
    docs: list[Document] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def config_problem(db: Session) -> str | None:
    v = einstellungen.load(db)
    if not v.get("datev.berater") or not v.get("datev.mandant"):
        return "Bitte zuerst Berater- und Mandantennummer eintragen (Einstellungen → DATEV-Export)."
    return None


def _ref(text: str | None) -> str:
    """Belegfeld 1: höchstens 36 Zeichen, nur Buchstaben, Ziffern und $ % & * + - /."""
    return re.sub(r"[^A-Za-z0-9$%&*+\-/]", "", text or "")[:36]


def _person(number: str | None, fallback: str) -> str:
    return number if number and re.fullmatch(r"\d{5}", number) else fallback


def _add(result: Export, amount: Decimal, side: str, **kwargs) -> None:
    if amount == 0:
        return
    if amount < 0:  # DATEV kennt keine negativen Umsätze – Soll und Haben tauschen
        amount, side = -amount, ("H" if side == "S" else "S")
    result.bookings.append(Booking(amount=amount, side=side, **kwargs))


def collect(db: Session, start: date, end: date) -> Export:
    if start.year != end.year:
        raise ExportError("Der Zeitraum muss in einem Kalenderjahr liegen (DATEV: ein Wirtschaftsjahr je Datei).")
    v = einstellungen.load(db)
    chart = v.get("konten.rahmen") or "SKR03"
    ku = bool(v.get("steuer.kleinunternehmer"))
    debitor_fallback = str(v.get("datev.sammeldebitor") or "69999")
    creditor_fallback = str(v.get("datev.sammelkreditor") or "99999")
    result = Export(start, end)

    stmt = (select(Document).where(Document.state == STATE_FINAL, Document.doc_date >= start,
                                   Document.doc_date <= end)
            .order_by(Document.doc_date, Document.id))
    for doc in db.scalars(stmt):
        if doc.direction == DIRECTION_IN and doc.status == STATUS_CANCELLED:
            continue
        groups = auswertung.tax_groups(doc)
        name = (doc.recipient_name if doc.direction == DIRECTION_OUT else doc.partner_name) or ""
        common = dict(day=doc.doc_date, ref=_ref(doc.number), text=name[:60])
        if doc.direction == DIRECTION_OUT:
            debitor = _person(doc.customer.number if doc.customer else None, debitor_fallback)
            zero_doc = all(g.rate == 0 for g in groups)
            for g in groups:
                account, _label, _group = konten.income_account(chart, g.rate, doc.hints, ku and zero_doc,
                                                                 steuerschuld.outgoing_kind(doc))
                if not account:
                    result.problems.append(f"Rechnung {doc.number}: kein Erlöskonto für {g.rate} % – bitte von "
                                           "Hand buchen lassen.")
                    continue
                _add(result, g.gross, "S", account=debitor, contra=account, tax_key="", **common)
        else:
            creditor = _person(doc.supplier.number if doc.supplier else None, creditor_fallback)
            side = "H" if doc.kind == "gutschrift" else "S"
            rc_kind = steuerschuld.incoming_kind(doc)
            if rc_kind:
                # § 13b: Rechnung ohne Umsatzsteuer – Steuer und Vorsteuer bucht DATEV über den § 13b-Schlüssel
                rate = steuerschuld.incoming_rate(doc)
                key = "" if ku else REVERSE_CHARGE_KEYS.get(rate, "")
                problem = (f"Eingangsrechnung {doc.number}: § 13b ({steuerschuld.KINDS_IN[rc_kind]}) – "
                           + ("als Kleinunternehmer bitte die geschuldete Steuer von der Steuerberatung buchen lassen."
                              if ku else f"mit BU-Schlüssel {key or '?'} exportiert; bitte den Sachverhalt in DATEV "
                                         "prüfen lassen (Kennzahl 46/47 bzw. 84/85)."))
                result.problems.append(problem)
                for g in groups:
                    _add(result, g.gross, side, account=doc.account or "", contra=creditor, tax_key=key, **common)
                result.docs.append(doc)
                continue
            for g in groups:
                key = ""
                if not ku and doc.account not in AUTOMATIC.get(chart, set()) and g.rate:
                    key = INPUT_TAX_KEYS.get(g.rate, "")
                    if not key:
                        result.problems.append(f"Eingangsrechnung {doc.number}: Steuersatz {g.rate} % ohne "
                                               "BU-Schlüssel – bitte prüfen lassen.")
                _add(result, g.gross, side, account=doc.account or "", contra=creditor, tax_key=key, **common)
        result.docs.append(doc)

    if v.get("datev.zahlungen"):
        _payments(db, result, chart, str(v.get("datev.geldkonto") or BANK_ACCOUNT.get(chart, "1200")),
                  debitor_fallback, creditor_fallback)
    return result


def _payments(db: Session, result: Export, chart: str, bank: str, debitor_fallback: str,
              creditor_fallback: str) -> None:
    stmt = (select(Payment, Document).join(Document, Document.id == Payment.document_id)
            .where(Document.state == STATE_FINAL, Payment.paid_on >= result.start, Payment.paid_on <= result.end)
            .order_by(Payment.paid_on, Payment.id))
    for payment, doc in db.execute(stmt).all():
        partner = doc.partner_name or doc.recipient_name or ""
        common = dict(day=payment.paid_on, ref=_ref(doc.number), text=f"Zahlung {partner}"[:60])
        if doc.direction == DIRECTION_OUT:
            debitor = _person(doc.customer.number if doc.customer else None, debitor_fallback)
            _add(result, payment.amount, "S", account=bank, contra=debitor, tax_key="", **common)
            continue
        creditor = _person(doc.supplier.number if doc.supplier else None, creditor_fallback)
        side = "S" if doc.kind == "gutschrift" else "H"
        _add(result, payment.amount, side, account=bank, contra=creditor, tax_key="", **common)
        if payment.skonto:
            for part in split_by_rate(payment.skonto, auswertung.tax_groups(doc)):
                account = SKONTO_RECEIVED[chart].get(part.rate, SKONTO_RECEIVED[chart][Decimal(0)])
                _add(result, part.gross, "S", account=creditor, contra=account, tax_key="",
                     day=payment.paid_on, ref=_ref(doc.number), text=f"Skonto {doc.partner_name or ''}"[:60])


# ===================================================================== Dateien
def _quote(text: str) -> str:
    return '"' + str(text).replace('"', "'").replace("\r", " ").replace("\n", " ") + '"'


def extf_csv(db: Session, result: Export, *, exported_by: str = "") -> bytes:
    v = einstellungen.load(db)
    chart = v.get("konten.rahmen") or "SKR03"
    now = datetime.now()
    header = [
        _quote("EXTF"), "700", "21", _quote("Buchungsstapel"), "13", now.strftime("%Y%m%d%H%M%S") + "000", "",
        _quote("RE"), _quote(exported_by[:25]), _quote(""), str(v.get("datev.berater") or ""),
        str(v.get("datev.mandant") or ""), f"{result.start.year}0101", "4", result.start.strftime("%Y%m%d"),
        result.end.strftime("%Y%m%d"), _quote(f"Buchungen {format_date(result.start)}-{format_date(result.end)}"),
        _quote(""), "1", "0", "0", _quote("EUR"), "", _quote(""), "", "", _quote(chart[-2:]), "", "", _quote(""),
        _quote(""),
    ]
    lines = [";".join(header), ";".join(COLUMNS)]
    for b in result.bookings:
        lines.append(";".join([
            format_decimal(b.amount).replace(".", ""), _quote(b.side), _quote("EUR"), "", "", _quote(""),
            b.account, b.contra, _quote(b.tax_key), b.day.strftime("%d%m"), _quote(b.ref), _quote(""), "",
            _quote(b.text),
        ]))
    return ("\r\n".join(lines) + "\r\n").encode("cp1252", errors="replace")


def _kind_label(doc: Document) -> str:
    if doc.direction == DIRECTION_OUT:
        return "Stornorechnung" if doc.kind == KIND_STORNO else "Ausgangsrechnung"
    return "Gutschrift (Eingang)" if doc.kind == "gutschrift" else "Eingangsrechnung"


def _file_name(doc: Document, used: set[str]) -> str | None:
    if doc.file is None:
        return None
    ext = ablage.absolute_path(doc.file).suffix or ".pdf"
    base = "_".join(x for x in (doc.doc_date.isoformat(), _ref(doc.number) or f"beleg-{doc.id}") if x)
    prefix = "AR" if doc.direction == DIRECTION_OUT else "ER"
    name = f"Belege/{prefix}_{base}{ext}"
    n = 2
    while name in used:
        name = f"Belege/{prefix}_{base}_{n}{ext}"
        n += 1
    used.add(name)
    return name


def overview_csv(result: Export, names: dict[int, str | None]) -> bytes:
    rows = [["Belegdatum", "Art", "Nummer", "Partner", "Netto", "USt", "Brutto", "Konto", "Status", "Datei"]]
    for doc in result.docs:
        partner = doc.recipient_name if doc.direction == DIRECTION_OUT else doc.partner_name
        rows.append([format_date(doc.doc_date), _kind_label(doc), doc.number or "", partner or "",
                     format_decimal(doc.net_total or ZERO), format_decimal(doc.vat_total or ZERO),
                     format_decimal(doc.gross_total or ZERO), doc.account or "", doc.status_label,
                     (names.get(doc.id) or "fehlt").removeprefix("Belege/")])

    def cell(text) -> str:
        text = str(text)
        if text[:1] in ("=", "+", "-", "@") and not text[1:2].isdigit():
            text = "'" + text
        return '"' + text.replace('"', '""') + '"' if any(c in text for c in ';"\n') else text

    return ("﻿" + "\r\n".join(";".join(cell(c) for c in row) for row in rows) + "\r\n").encode("utf-8")


def build_zip(db: Session, result: Export, *, exported_by: str = "") -> bytes:
    """ZIP: EXTF-Buchungsstapel, Übersicht (CSV) und alle Beleg-Dateien des Zeitraums."""
    period = f"{result.start.isoformat()}_{result.end.isoformat()}"
    buf = io.BytesIO()
    used: set[str] = set()
    names: dict[int, str | None] = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for doc in result.docs:
            name = _file_name(doc, used)
            path = ablage.absolute_path(doc.file) if doc.file is not None else None
            if name and path and path.exists():
                zf.writestr(name, ablage.read_bytes(doc.file, db))
                names[doc.id] = name
                if doc.xml_file is not None:  # XRechnung: die XML-Datei gehört dazu
                    xml_path = ablage.absolute_path(doc.xml_file)
                    if xml_path.exists():
                        zf.writestr(name.rsplit(".", 1)[0] + ".xml", ablage.read_bytes(doc.xml_file, db))
            else:
                names[doc.id] = None
                result.problems.append(f"Beleg {doc.number or doc.id}: Datei fehlt.")
        zf.writestr(f"EXTF_Buchungsstapel_{period}.csv", extf_csv(db, result, exported_by=exported_by))
        zf.writestr(f"Uebersicht_{period}.csv", overview_csv(result, names))
        notes = ["Export für die Steuerberatung", f"Zeitraum: {format_date(result.start)} – {format_date(result.end)}",
                 f"Belege: {len(result.docs)}, Buchungssätze: {len(result.bookings)}", "",
                 "Nur geprüfte, festgeschriebene Belege. Das Programm ersetzt keine Steuerberatung."]
        if result.problems:
            notes += ["", "Bitte beachten:"] + [f"- {p}" for p in result.problems]
        zf.writestr("LIESMICH.txt", "\r\n".join(notes).encode("utf-8"))
    return buf.getvalue()
