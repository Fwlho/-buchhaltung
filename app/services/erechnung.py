"""E-Rechnung: ZUGFeRD/Factur-X und XRechnung lesen und schreiben (KONZEPT.md 6.2/6.5).

Lesen (Eingangsrechnungen): XML im CII-Format (ZUGFeRD, Factur-X, XRechnung-CII) oder UBL (XRechnung-UBL),
als eigene Datei oder eingebettet in ein PDF. Die Werte gelten als exakt – nichts wird „geraten“.
Sicherheit: Fremdes XML wird mit defusedxml gelesen (keine Entitäten/DTD -> keine XML-Bomben).

Schreiben (Ausgangsrechnungen): XML im CII-Format nach EN 16931.
- ZUGFeRD (Standard): XML eingebettet in das Rechnungs-PDF (PDF/A-3).
- XRechnung (für Behörden, Leitweg-ID): eigene XML-Datei, zusätzlich zum PDF.
Prüfung: XSD-Schema (offline, mit der Bibliothek factur-x) plus eigene Prüfung der wichtigsten
Geschäftsregeln. Die Schematron-Prüfung der Bibliothek würde Daten an einen Prüfserver schicken und wird
deshalb bewusst NICHT verwendet.
"""
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from xml.etree.ElementTree import Element, SubElement, register_namespace, tostring

from defusedxml.ElementTree import fromstring as safe_fromstring
from sqlalchemy.orm import Session

from app.formatting import round_money
from app.models import Document
from app.models.belege import KIND_STORNO
from app.services import auslese, einstellungen
from app.services.betraege import TaxLine, complete_line, totals

# --------------------------------------------------------------- Namensräume
RSM = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
RAM = "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
UDT = "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100"
QDT = "urn:un:unece:uncefact:data:standard:QualifiedDataType:100"
UBL_INV = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
UBL_CN = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
NS = {"rsm": RSM, "ram": RAM, "udt": UDT, "qdt": QDT, "cbc": CBC, "cac": CAC}
for _prefix in ("rsm", "ram", "udt", "qdt"):
    register_namespace(_prefix, NS[_prefix])  # übliche Kürzel statt ns0/ns1 im erzeugten XML

GUIDELINE_EN16931 = "urn:cen.eu:en16931:2017"
GUIDELINE_XRECHNUNG = "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"
BUSINESS_PROCESS = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"

FORMATS = {"zugferd": "ZUGFeRD (PDF mit eingebetteten Daten)", "xrechnung": "XRechnung (XML-Datei)",
           "keine": "keine E-Rechnung (nur PDF)"}

# Einheiten nach UN/ECE Recommendation 20
UNIT_CODES = {"Stück": "H87", "Std.": "HUR", "m²": "MTK", "m": "MTR", "m³": "MTQ", "kg": "KGM", "l": "LTR",
              "km": "KMT", "Tag": "DAY", "Monat": "MON", "pauschal": "LS"}
UNIT_NAMES = {code: name for name, code in UNIT_CODES.items()} | {"C62": "Stück", "XPP": "Stück", "MIN": "Min."}
COUNTRY_CODES = {"deutschland": "DE", "germany": "DE", "österreich": "AT", "oesterreich": "AT", "schweiz": "CH",
                 "frankreich": "FR", "niederlande": "NL", "belgien": "BE", "luxemburg": "LU", "polen": "PL",
                 "dänemark": "DK", "italien": "IT", "spanien": "ES", "tschechien": "CZ"}
KU_REASON = "Kleinunternehmer gemäß § 19 UStG"
RC_REASON = "Steuerschuldnerschaft des Leistungsempfängers (§ 13b UStG)"
MAX_XML_BYTES = 5 * 1024 * 1024


class EInvoiceError(ValueError):
    pass


# ======================================================================= LESEN
@dataclass
class Item:
    name: str
    description: str = ""
    quantity: Decimal | None = None
    unit: str = ""
    price: Decimal | None = None
    net: Decimal | None = None
    rate: Decimal | None = None


@dataclass
class Parsed:
    format: str                       # "CII" oder "UBL"
    result: auslese.Result
    items: list[Item] = field(default_factory=list)
    seller_vat: str | None = None
    seller_iban: str | None = None
    buyer_name: str | None = None
    buyer_reference: str | None = None
    credit_note: bool = False
    payment_terms: str | None = None
    totals: dict[str, Decimal] = field(default_factory=dict)

    @property
    def payment_terms_readable(self) -> str | None:
        """XRechnung-Skonto-Kürzel (#SKONTO#TAGE=10#PROZENT=2.00#) in Klartext."""
        if not self.payment_terms:
            return None

        def repl(m: re.Match) -> str:
            percent = m.group(2).rstrip("0").rstrip(".").replace(".", ",")
            return f"{percent} % Skonto bei Zahlung innerhalb von {m.group(1)} Tagen. "

        return re.sub(r"#SKONTO#TAGE=(\d+)#PROZENT=(\d+(?:\.\d+)?)#(?:BASISBETRAG=[^#]*#)?", repl,
                      self.payment_terms).strip()


def _dec(text: str | None) -> Decimal | None:
    if text is None or not text.strip():
        return None
    try:
        return Decimal(text.strip())
    except InvalidOperation:
        return None


def _date(text: str | None) -> date | None:
    if not text:
        return None
    text = text.strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10] if "-" in text else text[:8], fmt).date()
        except ValueError:
            continue
    return None


def _text(node, path: str) -> str | None:
    found = node.find(path, NS) if node is not None else None
    return found.text.strip() if found is not None and found.text else None


def xml_from_pdf(data: bytes) -> bytes | None:
    """Eingebettetes Rechnungs-XML (factur-x.xml, zugferd-invoice.xml, xrechnung.xml …) aus einem PDF."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        attachments = reader.attachments
    except Exception:  # noqa: BLE001 – kaputte PDFs dürfen den Upload nicht verhindern
        return None
    preferred = ("factur-x.xml", "zugferd-invoice.xml", "xrechnung.xml", "ZUGFeRD-invoice.xml")
    names = sorted(attachments, key=lambda n: (n not in preferred, n))
    for name in names:
        if not name.lower().endswith(".xml"):
            continue
        for content in attachments[name]:
            if len(content) <= MAX_XML_BYTES and looks_like_invoice(content):
                return content
    return None


def looks_like_invoice(data: bytes) -> bool:
    head = data[:2000]
    return any(ns.encode() in head for ns in (RSM, UBL_INV, UBL_CN))


def parse(data: bytes, partners: list[auslese.Partner] | None = None,
          own: auslese.OwnCompany | None = None) -> Parsed:
    """Rechnungs-XML (CII oder UBL) lesen. Wirft EInvoiceError bei unbekanntem/kaputtem XML."""
    if len(data) > MAX_XML_BYTES:
        raise EInvoiceError("Die XML-Datei ist zu groß.")
    try:
        root = safe_fromstring(data)
    except Exception as exc:  # noqa: BLE001 – defusedxml wirft verschiedene Fehler (auch bei XML-Bomben)
        raise EInvoiceError(f"Die XML-Datei ist nicht lesbar: {exc.__class__.__name__}") from exc
    if root.tag == f"{{{RSM}}}CrossIndustryInvoice":
        parsed = _parse_cii(root)
    elif root.tag in (f"{{{UBL_INV}}}Invoice", f"{{{UBL_CN}}}CreditNote"):
        parsed = _parse_ubl(root)
    else:
        raise EInvoiceError("Keine bekannte E-Rechnung (weder CII noch UBL).")
    _match_partner(parsed, partners or [], own or auslese.OwnCompany())
    return parsed


def _tax_lines(rows: list[tuple[Decimal | None, Decimal | None, Decimal | None, str | None]], credit: bool,
               result: auslese.Result) -> list[TaxLine]:
    lines = []
    sign = Decimal(-1) if credit else Decimal(1)
    for rate, basis, amount, category in rows:
        rate = rate if rate is not None else Decimal(0)
        if category == "AE":
            result.hints.append("Reverse-Charge (§ 13b UStG): Die Umsatzsteuer schulden Sie selbst – "
                                "§ 13b ist unten vorausgewählt, bitte den Fall prüfen.")
        if basis is None:
            continue
        try:
            lines.append(complete_line(rate, sign * basis, sign * (amount or Decimal(0)), None))
        except Exception:  # noqa: BLE001 – Rundungsabweichung im XML: Werte trotzdem übernehmen
            vat = sign * (amount or Decimal(0))
            lines.append(TaxLine(rate, round_money(sign * basis), round_money(vat), round_money(sign * basis + vat)))
            result.uncertain.add("lines")
    return lines


def _skonto(terms: str | None, issue: date | None, result: auslese.Result) -> None:
    """XRechnung-Konvention: #SKONTO#TAGE=14#PROZENT=2.00#"""
    if not terms:
        return
    m = re.search(r"#SKONTO#TAGE=(\d+)#PROZENT=(\d+(?:\.\d+)?)#", terms)
    if m and issue:
        from datetime import timedelta

        result.skonto_percent = Decimal(m.group(2))
        result.skonto_until = issue + timedelta(days=int(m.group(1)))


def _parse_cii(root) -> Parsed:
    result = auslese.Result()
    doc = root.find("rsm:ExchangedDocument", NS)
    trade = root.find("rsm:SupplyChainTradeTransaction", NS)
    agreement = trade.find("ram:ApplicableHeaderTradeAgreement", NS) if trade is not None else None
    settlement = trade.find("ram:ApplicableHeaderTradeSettlement", NS) if trade is not None else None
    seller = agreement.find("ram:SellerTradeParty", NS) if agreement is not None else None
    credit = _text(doc, "ram:TypeCode") in ("381", "261", "262", "396")

    result.number = _text(doc, "ram:ID")
    result.doc_date = _date(_text(doc, "ram:IssueDateTime/udt:DateTimeString"))
    result.partner_name = _text(seller, "ram:Name")
    terms = settlement.find("ram:SpecifiedTradePaymentTerms", NS) if settlement is not None else None
    result.due_date = _date(_text(terms, "ram:DueDateDateTime/udt:DateTimeString"))
    terms_text = _text(terms, "ram:Description")
    _skonto(terms_text, result.doc_date, result)

    rows = []
    for tax in settlement.findall("ram:ApplicableTradeTax", NS) if settlement is not None else []:
        rows.append((_dec(_text(tax, "ram:RateApplicablePercent")), _dec(_text(tax, "ram:BasisAmount")),
                     _dec(_text(tax, "ram:CalculatedAmount")), _text(tax, "ram:CategoryCode")))
    result.lines = _tax_lines(rows, credit, result)
    summation = settlement.find("ram:SpecifiedTradeSettlementHeaderMonetarySummation", NS) \
        if settlement is not None else None
    grand = _dec(_text(summation, "ram:GrandTotalAmount"))
    result.gross = totals(result.lines)[2] if result.lines else grand

    items = []
    for item in trade.findall("ram:IncludedSupplyChainTradeLineItem", NS) if trade is not None else []:
        qty = item.find("ram:SpecifiedLineTradeDelivery/ram:BilledQuantity", NS)
        items.append(Item(
            name=_text(item, "ram:SpecifiedTradeProduct/ram:Name") or "",
            description=_text(item, "ram:SpecifiedTradeProduct/ram:Description") or "",
            quantity=_dec(qty.text) if qty is not None else None,
            unit=UNIT_NAMES.get(qty.get("unitCode", "") if qty is not None else "", ""),
            price=_dec(_text(item, "ram:SpecifiedLineTradeAgreement/ram:NetPriceProductTradePrice/ram:ChargeAmount")),
            net=_dec(_text(item, "ram:SpecifiedLineTradeSettlement/ram:SpecifiedTradeSettlementLineMonetarySummation/"
                                 "ram:LineTotalAmount")),
            rate=_dec(_text(item, "ram:SpecifiedLineTradeSettlement/ram:ApplicableTradeTax/ram:RateApplicablePercent")),
        ))
    vat_id = None
    for reg in seller.findall("ram:SpecifiedTaxRegistration/ram:ID", NS) if seller is not None else []:
        if reg.get("schemeID") == "VA" and reg.text:
            vat_id = reg.text.strip()
    iban = _text(settlement, "ram:SpecifiedTradeSettlementPaymentMeans/ram:PayeePartyCreditorFinancialAccount/"
                             "ram:IBANID")
    return Parsed(
        format="CII", result=result, items=items, seller_vat=vat_id, seller_iban=iban,
        buyer_name=_text(agreement, "ram:BuyerTradeParty/ram:Name"),
        buyer_reference=_text(agreement, "ram:BuyerReference"), credit_note=credit, payment_terms=terms_text,
        totals={k: v for k, v in {"netto": _dec(_text(summation, "ram:TaxBasisTotalAmount")),
                                  "brutto": grand, "zahlbetrag": _dec(_text(summation, "ram:DuePayableAmount"))}.items()
                if v is not None},
    )


def _parse_ubl(root) -> Parsed:
    result = auslese.Result()
    credit = root.tag == f"{{{UBL_CN}}}CreditNote"
    supplier = root.find("cac:AccountingSupplierParty/cac:Party", NS)
    result.number = _text(root, "cbc:ID")
    result.doc_date = _date(_text(root, "cbc:IssueDate"))
    result.due_date = _date(_text(root, "cbc:DueDate") or _text(root, "cac:PaymentMeans/cbc:PaymentDueDate"))
    result.partner_name = (_text(supplier, "cac:PartyLegalEntity/cbc:RegistrationName")
                           or _text(supplier, "cac:PartyName/cbc:Name"))
    terms_text = _text(root, "cac:PaymentTerms/cbc:Note")
    _skonto(terms_text, result.doc_date, result)
    rows = []
    for sub in root.findall("cac:TaxTotal/cac:TaxSubtotal", NS):
        rows.append((_dec(_text(sub, "cac:TaxCategory/cbc:Percent")), _dec(_text(sub, "cbc:TaxableAmount")),
                     _dec(_text(sub, "cbc:TaxAmount")), _text(sub, "cac:TaxCategory/cbc:ID")))
    result.lines = _tax_lines(rows, credit, result)
    grand = _dec(_text(root, "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount"))
    result.gross = totals(result.lines)[2] if result.lines else grand
    items = []
    line_tag, qty_tag = ("cac:CreditNoteLine", "cbc:CreditedQuantity") if credit else ("cac:InvoiceLine",
                                                                                      "cbc:InvoicedQuantity")
    for line in root.findall(line_tag, NS):
        qty = line.find(qty_tag, NS)
        items.append(Item(
            name=_text(line, "cac:Item/cbc:Name") or "", description=_text(line, "cac:Item/cbc:Description") or "",
            quantity=_dec(qty.text) if qty is not None else None,
            unit=UNIT_NAMES.get(qty.get("unitCode", "") if qty is not None else "", ""),
            price=_dec(_text(line, "cac:Price/cbc:PriceAmount")), net=_dec(_text(line, "cbc:LineExtensionAmount")),
            rate=_dec(_text(line, "cac:Item/cac:ClassifiedTaxCategory/cbc:Percent")),
        ))
    return Parsed(
        format="UBL", result=result, items=items,
        seller_vat=_text(supplier, "cac:PartyTaxScheme/cbc:CompanyID"),
        seller_iban=_text(root, "cac:PaymentMeans/cac:PayeeFinancialAccount/cbc:ID"),
        buyer_name=_text(root, "cac:AccountingCustomerParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName"),
        buyer_reference=_text(root, "cbc:BuyerReference"), credit_note=credit, payment_terms=terms_text,
        totals={k: v for k, v in {"netto": _dec(_text(root, "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount")),
                                  "brutto": grand,
                                  "zahlbetrag": _dec(_text(root, "cac:LegalMonetaryTotal/cbc:PayableAmount"))}.items()
                if v is not None},
    )


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", "", text or "").upper()


def _match_partner(parsed: Parsed, partners: list[auslese.Partner], own: auslese.OwnCompany) -> None:
    """Lieferant über USt-IdNr. bzw. IBAN aus dem XML wiedererkennen (exakt)."""
    r = parsed.result
    for p in partners:
        if (parsed.seller_vat and _norm(p.ust_id) == _norm(parsed.seller_vat)) or \
                (parsed.seller_iban and _norm(p.iban) == _norm(parsed.seller_iban)):
            r.partner_name, r.supplier_id = p.name, p.id
            break
    for key, value in (("number", r.number), ("doc_date", r.doc_date), ("partner", r.partner_name),
                       ("lines", r.lines)):
        if not value:
            r.uncertain.add(key)
    if parsed.credit_note:
        r.hints.append("Gutschrift / Rechnungskorrektur – die Beträge wurden negativ übernommen.")


# ===================================================================== SCHREIBEN
def format_for(db: Session, doc: Document) -> str:
    """Behörden (Leitweg-ID) bekommen XRechnung, sonst gilt die Firmeneinstellung."""
    customer = doc.customer
    if customer is not None and (customer.leitweg_id or customer.kind == "behoerde"):
        return "xrechnung"
    value = einstellungen.get(db, "rechnung.e_rechnung") or "zugferd"
    return value if value in FORMATS else "zugferd"


def country_code(name: str | None) -> str:
    text = (name or "").strip()
    if not text:
        return "DE"
    if re.fullmatch(r"[A-Za-z]{2}", text):
        return text.upper()
    return COUNTRY_CODES.get(text.lower(), "")


def missing(db: Session, doc: Document, fmt: str) -> list[str]:
    """Was für eine gültige E-Rechnung zusätzlich fehlt (zur Pflichtangaben-Prüfung beim Festschreiben)."""
    if fmt == "keine":
        return []
    values = einstellungen.load(db)
    problems = []
    customer = doc.customer
    if customer is None:
        return ["Kunde (für die E-Rechnung)"]
    if not country_code(values.get("firma.land")):
        problems.append("Land der eigenen Firma als Ländercode (z. B. DE)")
    if not country_code(customer.billing_country if customer.has_billing_address else customer.country):
        problems.append("Land des Kunden als Ländercode (z. B. AT)")
    if not ((customer.billing_zip and customer.billing_city) if customer.has_billing_address
            else (customer.zip and customer.city)):
        problems.append("PLZ und Ort des Kunden (Stammdaten)")
    if "13b" in (doc.hints or []) and not customer.ust_id:
        problems.append("USt-IdNr. des Kunden (Pflicht bei § 13b in der E-Rechnung)")
    if fmt == "xrechnung":
        for key, label in (("firma.telefon", "Telefonnummer der Firma"), ("firma.email", "E-Mail-Adresse der Firma")):
            if not values.get(key):
                problems.append(f"{label} (Pflicht in der XRechnung, Einstellungen → Firmendaten)")
        if not customer.email:
            problems.append("E-Mail-Adresse des Kunden (Pflicht in der XRechnung)")
    return problems


def _el(parent, tag: str, text=None, **attrs) -> Element:
    prefix, name = tag.split(":")
    el = SubElement(parent, f"{{{NS[prefix]}}}{name}", {k.replace('_', ''): v for k, v in attrs.items()})
    if text is not None:
        el.text = str(text)
    return el


def _amount(value: Decimal) -> str:
    return f"{round_money(value):.2f}"


def _price(value: Decimal) -> str:
    return f"{Decimal(value).quantize(Decimal('0.0001')).normalize():f}"


def _category(doc: Document, rate: Decimal, small_business: bool) -> tuple[str, str | None]:
    if small_business:
        return "E", KU_REASON
    if rate == 0 and "13b" in (doc.hints or []):
        return "AE", RC_REASON
    if rate == 0:
        return "E", "Steuerfrei"
    return "S", None


def _date102(parent, tag: str, value: date) -> None:
    holder = _el(parent, tag)
    _el(holder, "udt:DateTimeString", value.strftime("%Y%m%d"), format="102")


def _party(parent, tag: str, *, name, street, zip_code, city, country, email=None, contact=None, phone=None,
           vat_id=None, tax_number=None) -> None:
    party = _el(parent, tag)
    _el(party, "ram:Name", name)
    if contact or phone or (email and contact is not None):
        c = _el(party, "ram:DefinedTradeContact")
        if contact:
            _el(c, "ram:PersonName", contact)
        if phone:
            _el(_el(c, "ram:TelephoneUniversalCommunication"), "ram:CompleteNumber", phone)
        if email:
            _el(_el(c, "ram:EmailURIUniversalCommunication"), "ram:URIID", email)
    address = _el(party, "ram:PostalTradeAddress")
    if zip_code:
        _el(address, "ram:PostcodeCode", zip_code)
    if street:
        _el(address, "ram:LineOne", street)
    if city:
        _el(address, "ram:CityName", city)
    _el(address, "ram:CountryID", country)
    if email:
        _el(_el(party, "ram:URIUniversalCommunication"), "ram:URIID", email, schemeID="EM")
    if vat_id:
        _el(_el(party, "ram:SpecifiedTaxRegistration"), "ram:ID", vat_id, schemeID="VA")
    if tax_number:
        _el(_el(party, "ram:SpecifiedTaxRegistration"), "ram:ID", tax_number, schemeID="FC")


def build_cii(db: Session, doc: Document, fmt: str) -> bytes:
    """CII-XML nach EN 16931 (ZUGFeRD-Profil EN 16931 bzw. XRechnung 3.0)."""
    from app.services import rechnungen

    v = einstellungen.load(db)
    small_business = rechnungen.is_small_business(db)
    storno = doc.kind == KIND_STORNO
    sign = Decimal(-1) if storno else Decimal(1)  # Gutschrift (381) mit positiven Beträgen
    customer = doc.customer

    root = Element(f"{{{RSM}}}CrossIndustryInvoice")
    context = _el(root, "rsm:ExchangedDocumentContext")
    if fmt == "xrechnung":
        _el(_el(context, "ram:BusinessProcessSpecifiedDocumentContextParameter"), "ram:ID", BUSINESS_PROCESS)
    _el(_el(context, "ram:GuidelineSpecifiedDocumentContextParameter"), "ram:ID",
        GUIDELINE_XRECHNUNG if fmt == "xrechnung" else GUIDELINE_EN16931)

    header = _el(root, "rsm:ExchangedDocument")
    _el(header, "ram:ID", doc.number)
    _el(header, "ram:TypeCode", "381" if storno else "380")
    _date102(header, "ram:IssueDateTime", doc.doc_date)
    notes = [t for t in (doc.intro_text, *rechnungen.hint_texts(db, doc)) if t]
    for note in notes:
        _el(_el(header, "ram:IncludedNote"), "ram:Content", note)

    trade = _el(root, "rsm:SupplyChainTradeTransaction")
    for line in doc.lines:
        item = _el(trade, "ram:IncludedSupplyChainTradeLineItem")
        _el(_el(item, "ram:AssociatedDocumentLineDocument"), "ram:LineID", line.position)
        product = _el(item, "ram:SpecifiedTradeProduct")
        _el(product, "ram:Name", line.title)
        if line.description:
            _el(product, "ram:Description", line.description)
        agreement = _el(item, "ram:SpecifiedLineTradeAgreement")
        _el(_el(agreement, "ram:NetPriceProductTradePrice"), "ram:ChargeAmount", _price(line.unit_price or 0))
        quantity = sign * (line.quantity or Decimal(1))
        _el(_el(item, "ram:SpecifiedLineTradeDelivery"), "ram:BilledQuantity", _price(quantity),
            unitCode=UNIT_CODES.get(line.unit or "", "H87"))
        line_settlement = _el(item, "ram:SpecifiedLineTradeSettlement")
        tax = _el(line_settlement, "ram:ApplicableTradeTax")
        code, _ = _category(doc, line.vat_rate, small_business)
        _el(tax, "ram:TypeCode", "VAT")
        _el(tax, "ram:CategoryCode", code)
        _el(tax, "ram:RateApplicablePercent", _price(line.vat_rate))
        net = sign * line.net
        gross_line = round_money(quantity * (line.unit_price or 0))
        allowance = gross_line - net
        if allowance:  # Rabatt als Positions-Abschlag, damit Menge × Preis − Abschlag = Netto exakt stimmt
            charge = _el(line_settlement, "ram:SpecifiedTradeAllowanceCharge")
            _el(_el(charge, "ram:ChargeIndicator"), "udt:Indicator", "false")
            _el(charge, "ram:ActualAmount", _amount(abs(allowance)) if allowance > 0 else _amount(0))
            _el(charge, "ram:Reason", "Rabatt")
        _el(_el(line_settlement, "ram:SpecifiedTradeSettlementLineMonetarySummation"), "ram:LineTotalAmount",
            _amount(net))

    agreement = _el(trade, "ram:ApplicableHeaderTradeAgreement")
    _el(agreement, "ram:BuyerReference",
        (customer.leitweg_id if customer and customer.leitweg_id else (customer.number if customer else "-")))
    _party(agreement, "ram:SellerTradeParty", name=v.get("firma.name"), street=v.get("firma.strasse"),
           zip_code=v.get("firma.plz"), city=v.get("firma.ort"), country=country_code(v.get("firma.land")) or "DE",
           email=v.get("firma.email"), contact=v.get("firma.inhaber"), phone=v.get("firma.telefon"),
           vat_id=(v.get("steuer.ust_id") or None), tax_number=(v.get("steuer.steuernummer") or None))
    billing = customer.has_billing_address if customer else False
    _party(agreement, "ram:BuyerTradeParty", name=doc.recipient_name,
           street=(customer.billing_street if billing else customer.street) if customer else None,
           zip_code=(customer.billing_zip if billing else customer.zip) if customer else None,
           city=(customer.billing_city if billing else customer.city) if customer else None,
           country=country_code((customer.billing_country if billing else customer.country) if customer else None)
           or "DE", email=customer.email if customer else None, vat_id=customer.ust_id if customer else None)

    delivery = _el(trade, "ram:ApplicableHeaderTradeDelivery")
    if doc.service_date and not (doc.service_until and doc.service_until != doc.service_date):
        _date102(_el(delivery, "ram:ActualDeliverySupplyChainEvent"), "ram:OccurrenceDateTime", doc.service_date)

    settlement = _el(trade, "ram:ApplicableHeaderTradeSettlement")
    _el(settlement, "ram:PaymentReference", doc.number)
    _el(settlement, "ram:InvoiceCurrencyCode", "EUR")
    if v.get("bank.iban") and not storno:
        means = _el(settlement, "ram:SpecifiedTradeSettlementPaymentMeans")
        _el(means, "ram:TypeCode", "58")
        account = _el(means, "ram:PayeePartyCreditorFinancialAccount")
        _el(account, "ram:IBANID", v["bank.iban"])
        if v.get("bank.kontoinhaber"):
            _el(account, "ram:AccountName", v["bank.kontoinhaber"])
        if v.get("bank.bic"):
            _el(_el(means, "ram:PayeeSpecifiedCreditorFinancialInstitution"), "ram:BICID", v["bank.bic"])
    groups = rechnungen.tax_groups(doc)
    for g in groups:
        code, reason = _category(doc, g.rate, small_business)
        tax = _el(settlement, "ram:ApplicableTradeTax")
        _el(tax, "ram:CalculatedAmount", _amount(sign * g.vat))
        _el(tax, "ram:TypeCode", "VAT")
        if reason:
            _el(tax, "ram:ExemptionReason", reason)
        _el(tax, "ram:BasisAmount", _amount(sign * g.net))
        _el(tax, "ram:CategoryCode", code)
        _el(tax, "ram:RateApplicablePercent", _price(g.rate))
    if doc.service_date and doc.service_until and doc.service_until != doc.service_date:
        period = _el(settlement, "ram:BillingSpecifiedPeriod")
        _date102(period, "ram:StartDateTime", doc.service_date)
        _date102(period, "ram:EndDateTime", doc.service_until)
    # BR-CO-25: Bei einem Zahlbetrag sind Zahlungsbedingungen oder Fälligkeit Pflicht (auch bei Gutschriften)
    terms = _el(settlement, "ram:SpecifiedTradePaymentTerms")
    if storno:
        _el(terms, "ram:Description", f"Gutschrift zur Rechnung {doc.storno_of.number if doc.storno_of else ''}"
                                      " – der Betrag wird verrechnet bzw. erstattet.")
    else:
        _el(terms, "ram:Description", f"Zahlbar bis {doc.due_date.strftime('%d.%m.%Y')} ohne Abzug."
            if doc.due_date else "Zahlbar sofort ohne Abzug.")
        if doc.due_date:
            _date102(terms, "ram:DueDateDateTime", doc.due_date)
    net_total, vat_total, gross_total = totals(groups)
    summation = _el(settlement, "ram:SpecifiedTradeSettlementHeaderMonetarySummation")
    _el(summation, "ram:LineTotalAmount", _amount(sign * net_total))
    _el(summation, "ram:TaxBasisTotalAmount", _amount(sign * net_total))
    _el(summation, "ram:TaxTotalAmount", _amount(sign * vat_total), currencyID="EUR")
    _el(summation, "ram:GrandTotalAmount", _amount(sign * gross_total))
    _el(summation, "ram:DuePayableAmount", _amount(sign * gross_total))
    if storno and doc.storno_of is not None:
        ref = _el(settlement, "ram:InvoiceReferencedDocument")
        _el(ref, "ram:IssuerAssignedID", doc.storno_of.number)
        holder = _el(ref, "ram:FormattedIssueDateTime")
        _el(holder, "qdt:DateTimeString", doc.storno_of.doc_date.strftime("%Y%m%d"), format="102")

    _check_rules(doc, groups, sign)
    xml = tostring(root, encoding="UTF-8", xml_declaration=True)
    validate(xml)
    return xml


def _check_rules(doc: Document, groups: list[TaxLine], sign: Decimal) -> None:
    """Wichtigste Rechenregeln der EN 16931 (BR-CO-10/13/15), unabhängig vom Schema."""
    line_sum = sum((sign * line.net for line in doc.lines), Decimal(0))
    net_total, vat_total, gross_total = totals(groups)
    if round_money(line_sum) != round_money(sign * net_total):
        raise EInvoiceError("Summe der Positionen stimmt nicht mit der Netto-Summe überein (BR-CO-10).")
    if round_money(net_total + vat_total) != round_money(gross_total):
        raise EInvoiceError("Netto + Umsatzsteuer ergibt nicht den Gesamtbetrag (BR-CO-15).")


def validate(xml: bytes) -> None:
    """XSD-Prüfung (offline) gegen das Factur-X/ZUGFeRD-Schema EN 16931."""
    from facturx import xml_check_xsd

    try:
        xml_check_xsd(xml, flavor="factur-x", level="en16931")
    except Exception as exc:  # noqa: BLE001
        raise EInvoiceError(f"Die E-Rechnung entspricht nicht dem Schema: {exc}") from exc


# ------------------------------------------------------------------ PDF/A-3
def _srgb_profile() -> bytes:
    from PIL import ImageCms

    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def embed(pdf: bytes, xml: bytes, doc: Document, company: str) -> bytes:
    """XML als factur-x.xml in das PDF einbetten (ZUGFeRD/Factur-X, PDF/A-3) und ein sRGB-Farbprofil
    (OutputIntent) ergänzen. Die Schriften sind bereits eingebettet (siehe pdf_rechnung)."""
    from facturx import generate_from_binary
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject
    from pypdf.generic import TextStringObject

    title = f"{'Stornorechnung' if doc.kind == KIND_STORNO else 'Rechnung'} {doc.number}"
    combined = generate_from_binary(pdf, xml, flavor="factur-x", level="en16931", check_xsd=True,
                                    check_schematron=False, lang="de-DE",
                                    pdf_metadata={"author": company, "keywords": "Factur-X, ZUGFeRD, Rechnung",
                                                  "title": title, "subject": title})
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(combined)))
    profile = DecodedStreamObject()
    profile.set_data(_srgb_profile())
    profile[NameObject("/N")] = NumberObject(3)
    intent = DictionaryObject({
        NameObject("/Type"): NameObject("/OutputIntent"),
        NameObject("/S"): NameObject("/GTS_PDFA1"),
        NameObject("/OutputConditionIdentifier"): TextStringObject("sRGB IEC61966-2.1"),
        NameObject("/Info"): TextStringObject("sRGB IEC61966-2.1"),
        NameObject("/DestOutputProfile"): writer._add_object(profile),
    })
    writer._root_object[NameObject("/OutputIntents")] = ArrayObject([writer._add_object(intent)])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
