"""Angebots-PDF im Firmendesign – gleiches Layout wie die Rechnung (DIN 5008).

Interne Kosten und Marge erscheinen hier NIE.
"""
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer
from sqlalchemy.orm import Session

from app.formatting import format_date, format_decimal, format_money, format_percent, format_price
from app.models import Quote
from app.services import calc, pdf_vorlagen, rechnungen
from app.services.betraege import group_by_rate, totals
from app.services.pdf_rechnung import _esc, _lines, _qty, branding


def render(db: Session, quote: Quote, *, vorlage: str | None = None, watermark: str | None = None) -> bytes:
    """``vorlage``: abweichende Vorlage (z. B. Muster in den Einstellungen), sonst die eingestellte."""
    values, accent, logo = branding(db)
    design = pdf_vorlagen.design(values, "angebot", vorlage)
    accent = design.accent(values, accent)
    st = design.styles(accent)
    r = calc.compute_quote(quote.positions, quote.cost_items)
    small_business = rechnungen.is_small_business(db)

    info = [("Angebotsnr.", quote.number), ("Datum", format_date(quote.quote_date))]
    if quote.valid_until:
        info.append(("Gültig bis", format_date(quote.valid_until)))
    if quote.customer is not None:
        info.append(("Kundennr.", quote.customer.number))

    title = f"Angebot Nr. {quote.number}"
    story: list = [*design.title_flowables(title, st), Spacer(0, 3 * mm)]
    if quote.object_address:
        story.append(Paragraph(f"<b>Objekt:</b> {_lines(quote.object_address).replace('<br/>', ', ')}", st["body"]))
        story.append(Spacer(0, 2 * mm))
    if quote.intro_text:
        story.append(Paragraph(_lines(quote.intro_text), st["body"]))
        story.append(Spacer(0, 4 * mm))

    rates = {p.vat_rate for p in quote.positions} | {c.vat_rate for c in quote.cost_items}
    show_rate = len(rates) > 1
    head = ["Pos.", "Bezeichnung", "Menge", "Einheit", "Einzelpreis"] + (["USt"] if show_rate else []) + ["Gesamt"]
    widths = [12 * mm, None, 17 * mm, 19 * mm, 23 * mm] + ([11 * mm] if show_rate else []) + [25 * mm]

    def row(no, title_html, qty, unit, price, rate, total):
        cells = [Paragraph(str(no), st["cell"]), Paragraph(title_html, st["cell"]), Paragraph(qty, st["cell_r"]),
                 Paragraph(_esc(unit), st["cell_r"]), Paragraph(price, st["cell_r"])]
        if show_rate:
            cells.append(Paragraph(format_percent(rate), st["cell_r"]))
        cells.append(Paragraph(total, st["cell_r"]))
        return cells

    rows = []
    no = 0
    for p, res in zip(quote.positions, r.positions, strict=True):
        no += 1
        details = []
        if p.billing_type == "stunden":
            details.append(f"{p.staff_count or 1} Person(en) × {format_decimal(p.hours, 2)} Std.")
            qty, unit = _qty((p.hours or 0) * (p.staff_count or 1)), "Std."
        elif p.billing_type == "pauschal":
            qty, unit = "1", "pauschal"
        else:
            qty, unit = _qty(p.quantity), p.unit or ""
        if res.recurring:
            label = calc.INTERVALS.get(p.interval or "", ("",))[0]
            details.append(f"je Einsatz {format_money(res.net_per_visit)} · {label}, "
                           f"ca. {format_decimal(res.visits_per_month, 2)} Einsätze/Monat · "
                           f"monatlich {format_money(res.monthly)}"
                           + (f" · Laufzeit {res.contract_months} Monate" if res.contract_months else ""))
        text = f"<b>{_esc(p.title)}</b>"
        if p.description:
            text += f'<br/><font size="8" color="#555555">{_lines(p.description)}</font>'
        for d in details:
            text += f'<br/><font size="8" color="#555555">{_esc(d)}</font>'
        rows.append(row(no, text, qty, unit, format_price(p.unit_price) + " €", p.vat_rate,
                        format_money(res.line_total)))
    for c in quote.cost_items:
        no += 1
        text = f"<b>{_esc(c.block)}</b>" + (f"<br/>{_esc(c.description)}" if c.description else "")
        rows.append(row(no, text, _qty(c.quantity), c.unit or "", format_price(c.unit_price) + " €", c.vat_rate,
                        format_money(calc.compute_cost_item_net(c))))
    story.append(design.positions_table(head, widths, rows, accent))
    story.append(Spacer(0, 3 * mm))

    sums = []
    if not small_business:
        sums.append(("Summe netto", format_money(r.net_total)))
        for t in r.tax_lines:
            sums.append((f"zzgl. {format_percent(t.rate)} USt auf {format_money(t.net)}", format_money(t.vat)))
    story.append(design.sums_table(sums, "Angebotssumme", format_money(r.gross_total), accent))
    story.append(Spacer(0, 5 * mm))

    if r.monthly_net:
        monthly_lines = group_by_rate((res.vat_rate, res.monthly) for res in r.positions if res.monthly is not None)
        monthly_gross = totals(monthly_lines)[2]
        text = f"<b>Wiederkehrende Leistungen:</b> {format_money(r.monthly_net)} netto pro Monat"
        if not small_business:
            text += f" ({format_money(monthly_gross)} brutto)"
        story.append(Paragraph(text + ".", st["body"]))
        story.append(Spacer(0, 2 * mm))
    if small_business:
        story.append(Paragraph(_esc(rechnungen.KU_HINT), st["body"]))
    if quote.valid_until:
        story.append(Paragraph(f"Dieses Angebot ist gültig bis zum {format_date(quote.valid_until)}.", st["body"]))
    if quote.closing_text:
        story.append(Spacer(0, 4 * mm))
        story.append(Paragraph(_lines(quote.closing_text), st["body"]))
    acceptance = design.acceptance_block(st, accent)  # „Auftrag erteilt“ zum Unterschreiben (je nach Vorlage)
    if acceptance is not None:
        story += [Spacer(0, 8 * mm), acceptance]

    return design.build(story, values=values, logo=logo, accent=accent, recipient_name=quote.recipient_name,
                        recipient_address=quote.recipient_address, info=info, title=title, watermark=watermark,
                        label="Angebot")
