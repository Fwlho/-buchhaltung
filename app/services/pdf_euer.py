"""EÜR als PDF (schlichte Liste im Firmendesign, Querverweis auf die Positionen der Anlage EÜR)."""
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy.orm import Session

from app.formatting import format_datetime, format_money
from app.database import utcnow
from app.services import konten
from app.services.auswertung import Euer
from app.services.pdf_rechnung import FONT, _esc, _styles, branding


def render(db: Session, result: Euer) -> bytes:
    values, accent, _ = branding(db)
    st = _styles(accent)
    story = [
        Paragraph(_esc(values.get("firma.name") or ""), st["small"]),
        Paragraph(f"Einnahmen-Überschuss-Rechnung – {_esc(result.period.label)}", st["title"]),
        Paragraph(f"Nach Zahlungsdatum (Zufluss/Abfluss). Erstellt am {format_datetime(utcnow())}. "
                  "Vorschlag – ersetzt keine Steuerberatung.", st["small"]),
        Spacer(0, 5 * mm),
    ]
    rows: list[list] = []
    bold_rows: list[int] = []

    def add(label: str, amount=None, *, bold=False, indent=False):
        if bold:
            bold_rows.append(len(rows))
        text = ("&nbsp;&nbsp;&nbsp;&nbsp;" if indent else "") + _esc(label)
        rows.append([Paragraph(f"<b>{text}</b>" if bold else text, st["cell"]),
                     Paragraph(f"<b>{format_money(amount)}</b>" if bold and amount is not None else
                               (format_money(amount) if amount is not None else ""), st["cell_r"])])

    add("Betriebseinnahmen", bold=True)
    for g in result.income:
        add(g.label, g.net)
        for account, (label, net) in g.accounts.items():
            add(f"{account} {label}" if account != label else label, net, indent=True)
    add(konten.EUER_VAT_RECEIVED, result.vat_received)
    if result.vat_refund:
        add(konten.EUER_VAT_REFUND, result.vat_refund)
    add("Summe Betriebseinnahmen", result.income_total, bold=True)
    add("")
    add("Betriebsausgaben", bold=True)
    for g in result.expenses:
        add(g.label, g.net)
        for account, (label, net) in g.accounts.items():
            add(label if account == label else f"{account} {label}", net, indent=True)
    add(konten.EUER_VAT_PAID, result.vat_paid)
    if result.vat_to_office:
        add(konten.EUER_VAT_TO_OFFICE, result.vat_to_office)
    add("Summe Betriebsausgaben", result.expense_total, bold=True)
    add("")
    add("Überschuss" if result.surplus >= 0 else "Verlust", result.surplus, bold=True)

    table = Table(rows, colWidths=[125 * mm, 40 * mm])
    style = [("FONTNAME", (0, 0), (-1, -1), FONT)] + [("LINEBELOW", (0, i), (-1, i), 0.6, accent) for i in bold_rows]
    style += [("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
              ("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1), colors.HexColor("#f2f4f7"))]
    table.setStyle(TableStyle(style))
    story.append(table)
    story.append(Spacer(0, 6 * mm))
    for hint in result.hints:
        story.append(Paragraph("• " + _esc(hint), st["small"]))

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=22 * mm, rightMargin=20 * mm, topMargin=18 * mm,
                      bottomMargin=18 * mm, title=f"EÜR {result.period.label}").build(story)
    return buf.getvalue()
