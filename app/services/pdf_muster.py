"""Muster-PDFs für die Vorlagen-Auswahl (Einstellungen → Design).

Briefkopf, Farben, Bankdaten und Texte kommen aus den echten Firmen-Einstellungen; Kunde und Positionen sind
Beispielwerte. Die Beispiele sind einfache Objekte und werden NIE in der Datenbank gespeichert.
"""
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.services import einstellungen

WATERMARK = "MUSTER"
_CUSTOMER = SimpleNamespace(number="10001", ust_id=None)
_RECIPIENT = ("Muster GmbH", "z. Hd. Frau Beispiel\nBeispielstraße 12\n12345 Musterstadt")


def _vat_rate(db: Session) -> Decimal:
    return Decimal("0") if einstellungen.get(db, "steuer.kleinunternehmer") else \
        Decimal(str(einstellungen.get(db, "design.ust_satz") or "19"))


def _line(position: int, title: str, description: str, quantity: str, unit: str, price: str, rate: Decimal):
    net = (Decimal(quantity) * Decimal(price)).quantize(Decimal("0.01"))
    return SimpleNamespace(position=position, title=title, description=description, quantity=Decimal(quantity),
                           unit=unit, unit_price=Decimal(price), discount_percent=None, vat_rate=rate, net=net,
                           vat=(net * rate / 100).quantize(Decimal("0.01")))


def invoice(db: Session) -> SimpleNamespace:
    rate = _vat_rate(db)
    lines = [_line(1, "Beratung und Planung", "Bestandsaufnahme vor Ort, Abstimmung des Leistungsumfangs",
                   "4", "Std.", "85.00", rate),
             _line(2, "Ausführung der Arbeiten", "laut Angebot, inkl. Kleinmaterial", "1", "pauschal", "1250.00",
                   rate),
             _line(3, "Anfahrt", "", "2", "Stk.", "35.00", rate)]
    net = sum(line.net for line in lines)
    vat = sum(line.vat for line in lines)
    today = date.today()
    days = int(einstellungen.get(db, "design.zahlungsziel_tage") or 14)
    return SimpleNamespace(
        kind="rechnung", number=f"{today.year}-001", doc_date=today, due_date=today + timedelta(days=days),
        customer=_CUSTOMER, service_date=today - timedelta(days=3), service_until=None, storno_of=None, hints=[],
        labor_amount=None, recipient_name=_RECIPIENT[0], recipient_address=_RECIPIENT[1],
        object_address="Beispielstraße 12, 12345 Musterstadt",
        intro_text=einstellungen.get(db, "rechnung.einleitung"), closing_text=einstellungen.get(db, "design.danke_text"),
        lines=lines, net_total=net, vat_total=vat, gross_total=net + vat, payment_terms_days=days,
        payment_qr="girocode", notes=None)


def quote(db: Session) -> SimpleNamespace:
    rate = _vat_rate(db)
    today = date.today()

    def position(no, title, description, quantity, unit, price):
        return SimpleNamespace(position=no, title=title, description=description, billing_type="menge",
                               quantity=Decimal(quantity), unit=unit, unit_price=Decimal(price), hours=None,
                               staff_count=None, vat_rate=rate, recurring=False, interval=None, visits_per_month=None,
                               contract_months=None, cost_rate=None, catalog_item_id=None)

    return SimpleNamespace(
        number=f"A-{today.year}-001", quote_date=today, valid_until=today + timedelta(days=30), customer=_CUSTOMER,
        recipient_name=_RECIPIENT[0], recipient_address=_RECIPIENT[1], object_address=None,
        intro_text="vielen Dank für Ihre Anfrage. Gerne bieten wir Ihnen folgende Leistungen an:",
        closing_text="Wir freuen uns auf Ihren Auftrag.",
        positions=[position(1, "Beratung und Planung", "Bestandsaufnahme vor Ort", "4", "Std.", "85.00"),
                   position(2, "Ausführung der Arbeiten", "inkl. Kleinmaterial", "1", "pauschal", "1250.00")],
        cost_items=[])


def render(db: Session, art: str, vorlage: str | None) -> bytes:
    """Muster-PDF der Belegart ``art`` in der Vorlage ``vorlage`` (mit Wasserzeichen „MUSTER“)."""
    if art == "angebot":
        from app.services import pdf_angebot

        return pdf_angebot.render(db, quote(db), vorlage=vorlage, watermark=WATERMARK)
    if art == "mahnung":
        from app.services import mahnwesen, pdf_mahnung

        doc = invoice(db)
        doc.doc_date, doc.due_date = doc.doc_date - timedelta(days=30), doc.doc_date - timedelta(days=16)
        level = mahnwesen.levels(db)[0]
        today = date.today()
        calc = mahnwesen.Calculation(level, today, today + timedelta(days=level.days), doc.gross_total, level.fee,
                                     Decimal("0"), 16, None)
        dunning = SimpleNamespace(number=f"M-{today.year}-001", dunning_date=today, level_name=level.name)
        return pdf_mahnung.render(db, doc, dunning, calc, vorlage=vorlage, watermark=WATERMARK)
    from app.services import pdf_rechnung

    return pdf_rechnung.render(db, invoice(db), vorlage=vorlage, watermark=WATERMARK)
