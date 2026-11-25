"""Angebote (Modul „angebote“). Angebot -> Rechnung braucht zusätzlich das Recht „rechnungen_ausgang“."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import TenantContext, require_module
from app.formatting import format_date, format_decimal
from app.models import AuditLog, CatalogItem, Customer, CustomerAddress, Quote, User
from app.models.angebote import QUOTE_STATUS_LABELS
from app.models.stammdaten import UNITS
from app.services import anmeldung, angebote, auftraege, calc, rechnungen
from app.templating import redirect, render

router = APIRouter(prefix="/angebote")
q_read = require_module("angebote")
q_edit = require_module("angebote", edit=True)
BASE = "/angebote"


def _ip(request: Request) -> str:
    return anmeldung.client_ip(request)


def _quote(db: Session, quote_id: int) -> Quote:
    quote = angebote.get(db, quote_id)
    if quote is None:
        raise HTTPException(404, "Angebot nicht gefunden.")
    return quote


def _header(quote: Quote) -> dict:
    return {"recipient_name": quote.recipient_name or "", "recipient_address": quote.recipient_address or "",
            "object_address": quote.object_address or "", "quote_date": format_date(quote.quote_date),
            "valid_until": format_date(quote.valid_until), "intro_text": quote.intro_text or "",
            "closing_text": quote.closing_text or "", "notes": quote.notes or "",
            "extra_internal_cost": format_decimal(quote.extra_internal_cost) if quote.extra_internal_cost is not None else ""}


def _posted(form) -> tuple[dict, list[dict], list[dict]]:
    keys = ("recipient_name", "recipient_address", "object_address", "quote_date", "valid_until", "intro_text",
            "closing_text", "notes", "extra_internal_cost")
    header = {k: form.get(k) or "" for k in keys}
    positions, costs = [], []
    for i in range(angebote.MAX_ROWS):
        if f"pos-{i}-titel" not in form:
            break
        positions.append({k: form.get(f"pos-{i}-{k}") or "" for k in (
            "id", "titel", "beschreibung", "art", "menge", "einheit", "preis", "stunden", "personen", "satz", "wdh",
            "intervall", "einsaetze", "laufzeit", "kosten", "katalog")})
    for j in range(angebote.MAX_ROWS):
        if f"kost-{j}-block" not in form:
            break
        costs.append({k: form.get(f"kost-{j}-{k}") or "" for k in ("block", "beschreibung", "menge", "einheit", "preis",
                                                                   "satz")})
    return header, positions, costs


def _history(db: Session, ctx: TenantContext, quote: Quote):
    stmt = (select(AuditLog).where(AuditLog.tenant_id == ctx.tenant_id, AuditLog.entity == "angebot",
                                   AuditLog.entity_id == str(quote.id)).order_by(AuditLog.id.desc()).limit(50))
    entries = list(db.scalars(stmt))
    names = {u.id: u.name for u in (db.get(User, e.user_id) for e in entries if e.user_id) if u}
    return [(e, names.get(e.user_id, "–")) for e in entries]


def _page(request: Request, db: Session, ctx: TenantContext, quote: Quote, *, form=None, errors=None,
          extra_positions: int = angebote.EMPTY_POSITIONS, extra_costs: int = angebote.EMPTY_COSTS,
          status_code: int = 200, mail=None, mail_errors=None):
    if form is not None:
        v, positions, costs = _posted(form)
        positions += angebote.empty_position_rows(extra_positions)
        costs += angebote.empty_cost_rows(extra_costs)
    else:
        v, positions, costs = (_header(quote), angebote.position_rows(quote, extra_positions),
                               angebote.cost_rows(quote, extra_costs))
    addresses = []
    if quote.customer_id:
        addresses = list(db.scalars(select(CustomerAddress).where(CustomerAddress.customer_id == quote.customer_id,
                                                                  CustomerAddress.is_active.is_(True))))
    catalog = list(db.scalars(select(CatalogItem).where(CatalogItem.is_active.is_(True)).order_by(CatalogItem.title)))
    show_costs = ctx.has_extra("kalkulation_intern")
    return render(
        request, "angebot.html", status_code=status_code, quote=quote, v=v, positions=positions, costs=costs,
        errors=errors or {}, result=angebote.result(quote), addresses=addresses, catalog=catalog, units=UNITS,
        vat_rates=angebote.VAT_RATES, billing_labels=angebote.BILLING_LABELS, intervals=calc.INTERVALS,
        block_suggestions=angebote.BLOCK_SUGGESTIONS, recurring_on=angebote.recurring_enabled(db),
        small_business=rechnungen.is_small_business(db), show_costs=show_costs,
        can_edit=ctx.can("angebote", edit=True), can_invoice=ctx.can("rechnungen_ausgang", edit=True),
        invoices=angebote.invoices_of(db, quote), history=_history(db, ctx, quote),
        order=auftraege.of_quote(db, quote), can_order=ctx.can("auftraege", edit=True),
        **_mail_context(db, quote, mail, mail_errors),
    )


def _mail_context(db: Session, quote: Quote, mail=None, mail_errors=None) -> dict:
    from app.services import email_entwurf, imap

    if not quote.positions:
        return {"mail": None}
    if mail is None:
        mail = email_entwurf.default_draft(db, "angebot", email_entwurf.placeholders_quote(db, quote),
                                           quote.customer.email if quote.customer else None)
    return {"mail": mail, "mail_errors": mail_errors or {}, "mail_action": f"{BASE}/{quote.id}/email",
            "mail_configured": imap.config_for(db) is not None, "draft_at": quote.email_draft_at}


# ===================================================================== Liste
@router.get("")
def quote_list(request: Request, ctx: TenantContext = Depends(q_read), db: Session = Depends(get_db)):
    p = request.query_params
    quotes = angebote.listing(db, q=p.get("q", ""), status=p.get("status", ""))
    return render(request, "angebote.html", quotes=quotes, q=p.get("q", ""), status=p.get("status", ""),
                  status_labels=QUOTE_STATUS_LABELS, can_edit=ctx.can("angebote", edit=True))


@router.get("/neu")
def new_form(request: Request, ctx: TenantContext = Depends(q_edit), db: Session = Depends(get_db)):
    customers = list(db.scalars(select(Customer).where(Customer.is_active.is_(True)).order_by(Customer.name)))
    return render(request, "angebot_neu.html", customers=customers, preselect=request.query_params.get("kunde", ""),
                  can_create_customer=ctx.can("kunden", edit=True))


@router.post("/neu")
def create(request: Request, customer_id: str = Form(""), ctx: TenantContext = Depends(q_edit),
           db: Session = Depends(get_db)):
    customer = db.get(Customer, int(customer_id)) if customer_id.isdigit() else None
    if customer is None or not customer.is_active:
        return redirect(f"{BASE}/neu", "Bitte einen Kunden auswählen.", "fehler")
    quote = angebote.create(db, customer, user_id=ctx.user.id, ip=_ip(request))
    db.commit()
    return redirect(f"{BASE}/{quote.id}", f"Angebot {quote.number} angelegt – bitte Positionen eintragen.")


# ============================================================ Einzelangebot
@router.get("/{quote_id}")
def detail(request: Request, quote_id: int, ctx: TenantContext = Depends(q_read), db: Session = Depends(get_db)):
    return _page(request, db, ctx, _quote(db, quote_id))


@router.post("/{quote_id}")
async def save(request: Request, quote_id: int, ctx: TenantContext = Depends(q_edit), db: Session = Depends(get_db)):
    quote = _quote(db, quote_id)
    form = await request.form()
    errors = angebote.save_form(db, quote, form, user_id=ctx.user.id, ip=_ip(request),
                                can_see_costs=ctx.has_extra("kalkulation_intern"))
    if errors:
        db.rollback()
        return _page(request, db, ctx, _quote(db, quote_id), form=form, errors=errors, status_code=400,
                     extra_positions=0, extra_costs=0)
    db.commit()
    action = form.get("aktion")
    if action == "pdf":
        return _pdf(db, quote)
    if action == "mehr_zeilen":
        return _page(request, db, ctx, quote, extra_positions=6, extra_costs=4)
    return redirect(f"{BASE}/{quote.id}", "Gespeichert.")


def _pdf(db: Session, quote: Quote) -> Response:
    from app.services import pdf_angebot

    name = f"Angebot-{quote.number.replace('/', '-')}.pdf"
    return Response(pdf_angebot.render(db, quote), media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="{name}"', "X-Frame-Options": "SAMEORIGIN",
        "Content-Security-Policy": "frame-ancestors 'self'"})


@router.get("/{quote_id}/pdf")
def pdf(quote_id: int, ctx: TenantContext = Depends(q_read), db: Session = Depends(get_db)):
    return _pdf(db, _quote(db, quote_id))


@router.post("/{quote_id}/status")
def set_status(request: Request, quote_id: int, status: str = Form(""), ctx: TenantContext = Depends(q_edit),
               db: Session = Depends(get_db)):
    quote = _quote(db, quote_id)
    problem = angebote.set_status(db, quote, status, user_id=ctx.user.id, ip=_ip(request))
    if problem:
        return redirect(f"{BASE}/{quote.id}", problem, "fehler")
    db.commit()
    return redirect(f"{BASE}/{quote.id}", f"Status: {QUOTE_STATUS_LABELS[status]}.")


@router.post("/{quote_id}/rechnung")
def to_invoice(request: Request, quote_id: int, ctx: TenantContext = Depends(q_edit), db: Session = Depends(get_db)):
    if not ctx.can("rechnungen_ausgang", edit=True):
        raise HTTPException(403, "Zum Erstellen von Rechnungen fehlt Ihnen die Berechtigung.")
    quote = _quote(db, quote_id)
    doc, problem = angebote.to_invoice(db, quote, user_id=ctx.user.id, ip=_ip(request))
    if problem:
        db.rollback()
        return redirect(f"{BASE}/{quote.id}", problem, "fehler")
    db.commit()
    return redirect(f"/rechnungen/{doc.id}", f"Rechnungsentwurf aus Angebot {quote.number} erstellt – bitte "
                                             "Leistungsdatum eintragen und prüfen.")


@router.post("/{quote_id}/email")
async def email_draft(request: Request, quote_id: int, ctx: TenantContext = Depends(q_edit),
                      db: Session = Depends(get_db)):
    """Angebot als E-Mail-Entwurf (Postfach) oder .eml – nie automatisch versenden."""
    from app.services import email_entwurf, imap, mailprogramm, pdf_angebot

    quote = _quote(db, quote_id)
    if not quote.positions:
        return redirect(f"{BASE}/{quote.id}", "Das Angebot enthält noch keine Positionen.", "fehler")
    form = await request.form()
    draft = email_entwurf.Draft(to=(form.get("to") or "").strip(), subject=(form.get("subject") or "").strip(),
                                body=form.get("body") or "")
    errors = email_entwurf.check(draft)
    if form.get("aktion") == "entwurf" and not draft.to:
        errors["to"] = "Bitte eine Empfänger-Adresse angeben."
    if errors:
        return _page(request, db, ctx, quote, mail=draft, mail_errors=errors, status_code=400)
    filename = f"Angebot-{quote.number.replace('/', '-')}.pdf"
    msg = email_entwurf.build(db, draft, pdf_angebot.render(db, quote), filename)
    if form.get("aktion") == "eml":
        return Response(bytes(msg), media_type="message/rfc822", headers={
            "Content-Disposition": f'attachment; filename="{filename.replace(".pdf", ".eml")}"'})
    program = mailprogramm.chosen(form)
    try:
        folder = mailprogramm.open_compose(program, msg) if program else email_entwurf.append_to_drafts(db, msg)
    except (imap.ImapError, mailprogramm.MailProgramError) as exc:
        return _page(request, db, ctx, quote, mail=draft, mail_errors={"_": str(exc)}, status_code=400)
    email_entwurf.mark_created(db, quote, entity="angebot", user_id=ctx.user.id, ip=_ip(request), folder=folder,
                               to=draft.to)
    db.commit()
    if program:
        return redirect(f"{BASE}/{quote.id}", mailprogramm.opened_message(folder) + " Nach dem Senden bitte "
                                              "„Als versendet markieren“.")
    return redirect(f"{BASE}/{quote.id}", f"Entwurf im Ordner „{folder}“ angelegt – nach dem Senden bitte "
                                          "„Als versendet markieren“.")


@router.post("/{quote_id}/kopieren")
def duplicate(request: Request, quote_id: int, ctx: TenantContext = Depends(q_edit), db: Session = Depends(get_db)):
    copy = angebote.duplicate(db, _quote(db, quote_id), user_id=ctx.user.id, ip=_ip(request))
    db.commit()
    return redirect(f"{BASE}/{copy.id}", f"Kopie als neues Angebot {copy.number} angelegt.")


@router.post("/{quote_id}/loeschen")
def delete(request: Request, quote_id: int, ctx: TenantContext = Depends(q_edit), db: Session = Depends(get_db)):
    quote = _quote(db, quote_id)
    number = quote.number
    problem = angebote.delete(db, quote, user_id=ctx.user.id, ip=_ip(request))
    if problem:
        return redirect(f"{BASE}/{quote_id}", problem, "fehler")
    db.commit()
    return redirect(BASE, f"Angebot {number} gelöscht.")
