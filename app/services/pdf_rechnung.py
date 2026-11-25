"""Rechnungs-PDF im Firmendesign (Layout nach DIN 5008, Form B).

- Anschriftfeld passend für Fensterumschläge (45 mm von oben, 20 mm von links)
- Informationsblock rechts (Rechnungsnr., Datum, Kundennummer, Leistungsdatum)
- Positionen: Bezeichnung fett, Beschreibung darunter
- Steuerausweis je Steuersatz, Hinweise (§ 19, § 13b, § 35a, § 14b)
- GiroCode (EPC-QR) zum Bezahlen per Banking-App
- Fußzeile mit Firmendaten auf jeder Seite, Falz- und Lochmarken
Alles Firmenspezifische kommt aus den Einstellungen – nichts ist im Code fest hinterlegt.
"""
import io
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

import reportlab
from reportlab import rl_config
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import registerFontFamily
from reportlab.pdfbase.ttfonts import TTFont

from reportlab.graphics.barcode import qrencoder
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import BaseDocTemplate, Flowable, Frame, KeepTogether, PageTemplate, Paragraph, Spacer, Table
from reportlab.platypus.flowables import HRFlowable
from reportlab.platypus import TableStyle
from sqlalchemy.orm import Session

from app.formatting import format_date, format_decimal, format_money, format_percent, format_price
from app.models import Document, StoredFile
from app.models.belege import KIND_STORNO
from app.services import ablage, einstellungen
from app.services.pruefungen import format_iban

# Eingebettete Schrift (für PDF/A-3 / ZUGFeRD Pflicht): Bitstream Vera wird mit reportlab mitgeliefert
_FONT_DIR = Path(reportlab.__file__).parent / "fonts"
FONT, FONT_BOLD = "Vera", "Vera-Bold"
for _name, _file in ((FONT, "Vera.ttf"), (FONT_BOLD, "VeraBd.ttf"), ("Vera-Italic", "VeraIt.ttf"),
                     ("Vera-BoldItalic", "VeraBI.ttf")):
    pdfmetrics.registerFont(TTFont(_name, str(_FONT_DIR / _file)))
registerFontFamily(FONT, normal=FONT, bold=FONT_BOLD, italic="Vera-Italic", boldItalic="Vera-BoldItalic")
# reportlab setzt sonst auf jeder Seite „Helvetica“ als Startschrift – die wäre nicht eingebettet (kein PDF/A)
rl_config.canvas_basefontname = FONT

W, H = A4
LEFT, RIGHT = 25 * mm, 20 * mm
CONTENT_W = W - LEFT - RIGHT
TEXT = colors.HexColor("#222222")
MUTED = colors.HexColor("#666666")
LIGHT = colors.HexColor("#f2f4f7")


def _esc(text) -> str:
    return escape(str(text or ""))


def _lines(text) -> str:
    return "<br/>".join(_esc(x) for x in str(text or "").splitlines())


def _width(text: str, font: str, size: float) -> float:
    return pdfmetrics.stringWidth(text, font, size)


def _ellipsis(text: str, font: str, size: float, max_width: float) -> str:
    """Kürzt mit „…“, bis der Text in die Breite passt."""
    if _width(text, font, size) <= max_width:
        return text
    while text and _width(text + "…", font, size) > max_width:
        text = text[:-1]
    return text.rstrip() + "…"


def fit_text(text: str, font: str, size: float, max_width: float, min_size: float = 5.5) -> tuple[str, float]:
    """Eine Zeile passend machen: erst Schrift verkleinern (bis ``min_size``), dann mit „…“ kürzen.
    Gibt (Text, Schriftgröße) zurück."""
    text = str(text or "")
    while size > min_size and _width(text, font, size) > max_width:
        size = max(min_size, size - 0.25)
    return _ellipsis(text, font, size, max_width), size


def wrap_text(text: str, font: str, size: float, max_width: float) -> list[str]:
    """Zeilenumbruch nach gemessener Breite (an Leerzeichen; überlange Wörter werden getrennt)."""
    lines: list[str] = []
    for paragraph in str(text or "").splitlines() or [""]:
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}".strip()
            if _width(candidate, font, size) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
            while _width(word, font, size) > max_width:  # z. B. lange E-Mail-Adresse ohne Leerzeichen
                cut = len(word)
                while cut > 1 and _width(word[:cut], font, size) > max_width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            current = word
        lines.append(current)
    return lines


def _qty(value: Decimal | None) -> str:
    if value is None:
        return ""
    value = Decimal(value).normalize()
    places = max(0, -value.as_tuple().exponent)
    return format_decimal(value, min(places, 4))


# ------------------------------------------------------------------ GiroCode
def girocode_payload(*, name: str, iban: str, bic: str | None, amount: Decimal, reference: str) -> str:
    """SEPA-Überweisung als QR-Inhalt nach EPC069-12 (Version 002, UTF-8)."""
    return "\n".join([
        "BCD", "002", "1", "SCT", (bic or "").replace(" ", ""), name[:70], iban.replace(" ", ""),
        f"EUR{amount:.2f}", "", "", reference[:140],
    ])


class QrFlowable(Flowable):
    def __init__(self, payload: str, size: float):
        super().__init__()
        self.payload, self.size = payload, size
        self.width = self.height = size

    def draw(self):
        # Module direkt als Quadrate zeichnen – der Grafik-Renderer von reportlab würde sonst eine nicht
        # eingebettete Standardschrift ins PDF schreiben (verletzt PDF/A).
        qr = qrencoder.QRCode(None, qrencoder.QRErrorCorrectLevel.M)
        qr.addData(self.payload)
        qr.make()
        count = qr.getModuleCount()
        quiet = 4  # Ruhezone in Modulen
        module = self.size / (count + 2 * quiet)
        self.canv.saveState()
        self.canv.setFillColor(colors.black)
        for row in range(count):
            for col in range(count):
                if qr.isDark(row, col):
                    self.canv.rect((col + quiet) * module, self.size - (row + quiet + 1) * module, module, module,
                                   stroke=0, fill=1)
        self.canv.restoreState()


# --------------------------------------------------------------------- Seite
class _Page:
    """Zeichnet Briefkopf, Anschrift, Infoblock (Seite 1) sowie Fußzeile/Marken (alle Seiten)."""

    label: str | None = None  # Belegart („Rechnung“, Mahnstufe …) – für Vorlagen, die sie im Kopf zeigen
    first_top = 100 * mm      # Inhalt der ersten Seite beginnt so weit unter dem oberen Rand

    def content_bottom(self) -> float:
        """Der Inhalt endet oberhalb der Fußzeile (die je nach Firmendaten höher sein kann)."""
        return footer_layout(self.v)["content_bottom"]

    def __init__(self, values: dict, recipient_name: str | None, recipient_address: str | None, logo: bytes | None,
                 accent, watermark: str | None, info: list):
        self.v, self.logo, self.accent, self.watermark, self.info = values, logo, accent, watermark, info
        self.recipient_name, self.recipient_address = recipient_name, recipient_address

    def first(self, canv, pdf):
        self._header(canv)
        self._address(canv)
        self._info_block(canv)
        self.later(canv, pdf)

    def later(self, canv, pdf):
        canv.saveState()
        if self.v.get("design.falzmarken"):
            # Falzmarken (105 / 210 mm) und Lochmarke (148,5 mm) – nur auf Wunsch (Einstellung „Design“)
            canv.setStrokeColor(MUTED)
            canv.setLineWidth(0.3)
            for y_mm, length in ((105, 4), (148.5, 6), (210, 4)):
                canv.line(5 * mm, H - y_mm * mm, (5 + length) * mm, H - y_mm * mm)
        self._footer(canv, pdf.page)
        if self.watermark:
            canv.setFillColor(colors.Color(0.85, 0.2, 0.2, alpha=0.18))
            canv.setFont(FONT_BOLD, 70)
            canv.translate(W / 2, H / 2)
            canv.rotate(40)
            canv.drawCentredString(0, 0, self.watermark)
        canv.restoreState()

    def _header(self, canv):
        """Briefkopf: Firmenname zentriert, Logo links oder rechts (Einstellung „design.logo_position“)."""
        canv.saveState()
        top, bottom = H - 14 * mm, H - 39 * mm  # Kopfbereich oberhalb der Akzentlinie (42 mm)
        logo_w = 0
        if self.logo:
            try:
                img = ImageReader(io.BytesIO(self.logo))
                iw, ih = img.getSize()
                scale = min(50 * mm / iw, (top - bottom) / ih)
                logo_w, h = iw * scale, ih * scale
                x = LEFT if self.v.get("design.logo_position") == "links" else W - RIGHT - logo_w
                canv.drawImage(img, x, top - (top - bottom + h) / 2, logo_w, h, mask="auto")
            except Exception:  # noqa: BLE001 – kaputtes Logo darf die Rechnung nicht verhindern
                self.logo, logo_w = None, 0
        # Name genau in der Mitte – daher auf beiden Seiten so viel Platz freilassen, wie das Logo braucht
        avail = CONTENT_W - 2 * (logo_w + 4 * mm if logo_w else 0)
        lines, size = name_lines(self.v.get("firma.name") or "", avail)
        canv.setFillColor(self.accent)
        canv.setFont(FONT_BOLD, size)
        leading = size * 1.2
        y = (top + bottom) / 2 + (len(lines) - 1) * leading / 2 - size * 0.35
        for line in lines:
            canv.drawCentredString(W / 2, y, line)
            y -= leading
        canv.setStrokeColor(self.accent)
        canv.setLineWidth(1.2)
        canv.line(LEFT, H - 42 * mm, W - RIGHT, H - 42 * mm)
        canv.restoreState()

    def _address(self, canv):
        canv.saveState()
        x = 20 * mm
        sender = " · ".join(x for x in (self.v.get("firma.name"), self.v.get("firma.strasse"),
                                         f"{self.v.get('firma.plz') or ''} {self.v.get('firma.ort') or ''}".strip()) if x)
        window = 85 * mm  # Breite des Anschriftfelds (Fensterumschlag)
        sender, size = fit_text(sender, FONT, 7, window)
        canv.setFont(FONT, size)
        canv.setFillColor(MUTED)
        canv.drawString(x, H - 50 * mm, sender)
        canv.setStrokeColor(MUTED)
        canv.setLineWidth(0.3)
        canv.line(x, H - 51 * mm, x + window, H - 51 * mm)
        canv.setFillColor(TEXT)
        # Lange Namen/Zeilen umbrechen; passt es nicht in 7 Zeilen, Schrift etwas kleiner
        raw = [self.recipient_name or ""] + str(self.recipient_address or "").splitlines()
        for size, leading in ((10, 4.6 * mm), (9, 4.1 * mm), (8, 3.6 * mm)):
            lines = [part for line in raw for part in wrap_text(line, FONT, size, window)]
            if len(lines) <= 7:
                break
        canv.setFont(FONT, size)
        y = H - 57 * mm
        for line in lines[:8]:
            canv.drawString(x, y, line)
            y -= leading
        canv.restoreState()

    def _info_block(self, canv):
        canv.saveState()
        x_label, x_value = 118 * mm, 150 * mm
        label_w, value_w = x_value - x_label - 2 * mm, W - RIGHT - x_value
        y = H - 50 * mm
        for label, value in self.info:
            text, size = fit_text(label, FONT, 8.5, label_w, min_size=6.5)
            canv.setFont(FONT, size)
            canv.setFillColor(MUTED)
            canv.drawString(x_label, y, text)
            font = FONT_BOLD if label.startswith(("Rechnung", "Storno", "Mahnung")) else FONT
            canv.setFont(font, 8.5)
            canv.setFillColor(TEXT)
            for line in wrap_text(str(value or ""), font, 8.5, value_w)[:3]:  # lange Werte umbrechen
                canv.drawString(x_value, y, line)
                y -= 4.0 * mm
            y -= 0.4 * mm
        canv.restoreState()

    def _footer(self, canv, page_no):
        v = self.v
        layout = footer_layout(v)
        canv.saveState()
        canv.setStrokeColor(self.accent)
        canv.setLineWidth(0.6)
        canv.line(LEFT, layout["rule"], W - RIGHT, layout["rule"])
        canv.setFillColor(MUTED)
        # Vollständig statt abgekürzt: lange Angaben laufen in der eigenen Spalte in die nächste Zeile
        for i, lines in enumerate(layout["columns"]):
            y = layout["first_line"]
            for line, size in lines:
                canv.setFont(FONT, size)
                canv.drawString(LEFT + i * layout["col_w"], y, line)
                y -= FOOTER_LEADING
        if v.get("design.fusszeile"):
            line, size = fit_text(str(v["design.fusszeile"]).splitlines()[0], FONT, FOOTER_SIZE, CONTENT_W)
            canv.setFont(FONT, size)
            canv.drawCentredString(W / 2, 8 * mm, line)
        canv.setFont(FONT, FOOTER_SIZE)
        canv.drawRightString(W - RIGHT, layout["rule"] + 3.5 * mm, f"Seite {page_no}")
        canv.restoreState()


FOOTER_SIZE = 6.8
FOOTER_MIN_SIZE = 6.0
FOOTER_LEADING = 3 * mm


def footer_layout(v: dict) -> dict:
    """Fußzeile in vier Spalten; jede Angabe wird in ihrer Spalte umbrochen. Liefert die Zeilen und die
    Höhen – der Inhaltsbereich endet oberhalb (``content_bottom``), damit sich nichts überdeckt."""
    columns = [
        [v.get("firma.name"), v.get("firma.strasse"), f"{v.get('firma.plz') or ''} {v.get('firma.ort') or ''}".strip()],
        [f"Tel. {v['firma.telefon']}" if v.get("firma.telefon") else None, v.get("firma.email"), v.get("firma.website")],
        [f"Inhaber/GF: {v['firma.inhaber']}" if v.get("firma.inhaber") else None,
         f"St.-Nr. {v['steuer.steuernummer']}" if v.get("steuer.steuernummer") else None,
         f"USt-IdNr. {v['steuer.ust_id']}" if v.get("steuer.ust_id") else None, v.get("firma.handelsregister")],
        [v.get("bank.name"), f"IBAN {format_iban(v['bank.iban'])}" if v.get("bank.iban") else None,
         f"BIC {v['bank.bic']}" if v.get("bank.bic") else None],
    ]
    col_w = CONTENT_W / 4
    avail = col_w - 2 * mm

    def lines_of(text: str) -> list[tuple[str, float]]:
        # Knapp zu lang (z. B. IBAN): Schrift minimal kleiner statt Umbruch mitten in der Angabe
        size = FOOTER_SIZE
        while size > FOOTER_MIN_SIZE and _width(text, FONT, size) > avail:
            size = round(size - 0.1, 2)
        if _width(text, FONT, size) <= avail:
            return [(text, size)]
        return [(part, FOOTER_SIZE) for part in wrap_text(text, FONT, FOOTER_SIZE, avail)]

    wrapped = [[line for text in col if text for line in lines_of(str(text))] for col in columns]
    count = max((len(c) for c in wrapped), default=1) or 1
    last_line = (12 if v.get("design.fusszeile") else 9) * mm
    first_line = last_line + (count - 1) * FOOTER_LEADING
    rule = first_line + 3.5 * mm
    return {"columns": wrapped, "col_w": col_w, "first_line": first_line, "rule": rule,
            "content_bottom": max(32 * mm, rule + 8 * mm)}


def name_lines(name: str, avail: float) -> tuple[list[str], float]:
    """Firmenname im Briefkopf: möglichst eine große Zeile, sonst zwei kleinere, zuletzt gekürzt."""
    for size in (17, 16, 15, 14):
        if _width(name, FONT_BOLD, size) <= avail:
            return [name], size
    for size in (14, 13, 12, 11, 10):
        lines = wrap_text(name, FONT_BOLD, size, avail)
        if len(lines) <= 2:
            return lines, size
    lines = wrap_text(name, FONT_BOLD, 10, avail)
    return [lines[0], _ellipsis(" ".join(lines[1:]), FONT_BOLD, 10, avail)], 10


class BottomBlock(Flowable):
    """Setzt seinen Inhalt (Zahlungshinweis, GiroCode, Schlusstext) unten auf die Seite – direkt über die
    Fußzeile. Passt er nicht mehr auf die Seite, beginnt er unten auf der nächsten."""

    def __init__(self, flowables: list):
        super().__init__()
        self.flowables = flowables
        self._sizes: list[tuple[float, float]] = []

    def _content_height(self, avail_width: float, avail_height: float) -> float:
        self._sizes = [f.wrap(avail_width, avail_height) for f in self.flowables]
        return sum(h for _, h in self._sizes)

    def wrap(self, avail_width, avail_height):
        self.width = avail_width
        content = self._content_height(avail_width, avail_height)
        self.height = max(avail_height - 0.5, content)  # restliche Höhe der Seite belegen
        return self.width, self.height

    def split(self, avail_width, avail_height):
        return []  # nie zerteilen – lieber komplett auf die nächste Seite

    def draw(self):
        y = sum(h for _, h in self._sizes)
        for f, (_, h) in zip(self.flowables, self._sizes, strict=True):
            y -= h
            f.drawOn(self.canv, 0, y)


# -------------------------------------------------------------------- Inhalt
def _styles(accent):
    base = dict(fontName=FONT, fontSize=9.5, leading=12.5, textColor=TEXT)
    return {
        "title": ParagraphStyle("title", fontName=FONT_BOLD, fontSize=14, leading=18, textColor=accent),
        "body": ParagraphStyle("body", **base),
        "small": ParagraphStyle("small", **{**base, "fontSize": 8.5, "leading": 11, "textColor": MUTED}),
        "cell": ParagraphStyle("cell", **{**base, "fontSize": 9, "leading": 11.5}),
        "cell_r": ParagraphStyle("cell_r", **{**base, "fontSize": 9, "leading": 11.5, "alignment": 2}),
        "head": ParagraphStyle("head", **{**base, "fontName": FONT_BOLD, "fontSize": 8.5, "leading": 11}),
        "head_r": ParagraphStyle("head_r", **{**base, "fontName": FONT_BOLD, "fontSize": 8.5, "leading": 11,
                                              "alignment": 2}),
    }


def branding(db: Session) -> tuple[dict, colors.Color, bytes | None]:
    """Einstellungen, Akzentfarbe und Logo der aktiven Firma."""
    values = einstellungen.load(db)
    accent = colors.HexColor(values.get("design.akzentfarbe") or "#1f5fbf")
    logo = None
    if values.get("design.logo"):
        stored = db.get(StoredFile, values["design.logo"])
        if stored is not None:
            try:
                logo = ablage.read_bytes(stored, db)
            except (OSError, ValueError):
                logo = None
    return values, accent, logo


def positions_table(head: list[str], widths: list, rows: list[list], accent) -> Table:
    """Positionstabelle; ``widths`` darf genau ein None (= Restbreite) enthalten."""
    widths = list(widths)
    widths[widths.index(None)] = CONTENT_W - sum(w for w in widths if w)
    table = Table([head_row(head)] + rows, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), FONT), 
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, accent),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#dddddd")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def head_row(head: list[str]) -> list:
    st = _styles(colors.black)
    return [Paragraph(h, st["head_r"] if i >= 2 else st["head"]) for i, h in enumerate(head)]


def sums_table(rows: list[tuple[str, str]], total_label: str, total_value: str, accent) -> KeepTogether:
    st = _styles(accent)
    data = [[Paragraph(_esc(a), st["cell_r"]), Paragraph(_esc(b), st["cell_r"])] for a, b in rows]
    data.append([Paragraph(f"<b>{_esc(total_label)}</b>", st["cell_r"]),
                 Paragraph(f"<b>{_esc(total_value)}</b>", st["cell_r"])])
    table = Table(data, colWidths=[75 * mm, 30 * mm], hAlign="RIGHT")
    table.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), FONT), 
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, accent),
        ("BACKGROUND", (0, -1), (-1, -1), LIGHT),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    # Durchgehende Trennlinie unter den Summen über die ganze Seitenbreite
    rule = HRFlowable(width="100%", thickness=0.8, color=accent, spaceBefore=1.5 * mm, spaceAfter=0)
    return KeepTogether([table, rule])


def payment_block(pay_par, qr_text, payload: str) -> Table:
    """Zahlungshinweis links, GiroCode rechts."""
    block = Table([[[pay_par, Spacer(0, 2 * mm), qr_text], QrFlowable(payload, 26 * mm)]],
                  colWidths=[CONTENT_W - 30 * mm, 30 * mm])
    block.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), FONT), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("ALIGN", (1, 0), (1, 0), "RIGHT"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    return block


def build(story: list, *, values: dict, logo: bytes | None, accent, recipient_name: str | None,
          recipient_address: str | None, info: list, title: str, draft: bool = False,
          watermark: str | None = None, page_class=None, label: str | None = None) -> bytes:
    """Setzt Seitenlayout (Briefkopf, Anschrift, Infoblock, Fußzeile) und Inhalt zusammen.
    ``page_class`` kommt aus der gewählten Vorlage (``pdf_designs``), Standard ist das klassische Briefpapier."""
    buf = io.BytesIO()
    page = (page_class or _Page)(values, recipient_name, recipient_address, logo, accent,
                                 watermark or ("ENTWURF" if draft else None), info)
    page.label = label
    bottom = page.content_bottom()
    pdf = BaseDocTemplate(buf, pagesize=A4, title=title, author=values.get("firma.name") or "",
                          leftMargin=LEFT, rightMargin=RIGHT, topMargin=20 * mm, bottomMargin=bottom)
    # Ohne Innenabstand, damit Tabellen (volle Breite) und Text exakt am linken Rand fluchten
    no_pad = dict(leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, showBoundary=0)
    first = Frame(LEFT, bottom, CONTENT_W, H - page.first_top - bottom, id="first", **no_pad)
    later = Frame(LEFT, bottom, CONTENT_W, H - 20 * mm - bottom, id="later", **no_pad)
    pdf.addPageTemplates([
        PageTemplate(id="First", frames=[first], onPage=page.first, autoNextPageTemplate="Later"),
        PageTemplate(id="Later", frames=[later], onPage=page.later),
    ])
    pdf.build(story)
    return buf.getvalue()


def render(db: Session, doc: Document, *, draft: bool = False, vorlage: str | None = None,
           watermark: str | None = None) -> bytes:
    """``vorlage``: abweichende Vorlage (z. B. Muster in den Einstellungen), sonst die eingestellte."""
    from app.services import pdf_vorlagen, rechnungen

    values, accent, logo = branding(db)
    design = pdf_vorlagen.design(values, "rechnung", vorlage)
    accent = design.accent(values, accent)
    st = design.styles(accent)
    storno = doc.kind == KIND_STORNO
    kind_label = "Stornorechnung" if storno else "Rechnung"

    info = [(f"{kind_label}snr." if not storno else "Storno-Nr.", doc.number or "wird vergeben"),
            ("Datum", format_date(doc.doc_date))]
    if doc.customer is not None:
        info.append(("Kundennr.", doc.customer.number))
    if doc.service_date and doc.service_until and doc.service_until != doc.service_date:
        info.append(("Leistungszeitraum", f"{format_date(doc.service_date)}–{format_date(doc.service_until)}"))
    elif doc.service_date:
        info.append(("Leistungsdatum", format_date(doc.service_date)))
    if storno and doc.storno_of is not None:
        info.append(("zu Rechnung", doc.storno_of.number))
    if "13b" in (doc.hints or []) and doc.customer is not None and doc.customer.ust_id:
        info.append(("Ihre USt-IdNr.", doc.customer.ust_id))

    story: list = []
    title = f"{kind_label} Nr. {doc.number}" if doc.number else f"{kind_label} (Entwurf)"
    story += design.title_flowables(title, st)
    story.append(Spacer(0, 3 * mm))
    if doc.object_address:
        story.append(Paragraph(f"<b>Leistungsort:</b> {_lines(doc.object_address).replace('<br/>', ', ')}", st["body"]))
        story.append(Spacer(0, 2 * mm))
    if doc.intro_text:
        story.append(Paragraph(_lines(doc.intro_text), st["body"]))
        story.append(Spacer(0, 4 * mm))

    # Positionen
    rates = {line.vat_rate for line in doc.lines}
    show_rate = len(rates) > 1
    show_discount = any(line.discount_percent for line in doc.lines)
    head = ["Pos.", "Bezeichnung", "Menge", "Einheit", "Einzelpreis"]
    widths = [12 * mm, None, 17 * mm, 19 * mm, 23 * mm]
    if show_discount:
        head.append("Rabatt")
        widths.append(17 * mm)
    if show_rate:
        head.append("USt")
        widths.append(11 * mm)
    head.append("Gesamt")
    widths.append(24 * mm)
    rows = []
    for line in doc.lines:
        text = f"<b>{_esc(line.title)}</b>"
        if line.description:
            text += f'<br/><font size="8" color="#555555">{_lines(line.description)}</font>'
        row = [Paragraph(str(line.position), st["cell"]), Paragraph(text, st["cell"]),
               Paragraph(_qty(line.quantity), st["cell_r"]), Paragraph(_esc(line.unit), st["cell_r"]),
               Paragraph(format_price(line.unit_price) + " €" if line.unit_price is not None else "", st["cell_r"])]
        if show_discount:
            row.append(Paragraph(format_percent(line.discount_percent) if line.discount_percent else "", st["cell_r"]))
        if show_rate:
            row.append(Paragraph(format_percent(line.vat_rate), st["cell_r"]))
        row.append(Paragraph(format_money(line.net), st["cell_r"]))
        rows.append(row)
    story.append(design.positions_table(head, widths, rows, accent))
    story.append(Spacer(0, 3 * mm))

    # Summen
    small_business = rechnungen.is_small_business(db)
    sums = []
    if not small_business:
        sums.append(("Summe netto", format_money(doc.net_total)))
        for g in rechnungen.tax_groups(doc):
            sums.append((f"zzgl. {format_percent(g.rate)} USt auf {format_money(g.net)}", format_money(g.vat)))
    story.append(design.sums_table(sums, "Gesamtbetrag", format_money(doc.gross_total), accent))
    story.append(Spacer(0, 5 * mm))

    for hint in rechnungen.hint_texts(db, doc):
        story.append(Paragraph(_esc(hint), st["body"]))
    if doc.hints or small_business:
        story.append(Spacer(0, 3 * mm))

    # Zahlung, GiroCode und Schlusstext stehen unten auf der Seite, direkt über der Fußzeile
    bottom: list = []
    if storno:
        if doc.notes:
            bottom.append(Paragraph(f"Grund: {_esc(doc.notes)}", st["body"]))
    elif doc.gross_total and doc.gross_total > 0:
        if doc.payment_terms_days:
            pay = (f"Bitte überweisen Sie den Betrag von <b>{format_money(doc.gross_total)}</b> bis zum "
                   f"<b>{format_date(doc.due_date)}</b> unter Angabe der Rechnungsnummer auf unten genanntes Konto.")
        else:
            pay = (f"Der Betrag von <b>{format_money(doc.gross_total)}</b> ist sofort fällig. Bitte überweisen Sie "
                   "unter Angabe der Rechnungsnummer auf unten genanntes Konto.")
        pay_par = Paragraph(pay, st["body"])
        iban = values.get("bank.iban")
        if doc.payment_qr == "girocode" and iban and doc.number:
            payload = girocode_payload(name=values.get("bank.kontoinhaber") or values.get("firma.name") or "",
                                       iban=iban, bic=values.get("bank.bic"), amount=doc.gross_total,
                                       reference=f"Rechnung {doc.number}")
            qr_text = Paragraph("<b>Bequem bezahlen:</b> QR-Code mit der Banking-App scannen (GiroCode).", st["small"])
            bottom.append(design.payment_block(pay_par, qr_text, payload))
        else:
            bottom.append(pay_par)
    if doc.closing_text:
        bottom += [Spacer(0, 4 * mm), Paragraph(_lines(doc.closing_text), st["body"])]
    if bottom:
        story.append(BottomBlock(bottom))

    return design.build(story, values=values, logo=logo, accent=accent, recipient_name=doc.recipient_name,
                        recipient_address=doc.recipient_address, info=info, title=title, draft=draft,
                        watermark=watermark, label=kind_label)
