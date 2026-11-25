"""E-Rechnung: ZUGFeRD/XRechnung schreiben (Schema, PDF/A-3) und lesen (CII, UBL, eingebettet, sicher)."""
import email
import email.policy
import io
from decimal import Decimal as D

import pytest
from pypdf import PdfReader

from app.database import tenant_scope
from app.services import einstellungen, erechnung, imap
from tests.conftest import VALID_SETTINGS, post, read_stored
from tests.fake_imap import FakeIMAP
from tests.test_eingangsrechnungen import _all_docs, upload
from tests.test_email import MAIL_SETTINGS
from tests.test_rechnungen import _customer, _draft, _final, _ready

CII = {"rsm": erechnung.RSM, "ram": erechnung.RAM, "udt": erechnung.UDT}


def _stored(doc) -> bytes:
    return read_stored(doc.file)


def _xml_of(pdf: bytes):
    from defusedxml.ElementTree import fromstring

    xml = erechnung.xml_from_pdf(pdf)
    assert xml is not None
    return xml, fromstring(xml)


# ------------------------------------------------------------------- ZUGFeRD
def test_zugferd_eingebettet_und_gueltig(client, db):
    _ready(client)
    doc = _final(client, db)
    assert doc.e_invoice_format == "zugferd" and doc.xml_file_id is None
    pdf = _stored(doc)
    xml, root = _xml_of(pdf)
    erechnung.validate(xml)  # XSD EN 16931
    assert root.find("rsm:ExchangedDocument/ram:ID", CII).text == doc.number
    assert root.find("rsm:ExchangedDocument/ram:TypeCode", CII).text == "380"
    guideline = root.find("rsm:ExchangedDocumentContext/ram:GuidelineSpecifiedDocumentContextParameter/ram:ID", CII)
    assert guideline.text == erechnung.GUIDELINE_EN16931
    s = "rsm:SupplyChainTradeTransaction/ram:ApplicableHeaderTradeSettlement/"
    assert root.find(s + "ram:SpecifiedTradeSettlementHeaderMonetarySummation/ram:GrandTotalAmount", CII).text == "182.29"
    rates = {t.find("ram:RateApplicablePercent", CII).text: (t.find("ram:BasisAmount", CII).text,
                                                             t.find("ram:CalculatedAmount", CII).text)
             for t in root.findall(s + "ram:ApplicableTradeTax", CII)}
    assert rates == {"19": ("135.20", "25.69"), "7": ("20.00", "1.40")}
    assert root.find(s + "ram:SpecifiedTradeSettlementPaymentMeans/ram:PayeePartyCreditorFinancialAccount/ram:IBANID",
                     CII).text == "DE89370400440532013000"
    # Rabatt-Position: Menge × Preis − Abschlag = Netto
    line2 = root.findall("rsm:SupplyChainTradeTransaction/ram:IncludedSupplyChainTradeLineItem", CII)[1]
    assert line2.find(".//ram:SpecifiedTradeAllowanceCharge/ram:ActualAmount", CII).text == "11.55"
    assert line2.find(".//ram:LineTotalAmount", CII).text == "103.95"


@pytest.mark.parametrize("vorlage", ["klassisch", "petrol", "bronze", "terrakotta"])  # u. a. kursive Schrift
def test_pdf_a3_merkmale(client, db, vorlage):
    _ready(client)
    with tenant_scope(db, _customer(client, db).tenant_id):
        einstellungen.set_values(db, {"design.vorlage_rechnung": vorlage})
        db.commit()
    pdf = _stored(_final(client, db))
    reader = PdfReader(io.BytesIO(pdf))
    root = reader.trailer["/Root"]
    assert "/OutputIntents" in root and "/Metadata" in root and "/AF" in root
    xmp = root["/Metadata"].get_object().get_data()
    assert b"pdfaid:part" in xmp and b"3" in xmp and b"factur-x" in xmp.lower()
    for page in reader.pages:  # alle Schriften eingebettet (PDF/A-Pflicht)
        for font in page["/Resources"]["/Font"].values():
            descriptor = font.get_object().get("/FontDescriptor")
            assert descriptor is not None and any(k in descriptor.get_object() for k in ("/FontFile2", "/FontFile3"))


def test_ohne_e_rechnung(client, db):
    _ready(client)
    post(client, "/einstellungen/design", {**VALID_SETTINGS["design"], "rechnung.e_rechnung": "keine"})
    doc = _final(client, db)
    assert doc.e_invoice_format == "keine" and erechnung.xml_from_pdf(_stored(doc)) is None


def test_kleinunternehmer_und_13b_steuerkategorien(client, db):
    _ready(client)
    post(client, "/einstellungen/steuer", {**VALID_SETTINGS["steuer"], "steuer.kleinunternehmer": "1"})
    _, root = _xml_of(_stored(_final(client, db)))
    tax = root.find(".//ram:ApplicableHeaderTradeSettlement/ram:ApplicableTradeTax", CII)
    assert tax.find("ram:CategoryCode", CII).text == "E"
    assert tax.find("ram:ExemptionReason", CII).text == erechnung.KU_REASON

    post(client, "/einstellungen/steuer", {**VALID_SETTINGS["steuer"]})
    doc = _final(client, db, _draft(client, db, _customer(client, db, ust_id="DE123456789")), hint=["13b"])
    _, root = _xml_of(_stored(doc))
    tax = root.find(".//ram:ApplicableHeaderTradeSettlement/ram:ApplicableTradeTax", CII)
    assert tax.find("ram:CategoryCode", CII).text == "AE" and tax.find("ram:CalculatedAmount", CII).text == "0.00"
    buyer_vat = root.find(".//ram:BuyerTradeParty/ram:SpecifiedTaxRegistration/ram:ID", CII)
    assert buyer_vat.text == "DE123456789" and buyer_vat.get("schemeID") == "VA"


def test_storno_als_gutschrift(client, db):
    _ready(client)
    doc = _final(client, db)
    post(client, f"/rechnungen/{doc.id}/stornieren", {"grund": "Fehler"})
    storno = next(d for d in _all_docs(db) if d.kind == "storno")
    xml, root = _xml_of(_stored(storno))
    erechnung.validate(xml)
    assert root.find("rsm:ExchangedDocument/ram:TypeCode", CII).text == "381"
    assert root.find(".//ram:InvoiceReferencedDocument/ram:IssuerAssignedID", CII).text == doc.number
    assert root.find(".//ram:GrandTotalAmount", CII).text == "182.29"  # Gutschrift mit positiven Beträgen


# ------------------------------------------------------------------- XRechnung
def test_xrechnung_fuer_behoerde(client, db, monkeypatch):
    _ready(client)
    customer = _customer(client, db, kind="behoerde", name="Bezirksamt Mitte", leitweg_id="04011000-1234512345-06",
                         email="")
    draft = _draft(client, db, customer)
    from tests.test_rechnungen import _form
    post(client, f"/rechnungen/{draft.id}", _form(draft))
    r = post(client, f"/rechnungen/{draft.id}/festschreiben")
    for text in ("Telefonnummer der Firma", "E-Mail-Adresse der Firma", "E-Mail-Adresse des Kunden"):
        assert text in r.text, text   # Pflichtangaben der XRechnung werden vorher geprüft

    post(client, "/einstellungen/firma", {**VALID_SETTINGS["firma"], "firma.telefon": "030 123456",
                                          "firma.email": "info@muster.example"})
    post(client, f"/kunden/{customer.id}/bearbeiten", {"kind": "behoerde", "name": "Bezirksamt Mitte",
                                                       "street": "Amtsweg 1", "zip": "10115", "city": "Berlin",
                                                       "country": "Deutschland", "email": "rechnung@amt.example",
                                                       "leitweg_id": "04011000-1234512345-06"})
    doc = _final(client, db, draft)
    assert doc.e_invoice_format == "xrechnung" and doc.xml_file_id is not None
    r = client.get(f"/rechnungen/{doc.id}/xml")
    assert r.headers["content-type"].startswith("application/xml")
    erechnung.validate(r.content)
    from defusedxml.ElementTree import fromstring
    root = fromstring(r.content)
    ctx = "rsm:ExchangedDocumentContext/"
    assert root.find(ctx + "ram:GuidelineSpecifiedDocumentContextParameter/ram:ID", CII).text == \
        erechnung.GUIDELINE_XRECHNUNG
    assert root.find(ctx + "ram:BusinessProcessSpecifiedDocumentContextParameter/ram:ID", CII).text == \
        erechnung.BUSINESS_PROCESS
    assert root.find(".//ram:BuyerReference", CII).text == "04011000-1234512345-06"
    seller = root.find(".//ram:SellerTradeParty", CII)
    assert seller.find("ram:DefinedTradeContact/ram:TelephoneUniversalCommunication/ram:CompleteNumber", CII).text \
        == "030 123456"
    assert seller.find("ram:URIUniversalCommunication/ram:URIID", CII).get("schemeID") == "EM"
    assert erechnung.xml_from_pdf(_stored(doc)) is None  # bei XRechnung ist das PDF nur die Ansicht
    assert "XML-Datei herunterladen" in client.get(f"/rechnungen/{doc.id}").text

    # E-Mail-Entwurf enthält PDF und XML
    fake = FakeIMAP(["INBOX", "INBOX.Drafts"], special={"INBOX.Drafts": "\\Drafts"})
    monkeypatch.setattr(imap, "connect", lambda cfg: fake)
    post(client, "/einstellungen/email", MAIL_SETTINGS)
    post(client, f"/rechnungen/{doc.id}/email", {"to": "rechnung@amt.example", "subject": "Rechnung",
                                                 "body": "x", "aktion": "entwurf"})
    msg = email.message_from_bytes(fake.mails("INBOX.Drafts")[0]["data"], policy=email.policy.default)
    assert sorted(a.get_content_type() for a in msg.iter_attachments()) == ["application/pdf", "application/xml"]


# --------------------------------------------------------------------- Lesen
def test_eigene_zugferd_rechnung_wieder_einlesen(client, db):
    _ready(client)
    out = _final(client, db)
    upload(client, ("zugferd.pdf", _stored(out)))
    doc = _all_docs(db)[-1]
    assert doc.pending and doc.direction == "eingang"
    assert doc.number == out.number and doc.doc_date == out.doc_date and doc.due_date == out.due_date
    assert doc.partner_name == "Muster Service" and doc.review_fields == []  # exakt, nichts „bitte prüfen“
    assert {(line.vat_rate, line.net, line.vat) for line in doc.lines} == {
        (D("19"), D("135.20"), D("25.69")), (D("7"), D("20.00"), D("1.40"))}
    assert doc.gross_total == D("182.29") and "ZUGFeRD/Factur-X" in doc.notes


UBL = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
 xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
 <cbc:CustomizationID>urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0</cbc:CustomizationID>
 <cbc:ID>UBL-4711</cbc:ID>
 <cbc:IssueDate>2026-05-04</cbc:IssueDate>
 <cbc:DueDate>2026-05-18</cbc:DueDate>
 <cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
 <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
 <cbc:BuyerReference>991-12345-67</cbc:BuyerReference>
 <cac:AccountingSupplierParty><cac:Party>
  <cac:PartyTaxScheme><cbc:CompanyID>DE987654321</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>
  <cac:PartyLegalEntity><cbc:RegistrationName>Druckerei Nord GmbH</cbc:RegistrationName></cac:PartyLegalEntity>
 </cac:Party></cac:AccountingSupplierParty>
 <cac:AccountingCustomerParty><cac:Party><cac:PartyLegalEntity><cbc:RegistrationName>Muster Service</cbc:RegistrationName></cac:PartyLegalEntity></cac:Party></cac:AccountingCustomerParty>
 <cac:PaymentMeans><cbc:PaymentMeansCode>58</cbc:PaymentMeansCode><cac:PayeeFinancialAccount><cbc:ID>DE02120300000000202051</cbc:ID></cac:PayeeFinancialAccount></cac:PaymentMeans>
 <cac:PaymentTerms><cbc:Note>#SKONTO#TAGE=10#PROZENT=2.00#</cbc:Note></cac:PaymentTerms>
 <cac:TaxTotal><cbc:TaxAmount currencyID="EUR">22.50</cbc:TaxAmount>
  <cac:TaxSubtotal><cbc:TaxableAmount currencyID="EUR">100.00</cbc:TaxableAmount><cbc:TaxAmount currencyID="EUR">19.00</cbc:TaxAmount><cac:TaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>19</cbc:Percent></cac:TaxCategory></cac:TaxSubtotal>
  <cac:TaxSubtotal><cbc:TaxableAmount currencyID="EUR">50.00</cbc:TaxableAmount><cbc:TaxAmount currencyID="EUR">3.50</cbc:TaxAmount><cac:TaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>7</cbc:Percent></cac:TaxCategory></cac:TaxSubtotal>
 </cac:TaxTotal>
 <cac:LegalMonetaryTotal><cbc:TaxExclusiveAmount currencyID="EUR">150.00</cbc:TaxExclusiveAmount><cbc:TaxInclusiveAmount currencyID="EUR">172.50</cbc:TaxInclusiveAmount><cbc:PayableAmount currencyID="EUR">172.50</cbc:PayableAmount></cac:LegalMonetaryTotal>
 <cac:InvoiceLine><cbc:ID>1</cbc:ID><cbc:InvoicedQuantity unitCode="H87">2</cbc:InvoicedQuantity><cbc:LineExtensionAmount currencyID="EUR">100.00</cbc:LineExtensionAmount><cac:Item><cbc:Name>Flyer A5</cbc:Name><cac:ClassifiedTaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>19</cbc:Percent></cac:ClassifiedTaxCategory></cac:Item><cac:Price><cbc:PriceAmount currencyID="EUR">50.00</cbc:PriceAmount></cac:Price></cac:InvoiceLine>
 <cac:InvoiceLine><cbc:ID>2</cbc:ID><cbc:InvoicedQuantity unitCode="H87">1</cbc:InvoicedQuantity><cbc:LineExtensionAmount currencyID="EUR">50.00</cbc:LineExtensionAmount><cac:Item><cbc:Name>Broschüre</cbc:Name><cac:ClassifiedTaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>7</cbc:Percent></cac:ClassifiedTaxCategory></cac:Item><cac:Price><cbc:PriceAmount currencyID="EUR">50.00</cbc:PriceAmount></cac:Price></cac:InvoiceLine>
</Invoice>"""


def test_xrechnung_ubl_lesen_und_ansicht(client, db):
    _ready(client)
    post(client, "/lieferanten/neu", {"name": "Druckerei Nord", "ust_id": "DE987654321", "country": "Deutschland"})
    r = upload(client, ("rechnung.xml", UBL.encode()))
    assert "1 Beleg(e) hochgeladen" in r.text
    doc = _all_docs(db)[-1]
    assert (doc.number, doc.doc_date.isoformat(), doc.due_date.isoformat()) == ("UBL-4711", "2026-05-04", "2026-05-18")
    assert doc.supplier_id is not None and doc.partner_name == "Druckerei Nord"  # über USt-IdNr. erkannt
    assert doc.skonto_percent == D("2.00") and doc.skonto_until.isoformat() == "2026-05-14"
    assert doc.gross_total == D("172.50") and doc.review_fields == []
    page = client.get(f"/pruefbereich/{doc.id}").text
    for text in ("E-Rechnung", "Format UBL", "Flyer A5", "Broschüre", "991-12345-67", "172,50 €",
                 "2 % Skonto bei Zahlung innerhalb von 10 Tagen"):
        assert text in page, text


def test_gutschrift_negativ(client, db):
    _ready(client)
    credit = (UBL.replace("<Invoice xmlns=\"urn:oasis:names:specification:ubl:schema:xsd:Invoice-2\"",
                          "<CreditNote xmlns=\"urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2\"")
              .replace("</Invoice>", "</CreditNote>"))
    upload(client, ("gutschrift.xml", credit.encode()))
    doc = _all_docs(db)[-1]
    assert doc.kind == "gutschrift" and doc.gross_total == D("-172.50") and "Gutschrift" in doc.notes


def test_xml_bombe_wird_abgewehrt(client, db):
    _ready(client)
    bomb = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">'
            b'<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;">]><Invoice xmlns="urn:oasis:names:specification:ubl:schema:'
            b'xsd:Invoice-2">&lol3;</Invoice>')
    upload(client, ("bombe.xml", bomb))
    doc = _all_docs(db)[-1]
    assert "nicht lesbar" in doc.notes and doc.number is None


def test_email_import_mit_xrechnung(client, db, monkeypatch):
    from tests.test_email import _setup_mail, mail

    fake = FakeIMAP(["INBOX", "INBOX.Rechnungen", "INBOX.Verarbeitet"])
    monkeypatch.setattr(imap, "connect", lambda cfg: fake)
    _setup_mail(client)
    fake.add_mail("INBOX.Rechnungen", mail(attachments=(("xrechnung.xml", UBL.encode()),)))
    post(client, "/pruefbereich/email-abruf")
    doc = _all_docs(db)[-1]
    assert doc.source == "email" and doc.number == "UBL-4711" and doc.review_fields == []
