"""Umsetzung der PDF-Vorlagen (Auswahl siehe ``pdf_vorlagen.py``).

Eine Vorlage bestimmt nur das Aussehen: Briefkopf, Anschrift, Infoblock und Fußzeile (``page_class``),
Schriftstile, Positions- und Summentabelle, Zahlungsblock. Inhalte und Pflichtangaben liefern die
render-Funktionen in ``pdf_rechnung``/``pdf_angebot``/``pdf_mahnung`` – für alle Vorlagen gleich.

Neue Vorlagen erben von ``Klassisch`` und überschreiben nur, was anders aussieht. Regeln (Test
``test_pdf_a3_merkmale``, ``test_pdf_layout``): nur die Schriften ``FONT``/``FONT_BOLD``, jede ``TableStyle`` mit
``("FONTNAME", …, FONT)``, Texte auf festen Positionen mit ``wrap_text``/``fit_text``, Anschriftfeld für
Fensterumschläge (DIN 5008) beibehalten.

Die Vorlagen „Marine“ bis „Terrakotta“ (``PAKETE``) stammen aus den Claude-Design-Entwürfen
„Rechnungs-, Angebots- & Mahnvorlagen“ (A4 = 794 × 1123 px). Sie teilen ein Grundgerüst (``PaketSeite``) und
unterscheiden sich in Kopf, Tabellenkopf, Summenzeile, Fußzeile und Farbe (``Stil``). Statt der Web-Schriften
der Entwürfe wird die eingebettete Schrift Vera verwendet (PDF/A).
"""
import io
from dataclasses import dataclass
from functools import partial

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Flowable, KeepTogether, Paragraph, Spacer, Table, TableStyle

from app.services import pdf_rechnung as base
from app.services.pdf_rechnung import CONTENT_W, FONT, FONT_BOLD, LEFT, RIGHT, H, W, _esc, fit_text, wrap_text
from app.services.pruefungen import format_iban

FONT_ITALIC = "Vera-Italic"


class Klassisch:
    page_class = base._Page

    def accent(self, values: dict, accent):
        """Farbe der Belege – hier die Akzentfarbe aus den Einstellungen."""
        return accent

    def styles(self, accent) -> dict:
        return base._styles(accent)

    def title_flowables(self, title: str, st: dict) -> list:
        return [Paragraph(_esc(title), st["title"])]

    def positions_table(self, head, widths, rows, accent):
        return base.positions_table(head, widths, rows, accent)

    def sums_table(self, rows, total_label, total_value, accent):
        return base.sums_table(rows, total_label, total_value, accent)

    def payment_block(self, pay_par, qr_text, payload):
        return base.payment_block(pay_par, qr_text, payload)

    def acceptance_block(self, st: dict, accent):
        """Feld „Auftrag erteilt“ (Ort, Datum, Unterschrift) unter dem Angebot – im Klassisch-Layout nicht."""
        return None

    def build(self, story: list, **kwargs) -> bytes:
        return base.build(story, page_class=self.page_class, **kwargs)


# ======================================================== Vorlagen-Pakete
def px(value: float) -> float:
    """Maße der Entwürfe (A4 = 794 px breit) in PDF-Punkte."""
    return value * W / 794


def top(value: float) -> float:
    """Abstand vom oberen Rand (px) als y-Koordinate."""
    return H - px(value)


TEXT = colors.HexColor("#1F1F1F")
GREY = colors.HexColor("#5A5A5A")
FOOT_GREY = colors.HexColor("#4A4A4A")
RULE_GREY = colors.HexColor("#9A9A9A")


def tint(color, share: float = 0.078):
    """Heller Farbton (wie „Farbe + 14“ = 8 % Deckkraft im Entwurf), als feste Farbe ohne Transparenz."""
    return colors.Color(1 - (1 - color.red) * share, 1 - (1 - color.green) * share, 1 - (1 - color.blue) * share)


@dataclass(frozen=True)
class Stil:
    farbe: str
    # Kopf: logo_rechts | band (Farbband + Belegart rechts) | zentriert (Doppellinie) | kursiv (Belegart
    # kursiv + Linie) | briefkopf (Firmenangaben rechts) | streifen (senkrechter Farbstreifen rechts)
    kopf: str = "logo_rechts"
    logo: tuple[float, float, float, float] = (478, 48, 240, 90)  # x, y, Breite, Höhe in px
    titel_px: float = 20
    titel_farbig: bool = True
    titel_label: bool = False          # Belegart klein über „Nr. …“ (Anthrazit)
    info_kasten: bool = False          # Infoblock in getöntem Kasten (Pflaume)
    info_versal: bool = False          # Bezeichnungen im Infoblock in Großbuchstaben (Anthrazit)
    # Tabellenkopf: getoent | linie | doppellinie (oben + unten) | voll (Farbfläche, weiße Schrift)
    tabellenkopf: str = "getoent"
    kopf_linie: float = 2              # px
    kopf_oben: float = 1               # px, nur „doppellinie“
    kopf_versal: bool = False
    zeilenlinie: str = "#D9D9D9"
    zebra: bool = False                # jede zweite Zeile getönt, keine Zeilenlinien (Terrakotta)
    # Summenzeile: linie | balken (Farbfläche) | getoent | doppelt (doppelte Linie unten) | rahmen (oben + unten)
    summe: str = "linie"
    summe_linie: float = 2             # px
    summe_farbig: bool = True          # Gesamtbetrag in der Farbe
    summe_linie_dunkel: bool = False
    # Fußzeile: linie | band (Farbfläche, weiße Schrift) | band_getoent
    fuss: str = "linie"
    fuss_linie: float = 1              # px
    fuss_linienfarbe: str = "#D9D9D9"  # "akzent" = Farbe, "doppelt" = doppelte Linie in Farbe
    fuss_titel: bool = False           # Spaltenüberschriften (Anschrift, Kontakt, …)
    fuss_zentriert: bool = False
    fuss_erste_fett: bool = False      # erste Zeile (Firmenname) fett
    fuss_erste_farbig: bool = False
    auftrag: str = "fett"              # Schreibweise „Auftrag erteilt“: fett | versal | kursiv


class PaketSeite(base._Page):
    """Gemeinsames Briefpapier der Vorlagen-Pakete: DIN-5008-Anschriftfeld (45 mm von oben, 25 mm von links),
    Infoblock rechts, Kopf und Fußzeile je nach ``Stil``."""

    first_top = px(372)

    def __init__(self, *args, stil: Stil, **kwargs):
        super().__init__(*args, **kwargs)
        self.stil = stil

    # ------------------------------------------------------------ Seite 1
    def first(self, canv, pdf):
        self._header(canv)
        self._address(canv)
        self._info_block(canv)
        self.later(canv, pdf)

    def later(self, canv, pdf):
        if self.stil.kopf == "streifen":
            canv.saveState()
            canv.setFillColor(self.accent)
            canv.rect(W - px(10), 0, px(10), H, stroke=0, fill=1)
            canv.restoreState()
        super().later(canv, pdf)

    def _logo_or_name(self, canv, box, align: str) -> None:
        """Logo in den Kasten einpassen; ohne Logo steht dort der Firmenname."""
        x, y, w, h = px(box[0]), top(box[1] + box[3]), px(box[2]), px(box[3])
        if self.logo:
            try:
                img = ImageReader(io.BytesIO(self.logo))
                iw, ih = img.getSize()
                scale = min(w / iw, h / ih)
                lw, lh = iw * scale, ih * scale
                lx = {"links": x, "mitte": x + (w - lw) / 2}.get(align, x + w - lw)
                canv.drawImage(img, lx, y + (h - lh) / 2, lw, lh, mask="auto")
                return
            except Exception:  # noqa: BLE001 – kaputtes Logo darf den Beleg nicht verhindern
                self.logo = None
        name = self.v.get("firma.name") or ""
        lines, size = base.name_lines(name, w)
        canv.setFillColor(self.accent)
        canv.setFont(FONT_BOLD, size)
        leading = size * 1.2
        ty = y + h / 2 + (len(lines) - 1) * leading / 2 - size * 0.35
        for line in lines:
            if align == "links":
                canv.drawString(x, ty, line)
            elif align == "mitte":
                canv.drawCentredString(x + w / 2, ty, line)
            else:
                canv.drawRightString(x + w, ty, line)
            ty -= leading

    def _label(self, canv, text: str, *, font: str, size: float, y: float, spacing: float = 0) -> None:
        """Belegart rechtsbündig im Kopf (z. B. „RECHNUNG“), passend auf höchstens 100 mm."""
        text, size = fit_text(text, font, size, 100 * mm - spacing * len(text), min_size=10)
        width = base._width(text, font, size) + spacing * max(len(text) - 1, 0)
        t = canv.beginText(W - px(76) - width, y)
        t.setFont(font, size)
        t.setCharSpace(spacing)
        t.setFillColor(self.accent)
        t.textLine(text)
        canv.drawText(t)

    def _header(self, canv):
        s, label = self.stil, (self.label or "")
        canv.saveState()
        if s.kopf == "band":
            canv.setFillColor(self.accent)
            canv.rect(0, H - px(8), W, px(8), stroke=0, fill=1)
            self._logo_or_name(canv, s.logo, "links")
            self._label(canv, label.upper(), font=FONT_BOLD, size=px(24), y=top(58 + 24), spacing=px(24) * 0.14)
        elif s.kopf == "zentriert":
            self._logo_or_name(canv, s.logo, "mitte")
            canv.setStrokeColor(self.accent)
            canv.setLineWidth(px(1))
            for y in (138, 141):
                canv.line(LEFT, top(y), W - RIGHT, top(y))
        elif s.kopf == "kursiv":
            self._logo_or_name(canv, s.logo, "links")
            self._label(canv, label, font=FONT_ITALIC, size=px(28), y=top(98 + 28))
            canv.setStrokeColor(self.accent)
            canv.setLineWidth(px(2))
            canv.line(LEFT, top(148), W - RIGHT, top(148))
        elif s.kopf == "briefkopf":
            if self.logo:  # ohne Logo steht der Name nur einmal – im Briefkopf rechts
                self._logo_or_name(canv, s.logo, "links")
            self._letterhead(canv)
        elif s.kopf == "streifen":
            self._logo_or_name(canv, s.logo, "links")
        else:
            self._logo_or_name(canv, s.logo, "rechts")
        canv.restoreState()

    def _letterhead(self, canv):
        """Firmenangaben rechts oben (Stahlblau): Name, Anschrift, Telefon, E-Mail · Website."""
        v, right, width = self.v, W - px(76), px(280)
        name = v.get("firma.name") or ""
        size = px(16)
        while size > 9 and base._width(name, FONT_BOLD, size) > width:
            size -= 0.5
        names = wrap_text(name, FONT_BOLD, size, width)[:3]  # sehr lange Namen umbrechen statt kürzen
        canv.setFillColor(self.accent)
        canv.setFont(FONT_BOLD, size)
        y = top(44 + 16) + px(17) * (len(names) - 1) / 2
        for line in names:
            canv.drawRightString(right, y, line)
            y -= size * 1.2
        y += size * 1.2
        lines = [v.get("firma.strasse"), f"{v.get('firma.plz') or ''} {v.get('firma.ort') or ''}".strip(),
                 f"Tel. {v['firma.telefon']}" if v.get("firma.telefon") else None,
                 " · ".join(x for x in (v.get("firma.email"), v.get("firma.website")) if x)]
        canv.setFillColor(FOOT_GREY)
        for line in (x for x in lines if x):
            text, size = fit_text(line, FONT, px(12), width, min_size=6.5)
            canv.setFont(FONT, size)
            y -= px(17)
            canv.drawRightString(right, y, text)

    def _address(self, canv):
        """Anschriftfeld nach DIN 5008 Form B: Rücksendeangabe unten in der Vermerkzone, darunter die Anschrift."""
        canv.saveState()
        x, window = px(94), px(303)
        sender = " · ".join(x for x in (self.v.get("firma.name"), self.v.get("firma.strasse"),
                                         f"{self.v.get('firma.plz') or ''} {self.v.get('firma.ort') or ''}".strip()) if x)
        sender, size = fit_text(sender, FONT, px(12), window, min_size=5.5)
        base_y = top(170 + 67 - 8) + px(3)
        canv.setFont(FONT, size)
        canv.setFillColor(GREY)
        canv.drawString(x, base_y, sender)
        canv.setStrokeColor(RULE_GREY)
        canv.setLineWidth(0.5)
        canv.line(x, base_y - px(3), x + base._width(sender, FONT, size), base_y - px(3))
        canv.setFillColor(TEXT)
        raw = [self.recipient_name or ""] + str(self.recipient_address or "").splitlines()
        for size, leading in ((px(14), px(20)), (px(13), px(17)), (px(12), px(15))):
            lines = [part for line in raw for part in wrap_text(line, FONT, size, window)]
            if len(lines) * leading <= px(170 - 67):  # Anschriftzone (27,3 mm)
                break
        canv.setFont(FONT, size)
        y = top(170 + 67) - size
        for line in lines[:7]:
            canv.drawString(x, y, line)
            y -= leading
        canv.restoreState()

    def _info_block(self, canv):
        s = self.stil
        canv.saveState()
        x0, x1 = px(472), px(718)
        size, leading, gap = px(13), px(20), px(12)
        rows = []
        for label, value in self.info:
            lsize = px(12) if s.info_versal else size
            text = label.upper() if s.info_versal else label
            text, lsize = fit_text(text, FONT, lsize, px(110), min_size=6)
            bold = s.info_kasten and label.endswith("nr.") and label.startswith(("Rechnung", "Angebot", "Mahnung"))
            font = FONT_BOLD if bold else FONT
            avail = (x1 - x0) - base._width(text, FONT, lsize) - gap
            rows.append((text, lsize, font, wrap_text(str(value or ""), font, size, avail)[:3], bold))
        height = sum(len(r[3]) or 1 for r in rows) * leading
        y = top(189) - size
        if s.info_kasten:  # Kasten 260 px breit, 12/16 px Innenabstand
            canv.setFillColor(tint(self.accent))
            box_h = height + px(24)
            canv.roundRect(px(458), top(178) - box_h, px(260), box_h, px(4), stroke=0, fill=1)
            x0, x1 = px(474), px(702)
        for text, lsize, font, values, bold in rows:
            canv.setFont(FONT, lsize)
            canv.setFillColor(GREY)
            canv.drawString(x0, y, text)
            canv.setFont(font, size)
            canv.setFillColor(self.accent if bold else TEXT)
            for line in values or [""]:
                canv.drawRightString(x1, y, line)
                y -= leading
        canv.restoreState()

    # ---------------------------------------------------------- Fußzeile
    def _footer_columns(self) -> list[tuple[str | None, list[str]]]:
        v = self.v
        bank = [v.get("bank.name"), f"IBAN {format_iban(v['bank.iban'])}" if v.get("bank.iban") else None,
                f"BIC {v['bank.bic']}" if v.get("bank.bic") else None]
        tax = [f"St.-Nr. {v['steuer.steuernummer']}" if v.get("steuer.steuernummer") else None,
               f"USt-IdNr. {v['steuer.ust_id']}" if v.get("steuer.ust_id") else None]
        owner = f"Inhaber/GF: {v['firma.inhaber']}" if v.get("firma.inhaber") else None
        if self.stil.kopf == "briefkopf":  # Anschrift und Kontakt stehen schon im Briefkopf
            return [("Bankverbindung", bank), ("Steuerangaben", tax), ("Unternehmen", [owner, v.get("firma.handelsregister")])]
        return [("Anschrift", [v.get("firma.name"), v.get("firma.strasse"),
                               f"{v.get('firma.plz') or ''} {v.get('firma.ort') or ''}".strip()]),
                ("Kontakt", [f"Tel. {v['firma.telefon']}" if v.get("firma.telefon") else None, v.get("firma.email"),
                             v.get("firma.website")]),
                ("Bankverbindung", bank),
                ("Unternehmen", [owner, *tax, v.get("firma.handelsregister")])]

    def footer_layout(self) -> dict:
        s = self.stil
        columns = [(heading, [str(t) for t in texts if t]) for heading, texts in self._footer_columns()]
        gap = px(16 if len(columns) == 3 else 12)
        size, leading = FOOT_SIZE, FOOT_SIZE * 1.42
        # Spaltenbreiten nach Inhalt (z. B. braucht die IBAN mehr Platz als die Telefonnummer)
        avail = CONTENT_W - gap * (len(columns) - 1)
        natural = [max([base._width(t, FONT, size) for t in texts] + [base._width(h, FONT_BOLD, size)], default=1)
                   for h, texts in columns]
        even = avail / len(columns)
        widths = [min(n, even) for n in natural]
        wide = [i for i, n in enumerate(natural) if n > even]
        spare = avail - sum(widths)
        for i in wide:  # Restplatz auf die zu breiten Spalten verteilen
            widths[i] += spare / len(wide)
        if not wide:
            widths = [even] * len(columns)

        def lines_of(text: str, col_w: float, font: str = FONT) -> list[tuple[str, float]]:
            fitted, fsize = fit_text(text, font, size, col_w, min_size=FOOT_MIN_SIZE)
            if not fitted.endswith("…") or text.endswith("…"):
                return [(fitted, fsize)]
            return [(part, size) for part in wrap_text(text, font, size, col_w)]

        # Der Firmenname (erste Zeile der ersten Spalte) ist je nach Vorlage fett – breiter messen
        firm_bold = not s.fuss_titel and (s.fuss_erste_fett or s.fuss_erste_farbig)
        wrapped = []
        for col, ((heading, texts), col_w) in enumerate(zip(columns, widths, strict=True)):
            body = []
            for n, text in enumerate(texts):
                bold = firm_bold and col == 0 and n == 0
                parts = lines_of(text, col_w, FONT_BOLD if bold else FONT)
                body += [(line, fsize, bold) for line, fsize in parts]
            wrapped.append(([(heading, size, True)] if s.fuss_titel else []) + body)
        count = max((len(c) for c in wrapped), default=1) or 1
        last_line = (13 if self.v.get("design.fusszeile") else 10) * mm
        first_line = last_line + (count - 1) * leading
        rule = first_line + size + px(12)
        band = first_line + size + px(25) if s.fuss.startswith("band") else None
        edge = band or rule
        return {"columns": wrapped, "widths": widths, "gap": gap, "first_line": first_line, "leading": leading,
                "rule": rule, "band": band, "content_bottom": max(32 * mm, edge + 8 * mm)}

    def content_bottom(self) -> float:
        return self.footer_layout()["content_bottom"]

    def _footer(self, canv, page_no):
        s, layout = self.stil, self.footer_layout()
        canv.saveState()
        light = s.fuss == "band"
        if layout["band"] is not None:
            canv.setFillColor(self.accent if light else tint(self.accent))
            canv.rect(0, 0, W, layout["band"], stroke=0, fill=1)
        else:
            color = colors.HexColor(s.fuss_linienfarbe) if s.fuss_linienfarbe.startswith("#") else self.accent
            canv.setStrokeColor(color)
            canv.setLineWidth(px(s.fuss_linie))
            canv.line(LEFT, layout["rule"], W - RIGHT, layout["rule"])
            if s.fuss_linienfarbe == "doppelt":
                canv.line(LEFT, layout["rule"] + px(3), W - RIGHT, layout["rule"] + px(3))
        x = LEFT
        for col, (lines, col_w) in enumerate(zip(layout["columns"], layout["widths"], strict=True)):
            y = layout["first_line"]
            for n, (line, size, emphasis) in enumerate(lines):
                # Überschriften in jeder Spalte; sonst ist nur der Firmenname (erste Spalte) hervorgehoben
                heading = n == 0 and s.fuss_titel
                firm = emphasis and not heading
                canv.setFont(FONT_BOLD if emphasis else FONT, size)
                if light:
                    canv.setFillColor(colors.white)
                elif heading or (firm and s.fuss_erste_farbig):
                    canv.setFillColor(self.accent)
                else:
                    canv.setFillColor(FOOT_GREY)
                if s.fuss_zentriert:
                    canv.drawCentredString(x + col_w / 2, y, line)
                else:
                    canv.drawString(x, y, line)
                y -= layout["leading"]
            x += col_w + layout["gap"]
        canv.setFillColor(colors.white if light else FOOT_GREY)
        if self.v.get("design.fusszeile"):
            line, size = fit_text(str(self.v["design.fusszeile"]).splitlines()[0], FONT, FOOT_SIZE, CONTENT_W)
            canv.setFont(FONT, size)
            canv.drawCentredString(W / 2, 6 * mm, line)
        canv.setFillColor(FOOT_GREY)
        canv.setFont(FONT, 6.8)
        canv.drawRightString(W - RIGHT, (layout["band"] or layout["rule"]) + 3 * mm, f"Seite {page_no}")
        canv.restoreState()


FOOT_SIZE = 7.6
FOOT_MIN_SIZE = 6.3


class _Signature(Flowable):
    """„Auftrag erteilt“ mit zwei Unterschriftslinien (Ort, Datum | Unterschrift / Stempel), rechtsbündig."""

    def __init__(self, stil: Stil, accent):
        super().__init__()
        self.stil, self.accent = stil, accent
        self.width, self.height = px(290), px(13 + 30 + 4 + 16)

    def wrap(self, avail_width, avail_height):
        self._x = avail_width - self.width
        return avail_width, self.height

    def draw(self):
        c, s = self.canv, self.stil
        text, font = "Auftrag erteilt", FONT_BOLD
        if s.auftrag == "versal":
            text = text.upper()
        elif s.auftrag == "kursiv":
            font = FONT_ITALIC
        c.setFillColor(self.accent)
        c.setFont(font, px(14) if s.auftrag == "kursiv" else px(12.5))
        c.drawString(self._x, self.height - px(13), text)
        line_y = px(18)
        widths = (self.width - px(16)) / 2.4
        c.setStrokeColor(GREY)
        c.setLineWidth(px(1))
        c.setFillColor(GREY)
        c.setFont(FONT, px(12))
        x = self._x
        for label, w in (("Ort, Datum", widths), ("Unterschrift / Stempel", widths * 1.4)):
            c.line(x, line_y, x + w, line_y)
            c.drawString(x, line_y - px(14), label)
            x += w + px(16)


class Paket(Klassisch):
    """Eine Vorlage aus den Claude-Design-Paketen; Aussehen über ``stil``."""

    stil: Stil

    @property
    def page_class(self):
        return partial(PaketSeite, stil=self.stil)

    def accent(self, values: dict, accent):
        # Standard: die Farbe der Vorlage; auf Wunsch die eigene Akzentfarbe aus den Einstellungen
        return accent if values.get("design.vorlage_farbe") == "akzent" else colors.HexColor(self.stil.farbe)

    def styles(self, accent) -> dict:
        s = self.stil
        base_style = dict(fontName=FONT, fontSize=px(14), leading=px(21), textColor=TEXT)
        cell = dict(base_style, fontSize=px(13), leading=px(17))
        return {
            "title": ParagraphStyle("title", fontName=FONT_BOLD, fontSize=px(s.titel_px), leading=px(s.titel_px) * 1.3,
                                    textColor=accent if s.titel_farbig else TEXT),
            "label": ParagraphStyle("label", fontName=FONT, fontSize=px(12), leading=px(16), textColor=GREY),
            "body": ParagraphStyle("body", **base_style),
            "small": ParagraphStyle("small", **{**base_style, "fontSize": px(12), "leading": px(16), "textColor": GREY}),
            "cell": ParagraphStyle("cell", **cell),
            "cell_r": ParagraphStyle("cell_r", **cell, alignment=2),
            "sum": ParagraphStyle("sum", **{**cell, "alignment": 2}),
        }

    def title_flowables(self, title: str, st: dict) -> list:
        if self.stil.titel_label and " Nr. " in title:
            kind, number = title.rsplit(" Nr. ", 1)
            label = ParagraphStyle("label_versal", parent=st["label"])
            return [Paragraph(_esc(kind.upper()), label), Spacer(0, px(4)), Paragraph(_esc(f"Nr. {number}"), st["title"])]
        return [Paragraph(_esc(title), st["title"])]

    def positions_table(self, head, widths, rows, accent):
        s = self.stil
        widths = list(widths)
        widths[widths.index(None)] = CONTENT_W - sum(w for w in widths if w)
        head_color = colors.white if s.tabellenkopf == "voll" else accent
        head_row = []
        for i, (h, w) in enumerate(zip(head, widths, strict=True)):
            label = h.upper() if s.kopf_versal else h
            # Großbuchstaben sind breiter: Schrift so weit verkleinern, dass z. B. „EINZELPREIS“ in die Spalte passt
            _, size = fit_text(label, FONT_BOLD, px(12), w - px(12), min_size=6)
            style = ParagraphStyle(f"head{i}", fontName=FONT_BOLD, fontSize=size, leading=px(15), textColor=head_color,
                                   alignment=2 if i >= 2 else 0)
            head_row.append(Paragraph(_esc(label), style))
        table = Table([head_row] + rows, colWidths=widths, repeatRows=1)
        style = [("FONTNAME", (0, 0), (-1, -1), FONT), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("TOPPADDING", (0, 0), (-1, 0), px(8)), ("BOTTOMPADDING", (0, 0), (-1, 0), px(8)),
                 ("TOPPADDING", (0, 1), (-1, -1), px(9)), ("BOTTOMPADDING", (0, 1), (-1, -1), px(9)),
                 ("LEFTPADDING", (0, 0), (-1, -1), px(6)), ("RIGHTPADDING", (0, 0), (-1, -1), px(6))]
        if s.tabellenkopf == "getoent":
            style.append(("BACKGROUND", (0, 0), (-1, 0), tint(accent)))
        elif s.tabellenkopf == "voll":
            style.append(("BACKGROUND", (0, 0), (-1, 0), accent))
        else:
            style.append(("LINEBELOW", (0, 0), (-1, 0), px(s.kopf_linie), accent))
            if s.tabellenkopf == "doppellinie":
                style.append(("LINEABOVE", (0, 0), (-1, 0), px(s.kopf_oben), accent))
        if s.zebra:
            style += [("BACKGROUND", (0, r), (-1, r), tint(accent)) for r in range(2, len(rows) + 1, 2)]
            style.append(("LINEBELOW", (0, -1), (-1, -1), px(1), colors.HexColor(s.zeilenlinie)))
        elif rows:
            style.append(("LINEBELOW", (0, 1), (-1, -1), px(1), colors.HexColor(s.zeilenlinie)))
        table.setStyle(TableStyle(style))
        return table

    def sums_table(self, rows, total_label, total_value, accent):
        s = self.stil
        st = self.styles(accent)
        total_color = colors.white if s.summe == "balken" else (accent if s.summe_farbig else TEXT)
        total = ParagraphStyle("total", parent=st["sum"], fontName=FONT_BOLD, fontSize=px(15), leading=px(19),
                               textColor=total_color)
        data = [[Paragraph(_esc(a), st["sum"]), Paragraph(_esc(b), st["sum"])] for a, b in rows]
        data.append([Paragraph(_esc(total_label), total), Paragraph(_esc(total_value), total)])
        # Breiter als im Entwurf (290 px), damit „zzgl. 19 % USt auf …“ in eine Zeile passt
        table = Table(data, colWidths=[64 * mm, 30 * mm], hAlign="RIGHT")
        pad = px(10) if s.summe in ("balken", "getoent") else 0
        style = [("FONTNAME", (0, 0), (-1, -1), FONT), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                 ("TOPPADDING", (0, 0), (-1, -1), px(2)), ("BOTTOMPADDING", (0, 0), (-1, -1), px(2)),
                 ("LEFTPADDING", (0, 0), (-1, -1), pad), ("RIGHTPADDING", (0, 0), (-1, -1), pad),
                 ("TOPPADDING", (0, -1), (-1, -1), px(5)), ("BOTTOMPADDING", (0, -1), (-1, -1), px(5))]
        line_color = TEXT if s.summe_linie_dunkel else accent
        if s.summe == "balken":
            style.append(("BACKGROUND", (0, -1), (-1, -1), accent))
        elif s.summe == "getoent":
            style.append(("BACKGROUND", (0, -1), (-1, -1), tint(accent)))
        else:
            style.append(("LINEABOVE", (0, -1), (-1, -1), px(s.summe_linie), line_color))
            if s.summe == "rahmen":
                style.append(("LINEBELOW", (0, -1), (-1, -1), px(1), accent))
            elif s.summe == "doppelt":
                style.append(("LINEBELOW", (0, -1), (-1, -1), px(1), accent, None, None, None, 2, px(2)))
        table.setStyle(TableStyle(style))
        return KeepTogether([table])

    def acceptance_block(self, st: dict, accent):
        return _Signature(self.stil, accent)


def _paket(key: str, stil: Stil) -> type[Paket]:
    return type(f"Paket_{key}", (Paket,), {"stil": stil})


# Maße und Farben aus den Entwürfen (px bei 794 px Seitenbreite)
PAKETE: dict[str, Stil] = {
    "marine": Stil("#1F3A5F"),
    "petrol": Stil("#0F5257", kopf="band", logo=(94, 44, 220, 90), titel_px=19, titel_farbig=False,
                   tabellenkopf="linie", kopf_versal=True, zeilenlinie="#DADDDD", summe="balken",
                   fuss_linie=2, fuss_linienfarbe="akzent", auftrag="versal"),
    "bordeaux": Stil("#6B1E2E", kopf="zentriert", logo=(277, 32, 240, 84), titel_px=23, tabellenkopf="doppellinie",
                     kopf_linie=1, kopf_versal=True, zeilenlinie="#E0DADB", summe="doppelt", summe_linie=1,
                     summe_farbig=False, summe_linie_dunkel=True, fuss_linienfarbe="doppelt", fuss_zentriert=True),
    "anthrazit": Stil("#2E3338", titel_px=24, titel_label=True, info_versal=True, tabellenkopf="linie", kopf_linie=1,
                      kopf_versal=True, zeilenlinie="#E2E3E4", summe_linie=1, fuss_linienfarbe="#C9CBCD",
                      auftrag="versal"),
    "tannengruen": Stil("#2F5D3A", titel_px=21, zeilenlinie="#DCE3DD", summe="getoent", fuss="band",
                        fuss_erste_fett=True),
    "indigo": Stil("#33407A", titel_px=22, tabellenkopf="voll", zeilenlinie="#DDDFEA", summe_linie=1,
                   fuss_linienfarbe="akzent", fuss_titel=True),
    "bronze": Stil("#7A4E1A", kopf="kursiv", logo=(94, 44, 220, 90), titel_px=22, titel_farbig=False,
                   tabellenkopf="doppellinie", kopf_oben=2, kopf_linie=1, zeilenlinie="#E6DED4", summe_linie=1,
                   fuss_linienfarbe="akzent", auftrag="kursiv"),
    "pflaume": Stil("#5B2D57", logo=(478, 40, 240, 84), titel_px=21, info_kasten=True, tabellenkopf="linie",
                    zeilenlinie="#E5DDE4", fuss="band_getoent", fuss_erste_fett=True, fuss_erste_farbig=True),
    "stahlblau": Stil("#2C5878", kopf="briefkopf", logo=(94, 44, 220, 90), tabellenkopf="linie", kopf_linie=1,
                      zeilenlinie="#DCE3E9", summe="rahmen", summe_linie=1, fuss_linienfarbe="#C9D3DB",
                      fuss_titel=True),
    "terrakotta": Stil("#8C3B22", kopf="streifen", logo=(94, 44, 220, 90), titel_px=21, tabellenkopf="linie",
                       zebra=True, summe_linie=2, summe_linie_dunkel=True, fuss_erste_fett=True),
}

DESIGNS: dict[str, type[Klassisch]] = {
    "klassisch": Klassisch,
    **{key: _paket(key, stil) for key, stil in PAKETE.items()},
}
