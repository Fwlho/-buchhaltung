"""Ausgangsrechnungen (Modul „rechnungen_ausgang“)."""
import re
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import TenantContext, require_module
from app.formatting import format_date, format_decimal
from app.models import AuditLog, CatalogItem, Customer, CustomerAddress, Document, User
from app.models.stammdaten import UNITS
from app.services import ablage, anmeldung, belege, mahnwesen, rechnungen, rechnungs_import, vertraege
from app.templating import redirect, render

router = APIRouter(prefix="/rechnungen")

out_read = require_module("rechnungen_ausgang")
out_edit = require_module("rechnungen_ausgang", edit=True)
BASE = "/rechnungen"


def _ip(request: Request) -> str:
    return anmeldung.client_ip(request)


def _doc(db: Session, doc_id: int) -> Document:
    doc = rechnungen.get(db, doc_id)
    if doc is None:
        raise HTTPException(404, "Rechnung nicht gefunden.")
    return doc


def _require_extra(ctx: TenantContext) -> None:
    if not ctx.has_extra("festschreiben"):
        raise HTTPException(403, "Dafür fehlt Ihnen das Sonderrecht „Festschreiben“.")


def _history(db: Session, ctx: TenantContext, doc: Document):
    stmt = (select(AuditLog).where(AuditLog.tenant_id == ctx.tenant_id, AuditLog.entity == "ausgangsrechnung",
                                   AuditLog.entity_id == str(doc.id)).order_by(AuditLog.id.desc()).limit(50))
    entries = list(db.scalars(stmt))
    names = {u.id: u.name for u in (db.get(User, e.user_id) for e in entries if e.user_id) if u}
    return [(e, names.get(e.user_id, "–")) for e in entries]


def _header_values(doc: Document) -> dict:
    return {
        "recipient_name": doc.recipient_name or "", "recipient_address": doc.recipient_address or "",
        "object_address": doc.object_address or "", "doc_date": format_date(doc.doc_date),
        "service_date": format_date(doc.service_date), "service_until": format_date(doc.service_until),
        "payment_terms_days": "" if doc.payment_terms_days is None else str(doc.payment_terms_days),
        "intro_text": doc.intro_text or "", "closing_text": doc.closing_text or "", "notes": doc.notes or "",
        "labor_amount": format_decimal(doc.labor_amount) if doc.labor_amount is not None else "",
        "payment_qr": doc.payment_qr or "girocode", "hints": list(doc.hints or []),
    }


def _posted(form) -> tuple[dict, list[dict]]:
    keys = ("recipient_name", "recipient_address", "object_address", "doc_date", "service_date", "service_until",
            "payment_terms_days", "intro_text", "closing_text", "notes", "labor_amount", "payment_qr")
    values = {k: form.get(k) or "" for k in keys}
    values["hints"] = form.getlist("hint")
    rows = []
    for i in range(rechnungen.MAX_ROWS):
        if f"pos-{i}-titel" not in form:
            break
        rows.append({k: form.get(f"pos-{i}-{k}") or "" for k in
                     ("titel", "beschreibung", "menge", "einheit", "preis", "rabatt", "satz", "katalog")})
    return values, rows


def _mail_context(db: Session, doc: Document, mail=None, mail_errors=None) -> dict:
    if not (doc.is_final and doc.kind == "rechnung"):
        return {"mail": None}
    from app.services import email_entwurf, imap

    if mail is None:
        mail = email_entwurf.default_draft(db, "rechnung", email_entwurf.placeholders_invoice(db, doc),
                                           doc.customer.email if doc.customer else None)
    return {"mail": mail, "mail_errors": mail_errors or {}, "mail_action": f"{BASE}/{doc.id}/email",
            "mail_configured": imap.config_for(db) is not None, "draft_at": doc.email_draft_at}


def _page(request: Request, db: Session, ctx: TenantContext, doc: Document, *, form=None, errors=None,
          extra_rows: int = rechnungen.EMPTY_ROWS, payment_errors=None, status_code: int = 200,
          mail=None, mail_errors=None):
    if form is not None:
        values, rows = _posted(form)
        rows += rechnungen.empty_rows(extra_rows)
    else:
        values, rows = _header_values(doc), rechnungen.form_rows(doc, extra_rows)
    addresses = []
    if doc.customer_id:
        addresses = list(db.scalars(select(CustomerAddress).where(CustomerAddress.customer_id == doc.customer_id,
                                                                  CustomerAddress.is_active.is_(True))))
    catalog = list(db.scalars(select(CatalogItem).where(CatalogItem.is_active.is_(True)).order_by(CatalogItem.title)))
    small_business = rechnungen.is_small_business(db)
    return render(
        request, "rechnung.html", status_code=status_code, doc=doc, v=values, rows=rows, errors=errors or {},
        payment_errors=payment_errors or {}, addresses=addresses, catalog=catalog, units=UNITS,
        vat_rates=rechnungen.VAT_RATES, hints=rechnungen.HINTS, small_business=small_business,
        groups=rechnungen.tax_groups(doc), hint_texts=rechnungen.hint_texts(db, doc),
        can_edit=ctx.can("rechnungen_ausgang", edit=True), can_finalize=ctx.has_extra("festschreiben"),
        missing=rechnungen.missing_for_finalize(db, doc) if doc.is_draft else [],
        storno=rechnungen.storno_for(db, doc), history=_history(db, ctx, doc), today=format_date(date.today()),
        dunnings=mahnwesen.dunnings_of(db, doc) if doc.is_final else [],
        **_mail_context(db, doc, mail, mail_errors),
    )


# ===================================================================== Liste
@router.get("")
def invoice_list(request: Request, ctx: TenantContext = Depends(out_read), db: Session = Depends(get_db)):
    p = request.query_params
    year = int(p["jahr"]) if p.get("jahr", "").isdigit() else None
    docs = rechnungen.outgoing(db, q=p.get("q", ""), status=p.get("status", ""), year=year,
                               overdue=p.get("ueberfaellig") == "1")
    final = [d for d in docs if d.is_final and d.kind == "rechnung" and d.status != "storniert"]
    sums = {"net": sum((d.net_total or 0 for d in final), 0), "gross": sum((d.gross_total or 0 for d in final), 0),
            "open": sum((d.open_amount for d in final), 0)}
    return render(request, "rechnungen.html", docs=docs, years=rechnungen.years(db), q=p.get("q", ""),
                  status=p.get("status", ""), year=year, overdue=p.get("ueberfaellig") == "1", sums=sums,
                  today_date=date.today(), can_edit=ctx.can("rechnungen_ausgang", edit=True),
                  contracts_on=vertraege.enabled(db), import_pending=rechnungs_import.pending_count(db),
                  can_finalize=ctx.has_extra("festschreiben"), today=format_date(date.today()),
                  list_url=BASE + (f"?{request.url.query}" if request.url.query else ""))


@router.post("/auswahl")
async def bulk_action(request: Request, ctx: TenantContext = Depends(out_read), db: Session = Depends(get_db)):
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    docs = [d for d in (rechnungen.get(db, i) for i in ids) if d is not None and d.file is not None]
    if not docs:
        return redirect(BASE, "Bitte festgeschriebene Rechnungen auswählen (Entwürfe haben noch kein PDF).", "fehler")
    if form.get("aktion") == "zip":
        return Response(belege.zip_files(docs), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="rechnungen.zip"'})
    if form.get("aktion") == "drucken":
        data, _ = belege.merged_pdf(docs)
        return Response(data, media_type="application/pdf", headers={"Content-Disposition": 'inline; filename="drucken.pdf"'})
    raise HTTPException(400, "Unbekannte Aktion.")


# ================================================================ Neu anlegen
@router.get("/neu")
def new_form(request: Request, ctx: TenantContext = Depends(out_edit), db: Session = Depends(get_db)):
    customers = list(db.scalars(select(Customer).where(Customer.is_active.is_(True)).order_by(Customer.name)))
    return render(request, "rechnung_neu.html", customers=customers,
                  preselect=request.query_params.get("kunde", ""), can_create_customer=ctx.can("kunden", edit=True))


@router.post("/neu")
def create(request: Request, customer_id: str = Form(""), ctx: TenantContext = Depends(out_edit),
           db: Session = Depends(get_db)):
    customer = db.get(Customer, int(customer_id)) if customer_id.isdigit() else None
    if customer is None or not customer.is_active:
        return redirect(f"{BASE}/neu", "Bitte einen Kunden auswählen.", "fehler")
    doc = rechnungen.create_draft(db, customer, user_id=ctx.user.id, ip=_ip(request))
    db.commit()
    return redirect(f"{BASE}/{doc.id}", "Entwurf angelegt – bitte Positionen eintragen.")


# ============================================================ Einzelrechnung
@router.get("/{doc_id}")
def detail(request: Request, doc_id: int, ctx: TenantContext = Depends(out_read), db: Session = Depends(get_db)):
    return _page(request, db, ctx, _doc(db, doc_id))


@router.post("/{doc_id}")
async def save(request: Request, doc_id: int, ctx: TenantContext = Depends(out_edit), db: Session = Depends(get_db)):
    doc = _doc(db, doc_id)
    form = await request.form()
    errors = rechnungen.save_form(db, doc, form, user_id=ctx.user.id, ip=_ip(request))
    if errors:
        db.rollback()
        return _page(request, db, ctx, _doc(db, doc_id), form=form, errors=errors, status_code=400, extra_rows=0)
    db.commit()
    action = form.get("aktion")
    if action == "vorschau":
        return _pdf_response(rechnungen_pdf(db, doc, draft=True), "vorschau.pdf")
    if action == "mehr_zeilen":
        return _page(request, db, ctx, doc, extra_rows=8)
    return redirect(f"{BASE}/{doc.id}", "Gespeichert.")


def rechnungen_pdf(db: Session, doc: Document, draft: bool) -> bytes:
    from app.services import pdf_rechnung

    return pdf_rechnung.render(db, doc, draft=draft)


def _pdf_response(data: bytes, name: str) -> Response:
    return Response(data, media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="{name}"', "X-Frame-Options": "SAMEORIGIN",
        "Content-Security-Policy": "frame-ancestors 'self'"})


@router.get("/{doc_id}/vorschau")
def preview(doc_id: int, ctx: TenantContext = Depends(out_read), db: Session = Depends(get_db)):
    doc = _doc(db, doc_id)
    if doc.is_final:
        return redirect(f"{BASE}/{doc.id}/pdf")
    return _pdf_response(rechnungen_pdf(db, doc, draft=True), "vorschau.pdf")


@router.get("/{doc_id}/pdf")
def stored_pdf(doc_id: int, ctx: TenantContext = Depends(out_read), db: Session = Depends(get_db)):
    """Das beim Festschreiben gespeicherte Original – wird nie neu erzeugt."""
    doc = _doc(db, doc_id)
    if doc.file is None:
        raise HTTPException(404, "Für Entwürfe gibt es noch kein PDF – bitte die Vorschau nutzen.")
    path = ablage.absolute_path(doc.file)
    if not path.exists():
        raise HTTPException(404, "Die PDF-Datei fehlt im Ablageordner.")
    return ablage.response(doc.file, headers={"X-Frame-Options": "SAMEORIGIN"})


@router.get("/{doc_id}/xml")
def stored_xml(doc_id: int, ctx: TenantContext = Depends(out_read), db: Session = Depends(get_db)):
    """XRechnung-Datei (beim Festschreiben gespeichert)."""
    doc = _doc(db, doc_id)
    if doc.xml_file is None:
        raise HTTPException(404, "Zu dieser Rechnung gibt es keine XRechnung-Datei.")
    # Download (attachment), nie als Webseite
    return ablage.response(doc.xml_file, media_type="application/xml", inline=False)


@router.post("/{doc_id}/festschreiben")
def finalize(request: Request, doc_id: int, ctx: TenantContext = Depends(out_edit), db: Session = Depends(get_db)):
    _require_extra(ctx)
    doc = _doc(db, doc_id)
    missing = rechnungen.finalize(db, doc, user_id=ctx.user.id, ip=_ip(request))
    if missing:
        db.rollback()
        return redirect(f"{BASE}/{doc_id}", "Festschreiben nicht möglich. Es fehlt: " + ", ".join(missing), "fehler")
    db.commit()
    warning = rechnungen.copy_to_folder(db, doc)
    message = f"Rechnung {doc.number} festgeschrieben – das PDF ist unveränderbar gespeichert."
    return redirect(f"{BASE}/{doc.id}", message + (" " + warning if warning else ""), "hinweis" if warning else "ok")


@router.post("/{doc_id}/stornieren")
def cancel(request: Request, doc_id: int, grund: str = Form(""), ctx: TenantContext = Depends(out_edit),
           db: Session = Depends(get_db)):
    _require_extra(ctx)
    doc = _doc(db, doc_id)
    storno, problem = rechnungen.cancel(db, doc, grund, user_id=ctx.user.id, ip=_ip(request))
    if problem:
        db.rollback()
        return redirect(f"{BASE}/{doc_id}", problem, "fehler")
    db.commit()
    rechnungen.copy_to_folder(db, storno)
    return redirect(f"{BASE}/{storno.id}", f"Stornorechnung {storno.number} erstellt; Rechnung {doc.number} ist storniert.")


@router.post("/{doc_id}/zahlung")
async def payment(request: Request, doc_id: int, ctx: TenantContext = Depends(out_edit), db: Session = Depends(get_db)):
    doc = _doc(db, doc_id)
    form = await request.form()
    errors = belege.add_payment(db, doc, form, user_id=ctx.user.id, ip=_ip(request))
    if errors:
        db.rollback()
        return _page(request, db, ctx, _doc(db, doc_id), payment_errors=errors, status_code=400)
    db.commit()
    return redirect(f"{BASE}/{doc.id}", "Zahlungseingang erfasst." + (" Rechnung ist bezahlt." if doc.status == "bezahlt" else ""))


def _back(form, doc: Document) -> str:
    """Zurück zur Liste (mit Filtern) oder zur Rechnung – nur Ziele unter /rechnungen."""
    target = str(form.get("zurueck") or "")
    if target.startswith(BASE) and not target.startswith("//") and "\\" not in target:
        return target
    return f"{BASE}/{doc.id}"


@router.post("/{doc_id}/bezahlt")
async def mark_paid(request: Request, doc_id: int, ctx: TenantContext = Depends(out_edit),
                    db: Session = Depends(get_db)):
    """Häkchen „Bezahlt“ (Übersicht und Rechnungsseite): offenen Betrag zum angegebenen Tag buchen."""
    doc = _doc(db, doc_id)
    form = await request.form()
    problem = belege.mark_paid(db, doc, str(form.get("paid_on") or ""), user_id=ctx.user.id, ip=_ip(request))
    if problem:
        db.rollback()
        return redirect(_back(form, doc), f"Rechnung {doc.number}: {problem}", "fehler")
    db.commit()
    return redirect(_back(form, doc), f"Rechnung {doc.number} als bezahlt markiert.")


@router.post("/{doc_id}/unbezahlt")
async def unmark_paid(request: Request, doc_id: int, ctx: TenantContext = Depends(out_edit),
                      db: Session = Depends(get_db)):
    _require_extra(ctx)
    doc = _doc(db, doc_id)
    form = await request.form()
    problem = belege.unmark_paid(db, doc, user_id=ctx.user.id, ip=_ip(request))
    if problem:
        db.rollback()
        return redirect(_back(form, doc), problem, "fehler")
    db.commit()
    return redirect(_back(form, doc), f"Häkchen entfernt – Rechnung {doc.number} ist wieder offen.")


@router.post("/{doc_id}/zahlung/{payment_id}/loeschen")
def payment_delete(request: Request, doc_id: int, payment_id: int, ctx: TenantContext = Depends(out_edit),
                   db: Session = Depends(get_db)):
    _require_extra(ctx)
    doc = _doc(db, doc_id)
    if not belege.delete_payment(db, doc, payment_id, user_id=ctx.user.id, ip=_ip(request)):
        raise HTTPException(404, "Zahlung nicht gefunden.")
    db.commit()
    return redirect(f"{BASE}/{doc.id}", "Zahlung entfernt.")


@router.post("/{doc_id}/loeschen")
def delete(request: Request, doc_id: int, ctx: TenantContext = Depends(out_edit), db: Session = Depends(get_db)):
    doc = _doc(db, doc_id)
    problem = belege.delete_draft(db, doc, user_id=ctx.user.id, ip=_ip(request))
    if problem:
        return redirect(f"{BASE}/{doc_id}", "Festgeschriebene Rechnungen können nicht gelöscht werden – bitte stornieren.",
                        "fehler")
    db.commit()
    return redirect(BASE, "Entwurf gelöscht.")


@router.post("/{doc_id}/email")
async def email_draft(request: Request, doc_id: int, ctx: TenantContext = Depends(out_edit),
                      db: Session = Depends(get_db)):
    """E-Mail-Entwurf im Postfach anlegen, im Mailprogramm öffnen oder als .eml – nie automatisch versenden."""
    from app.services import email_entwurf, imap, mailprogramm

    doc = _doc(db, doc_id)
    if not (doc.is_final and doc.kind == "rechnung"):
        return redirect(f"{BASE}/{doc.id}", "Nur festgeschriebene Rechnungen können versendet werden.", "fehler")
    form = await request.form()
    draft = email_entwurf.Draft(to=(form.get("to") or "").strip(), subject=(form.get("subject") or "").strip(),
                                body=form.get("body") or "")
    errors = email_entwurf.check(draft)
    if form.get("aktion") == "entwurf" and not draft.to:
        errors["to"] = "Bitte eine Empfänger-Adresse angeben."
    if errors:
        return _page(request, db, ctx, doc, mail=draft, mail_errors=errors, status_code=400)
    msg = email_entwurf.build(db, draft, email_entwurf.invoice_pdf(doc), doc.file.original_name,
                              xml=email_entwurf.invoice_xml(doc))
    if form.get("aktion") == "eml":
        name = re.sub(r"[^A-Za-z0-9._-]", "_", doc.file.original_name.rsplit(".", 1)[0]) + ".eml"
        return Response(bytes(msg), media_type="message/rfc822",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    program = mailprogramm.chosen(form)
    try:
        folder = mailprogramm.open_compose(program, msg) if program else email_entwurf.append_to_drafts(db, msg)
    except (imap.ImapError, mailprogramm.MailProgramError) as exc:
        return _page(request, db, ctx, doc, mail=draft, mail_errors={"_": str(exc)}, status_code=400)
    email_entwurf.mark_created(db, doc, entity="ausgangsrechnung", user_id=ctx.user.id, ip=_ip(request),
                               folder=folder, to=draft.to)
    db.commit()
    if program:
        return redirect(f"{BASE}/{doc.id}", mailprogramm.opened_message(folder))
    return redirect(f"{BASE}/{doc.id}", f"Entwurf im Ordner „{folder}“ angelegt – bitte im Mailprogramm prüfen "
                                        "und selbst senden.")


@router.post("/{doc_id}/duplizieren")
def duplicate(request: Request, doc_id: int, ctx: TenantContext = Depends(out_edit), db: Session = Depends(get_db)):
    copy = rechnungen.duplicate(db, _doc(db, doc_id), user_id=ctx.user.id, ip=_ip(request))
    db.commit()
    return redirect(f"{BASE}/{copy.id}", "Kopie als neuer Entwurf angelegt – bitte Leistungsdatum eintragen.")
