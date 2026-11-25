"""E-Mail-Entwürfe für Rechnungen und Angebote (KONZEPT.md 6.14) – KEIN automatischer Versand.

Die Mail wird mit Betreff/Text aus der Vorlage und dem PDF im Anhang gebaut und per IMAP APPEND
mit dem Kennzeichen \\Draft in den Entwürfe-Ordner des Firmenpostfachs gelegt. Versenden tut
der Nutzer selbst aus seinem Mailprogramm. Ersatzweise gibt es die Mail als .eml-Datei.
"""
import imaplib
import re
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from sqlalchemy.orm import Session

from app.database import utcnow
from app.formatting import format_date, format_money
from app.models import Document, Quote
from app.services import ablage, audit, einstellungen, imap, pruefungen

MONTHS = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
          "November", "Dezember")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


@dataclass
class Draft:
    to: str
    subject: str
    body: str


def fill(template: str, values: dict[str, str]) -> str:
    """Platzhalter wie {nummer} ersetzen. Bewusst nur einfache Namen (kein str.format –
    damit lassen sich über Vorlagen keine internen Werte auslesen). Unbekannte bleiben stehen."""
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template or "")


def _common(db: Session) -> dict[str, str]:
    v = einstellungen.load(db)
    return {"firma": v.get("firma.name") or "", "kontoinhaber": v.get("bank.kontoinhaber") or ""}


def placeholders_invoice(db: Session, doc: Document) -> dict[str, str]:
    month_date = doc.service_date or doc.doc_date
    return {**_common(db), "nummer": doc.number or "", "datum": format_date(doc.doc_date),
            "monat": MONTHS[month_date.month - 1] if month_date else "", "betrag": format_money(doc.gross_total),
            "faellig": format_date(doc.due_date), "kunde": doc.recipient_name or ""}


def placeholders_quote(db: Session, quote: Quote) -> dict[str, str]:
    return {**_common(db), "nummer": quote.number, "datum": format_date(quote.quote_date),
            "monat": MONTHS[quote.quote_date.month - 1], "betrag": format_money(quote.gross_total),
            "faellig": format_date(quote.valid_until), "kunde": quote.recipient_name or ""}


def default_draft(db: Session, kind: str, values: dict[str, str], customer_email: str | None) -> Draft:
    v = einstellungen.load(db)
    return Draft(to=customer_email or "", subject=fill(v.get(f"{kind}.mail_betreff") or "", values),
                 body=fill(v.get(f"{kind}.mail_text") or "", values))


def build(db: Session, draft: Draft, attachment: bytes, filename: str,
          xml: tuple[bytes, str] | None = None) -> EmailMessage:
    v = einstellungen.load(db)
    sender = v.get("email.absender") or v.get("firma.email") or v.get("imap.benutzer") or ""
    msg = EmailMessage()
    msg["From"] = f"{v.get('firma.name') or ''} <{sender}>" if sender else (v.get("firma.name") or "")
    if draft.to:
        msg["To"] = draft.to
    msg["Subject"] = draft.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(draft.body)
    msg.add_attachment(attachment, maintype="application", subtype="pdf", filename=filename)
    if xml is not None:  # XRechnung: die XML-Datei ist die eigentliche Rechnung, das PDF nur die Ansicht
        msg.add_attachment(xml[0], maintype="application", subtype="xml", filename=xml[1])
    return msg


def invoice_xml(doc: Document) -> tuple[bytes, str] | None:
    if doc.xml_file is None:
        return None
    return ablage.read_bytes(doc.xml_file), doc.xml_file.original_name


def check(draft: Draft) -> dict[str, str]:
    errors = {}
    if draft.to and not all(pruefungen.email_valid(x.strip()) for x in draft.to.split(",")):
        errors["to"] = "Bitte gültige E-Mail-Adresse(n) angeben (mehrere mit Komma trennen)."
    if not draft.subject.strip():
        errors["subject"] = "Bitte einen Betreff angeben."
    if len(draft.subject) > 300 or "\n" in draft.subject or "\r" in draft.subject:
        errors["subject"] = "Der Betreff muss einzeilig sein (höchstens 300 Zeichen)."
    return errors


def append_to_drafts(db: Session, msg: EmailMessage) -> str:
    """Legt die Mail im Entwürfe-Ordner ab. Gibt den Ordnernamen zurück; wirft imap.ImapError."""
    cfg = imap.config_for(db)
    if cfg is None:
        raise imap.ImapError("Kein Postfach eingerichtet (Einstellungen → E-Mail). Bitte die .eml-Datei nutzen.")
    conn = imap.connect(cfg)
    try:
        folder = imap.drafts_folder(conn, cfg.folder_drafts)
        if folder is None:
            raise imap.ImapError("Der Entwürfe-Ordner wurde nicht gefunden. Bitte in den Einstellungen "
                                 "(E-Mail → Entwürfe-Ordner) den genauen Namen eintragen.")
        try:
            typ, data = conn.append(imap.quote(folder), r"(\Draft \Seen)",
                                    imaplib.Time2Internaldate(time.time()), bytes(msg))
        except imaplib.IMAP4.error as exc:
            raise imap.ImapError(f"Der Entwurf konnte nicht gespeichert werden: {exc}") from exc
        if typ != "OK":
            raise imap.ImapError(f"Der Mailserver hat den Entwurf abgelehnt: {data}")
        return folder
    finally:
        imap.logout(conn)


def mark_created(db: Session, obj, *, entity: str, user_id: int, ip: str | None, folder: str, to: str) -> None:
    obj.email_draft_at = utcnow()
    audit.log(db, action="email_entwurf", entity=entity, entity_id=obj.id, tenant_id=obj.tenant_id, user_id=user_id,
              changes={"an": to, "ordner": folder}, ip=ip)


def invoice_pdf(doc: Document) -> bytes:
    """Für Rechnungen immer das beim Festschreiben gespeicherte Original-PDF."""
    if doc.file is None:
        raise ValueError("Nur festgeschriebene Rechnungen können versendet werden.")
    return ablage.read_bytes(doc.file)
