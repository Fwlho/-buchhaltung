"""Mahnungs-PDF im Firmendesign – gleiches Briefpapier wie Rechnung und Angebot (DIN 5008)."""
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer
from sqlalchemy.orm import Session

from app.formatting import format_date, format_decimal, format_money
from app.models import Document, Dunning
from app.services import pdf_vorlagen
from app.services.email_entwurf import fill
from app.services.pdf_rechnung import BottomBlock, _esc, _lines, branding, girocode_payload


def render(db: Session, doc: Document, dunning: Dunning, calc, *, vorlage: str | None = None,
           watermark: str | None = None) -> bytes:
    """``vorlage``: abweichende Vorlage (z. B. Muster in den Einstellungen), sonst die eingestellte."""
    from app.services import mahnwesen

    values, accent, logo = branding(db)
    design = pdf_vorlagen.design(values, "mahnung", vorlage)
    accent = design.accent(values, accent)
    st = design.styles(accent)
    info = [("Mahnungsnr.", dunning.number), ("Datum", format_date(dunning.dunning_date))]
    if doc.customer is not None:
        info.append(("Kundennr.", doc.customer.number))
    info.append(("zu Rechnung", doc.number))

    title = f"{dunning.level_name} zur Rechnung Nr. {doc.number}"
    story: list = [*design.title_flowables(title, st), Spacer(0, 4 * mm),
                   Paragraph("Sehr geehrte Damen und Herren,", st["body"]), Spacer(0, 2 * mm),
                   Paragraph(_lines(fill(calc.level.text, mahnwesen.placeholders(db, doc, calc))), st["body"]),
                   Spacer(0, 5 * mm)]

    paid = (doc.gross_total or 0) - calc.open_amount
    row = [Paragraph(_esc(doc.number), st["cell"]), Paragraph(format_date(doc.doc_date), st["cell"]),
           Paragraph(format_date(doc.due_date), st["cell_r"]), Paragraph(format_money(doc.gross_total), st["cell_r"]),
           Paragraph(format_money(paid) if paid else "–", st["cell_r"]),
           Paragraph(format_money(calc.open_amount), st["cell_r"])]
    story.append(design.positions_table(["Rechnung", "Datum", "Fällig am", "Betrag", "bezahlt", "offen"],
                                 [None, 24 * mm, 24 * mm, 27 * mm, 25 * mm, 27 * mm], [row], accent))
    story.append(Spacer(0, 3 * mm))
    sums = [("Offener Rechnungsbetrag", format_money(calc.open_amount))]
    if calc.fees:
        sums.append(("Mahngebühren", format_money(calc.fees)))
    if calc.interest:
        sums.append((f"Verzugszinsen {format_decimal(calc.rate)} % p. a. für {calc.interest_days} Tage",
                     format_money(calc.interest)))
    story.append(design.sums_table(sums, "Zu zahlen", format_money(calc.total), accent))
    story.append(Spacer(0, 5 * mm))

    pay = Paragraph(f"Bitte überweisen Sie <b>{format_money(calc.total)}</b> bis zum "
                    f"<b>{format_date(calc.pay_until)}</b> unter Angabe der Rechnungsnummer auf unten genanntes "
                    "Konto.", st["body"])
    iban = values.get("bank.iban")
    if iban and values.get("rechnung.zahlungs_qr", "girocode") == "girocode":
        payload = girocode_payload(name=values.get("bank.kontoinhaber") or values.get("firma.name") or "",
                                   iban=iban, bic=values.get("bank.bic"), amount=calc.total,
                                   reference=f"Rechnung {doc.number}")
        qr_text = Paragraph("<b>Bequem bezahlen:</b> QR-Code mit der Banking-App scannen (GiroCode).", st["small"])
        bottom: list = [design.payment_block(pay, qr_text, payload)]
    else:
        bottom = [pay]
    # Wie bei der Rechnung: Zahlungsaufforderung und Gruß unten direkt über der Fußzeile
    bottom += [Spacer(0, 4 * mm),
               Paragraph("Sollten Sie die Zahlung inzwischen veranlasst haben, betrachten Sie dieses Schreiben "
                         "bitte als gegenstandslos.", st["small"]),
               Spacer(0, 4 * mm),
               Paragraph(f"Mit freundlichen Grüßen<br/>{_esc(values.get('firma.name') or '')}", st["body"])]
    story.append(BottomBlock(bottom))

    return design.build(story, values=values, logo=logo, accent=accent, recipient_name=doc.recipient_name,
                        recipient_address=doc.recipient_address, info=info, title=title, watermark=watermark,
                        label=dunning.level_name)
