"""Aufträge / Projektübersicht (Modul „auftraege“).

Preise sieht nur, wer auch Angebote oder Ausgangsrechnungen sehen darf. Rechnung bzw. Vertrag aus einem
Auftrag braucht zusätzlich das Recht „rechnungen_ausgang“ (bearbeiten).
"""
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import TenantContext, require_module
from app.models import Customer, Order, OrderTask
from app.models.auftraege import ORDER_KIND_LABELS, ORDER_STATUS_LABELS
from app.models.stammdaten import UNITS
from app.services import anmeldung, angebote, auftraege, rechnungen, vertraege
from app.templating import redirect, render

router = APIRouter(prefix="/auftraege")
o_read = require_module("auftraege")
o_edit = require_module("auftraege", edit=True)
BASE = "/auftraege"


def _ip(request: Request) -> str:
    return anmeldung.client_ip(request)


def _order(db: Session, order_id: int) -> Order:
    order = auftraege.get(db, order_id)
    if order is None:
        raise HTTPException(404, "Auftrag nicht gefunden.")
    return order


def _show_prices(ctx: TenantContext) -> bool:
    return ctx.can("angebote") or ctx.can("rechnungen_ausgang")


def _back(raw: str | None, order: Order | None = None) -> str:
    """Rücksprung in die Übersicht (mit Filtern und aufgeklappter Karte) – nur innerhalb von /auftraege."""
    url = raw or BASE
    if not (url == BASE or url.startswith(BASE + "?")):
        url = BASE
    if order is not None:
        key = auftraege.object_of(order)[0]
        url += ("&" if "?" in url else "?") + urlencode({"offen": key}) + f"#objekt-{key}"
    return url


@router.get("")
def overview(request: Request, status: str = "", art: str = "", q: str = "", sort: str = "datum", offen: str = "",
             ctx: TenantContext = Depends(o_read), db: Session = Depends(get_db)):
    status = status if status in ORDER_STATUS_LABELS else ""
    art = art if art in ORDER_KIND_LABELS else ""
    sort = sort if sort in auftraege.SORTS else "datum"
    # Rechnungen, angenommene Angebote und Verträge ohne Auftrag automatisch übernehmen
    synced = auftraege.sync(db)
    db.commit()
    found = auftraege.cards(db, status=status, kind=art, q=q.strip(), sort=sort)
    orders = [o for c in found for o in c.orders]
    docs = auftraege.documents_of(db, orders)
    suggest = {o.id for o in orders if auftraege.suggest_done(o, docs[o.id])}
    params = {k: v for k, v in (("status", status), ("art", art), ("q", q.strip()), ("sort", sort)) if v}
    here = BASE + ("?" + urlencode(params) if params else "")
    customers = list(db.scalars(select(Customer).where(Customer.is_active.is_(True)).order_by(Customer.name)))
    return render(request, "auftraege.html", cards=found, docs=docs, suggest=suggest, tabs=auftraege.TABS,
                  counts=auftraege.counts(db), sorts=auftraege.SORTS, kinds=ORDER_KIND_LABELS,
                  statuses=ORDER_STATUS_LABELS, status=status, art=art, q=q.strip(), sort=sort, offen=offen,
                  params=params, here=here, customers=customers, show_prices=_show_prices(ctx), synced=synced,
                  can_edit=ctx.can("auftraege", edit=True),
                  can_invoice=ctx.can("rechnungen_ausgang", edit=True),
                  contracts_on=vertraege.enabled(db), can_quotes=ctx.can("angebote"),
                  can_invoices=ctx.can("rechnungen_ausgang"))


@router.post("/neu")
def create(request: Request, customer_id: str = Form(""), ctx: TenantContext = Depends(o_edit),
           db: Session = Depends(get_db)):
    customer = db.get(Customer, int(customer_id)) if customer_id.isdigit() else None
    if customer is None or not customer.is_active:
        return redirect(BASE, "Bitte einen Kunden auswählen.", "fehler")
    order = auftraege.create(db, customer, user_id=ctx.user.id, ip=_ip(request))
    db.commit()
    return redirect(f"{BASE}/{order.id}", f"Auftrag {order.number} angelegt – bitte Objekt und Leistungen eintragen.")


@router.post("/aus-angebot/{quote_id}")
def from_quote(request: Request, quote_id: int, ctx: TenantContext = Depends(o_edit),
               db: Session = Depends(get_db)):
    quote = angebote.get(db, quote_id)
    if quote is None:
        raise HTTPException(404, "Angebot nicht gefunden.")
    existing = auftraege.of_quote(db, quote)
    order, problem = auftraege.from_quote(db, quote, user_id=ctx.user.id, ip=_ip(request))
    if problem:
        db.rollback()
        return redirect(f"/angebote/{quote_id}", problem, "fehler")
    db.commit()
    if existing is not None:
        return redirect(_back(BASE, order), f"Zu diesem Angebot gibt es schon den Auftrag {order.number}.")
    return redirect(_back(BASE, order), f"Auftrag {order.number} aus Angebot {quote.number} angelegt.")


def _page(request: Request, db: Session, ctx: TenantContext, order: Order, *, form=None, errors=None,
          status_code: int = 200):
    if form is not None:
        values = {k: form.get(k) or "" for k in auftraege.header_values(order)}
        rows = [{k: form.get(f"pos-{i}-{k}") or "" for k in ("titel", "beschreibung", "menge", "einheit", "preis",
                                                              "satz")}
                for i in range(auftraege.MAX_LINES) if f"pos-{i}-titel" in form]
        tasks = [{"text": form.get(f"aufgabe-{i}-text") or "", "erledigt": bool(form.get(f"aufgabe-{i}-erledigt"))}
                 for i in range(auftraege.MAX_TASKS) if f"aufgabe-{i}-text" in form]
    else:
        values, rows, tasks = auftraege.header_values(order), auftraege.line_rows(order), auftraege.task_rows(order)
    docs = auftraege.documents_of(db, [order])[order.id]
    addresses = [a for a in order.customer.addresses if a.is_active or a.id == order.address_id]
    return render(request, "auftrag.html", status_code=status_code, order=order, v=values, rows=rows, tasks=tasks,
                  errors=errors or {}, kinds=ORDER_KIND_LABELS, statuses=ORDER_STATUS_LABELS, units=UNITS,
                  vat_rates=rechnungen.VAT_RATES, addresses=addresses, docs=docs,
                  suggest=auftraege.suggest_done(order, docs), show_prices=_show_prices(ctx),
                  can_edit=ctx.can("auftraege", edit=True), can_invoice=ctx.can("rechnungen_ausgang", edit=True),
                  contracts_on=vertraege.enabled(db), can_quotes=ctx.can("angebote"),
                  can_invoices=ctx.can("rechnungen_ausgang"), back=_back(BASE, order))


@router.get("/{order_id}")
def detail(request: Request, order_id: int, ctx: TenantContext = Depends(o_read), db: Session = Depends(get_db)):
    return _page(request, db, ctx, _order(db, order_id))


@router.post("/{order_id}")
async def save(request: Request, order_id: int, ctx: TenantContext = Depends(o_edit),
               db: Session = Depends(get_db)):
    order = _order(db, order_id)
    form = await request.form()
    errors = auftraege.save_form(db, order, form, user_id=ctx.user.id, ip=_ip(request),
                                 with_prices=_show_prices(ctx))
    if errors:
        db.rollback()
        return _page(request, db, ctx, _order(db, order_id), form=form, errors=errors, status_code=400)
    db.commit()
    return redirect(f"{BASE}/{order.id}", "Auftrag gespeichert.")


@router.post("/{order_id}/stand")
def set_status(request: Request, order_id: int, status: str = Form(""), zurueck: str = Form(""),
               ctx: TenantContext = Depends(o_edit), db: Session = Depends(get_db)):
    order = _order(db, order_id)
    problem = auftraege.set_status(db, order, status, user_id=ctx.user.id, ip=_ip(request))
    if problem:
        db.rollback()
        return redirect(_back(zurueck, order), problem, "fehler")
    db.commit()
    return redirect(_back(zurueck, order), f"{order.number}: {order.status_label}.")


@router.post("/aufgabe/{task_id}")
def toggle_task(request: Request, task_id: int, zurueck: str = Form(""), ctx: TenantContext = Depends(o_edit),
                db: Session = Depends(get_db)):
    task = db.get(OrderTask, task_id)
    if task is None:
        raise HTTPException(404, "Aufgabe nicht gefunden.")
    auftraege.toggle_task(db, task, user_id=ctx.user.id, ip=_ip(request))
    db.commit()
    return redirect(_back(zurueck, task.order))


@router.post("/{order_id}/rechnung")
def to_invoice(request: Request, order_id: int, ctx: TenantContext = Depends(o_edit), db: Session = Depends(get_db)):
    if not ctx.can("rechnungen_ausgang", edit=True):
        raise HTTPException(403, "Zum Erstellen von Rechnungen fehlt Ihnen die Berechtigung.")
    order = _order(db, order_id)
    doc, problem = auftraege.to_invoice(db, order, user_id=ctx.user.id, ip=_ip(request))
    if problem:
        db.rollback()
        return redirect(f"{BASE}/{order.id}", problem, "fehler")
    db.commit()
    return redirect(f"/rechnungen/{doc.id}", f"Rechnungsentwurf aus Auftrag {order.number} erstellt – bitte "
                                             "Leistungsdatum eintragen und prüfen.")


@router.post("/{order_id}/vertrag")
def to_contract(request: Request, order_id: int, ctx: TenantContext = Depends(o_edit),
                db: Session = Depends(get_db)):
    if not ctx.can("rechnungen_ausgang", edit=True):
        raise HTTPException(403, "Zum Anlegen von Verträgen fehlt Ihnen die Berechtigung.")
    order = _order(db, order_id)
    contract, problem = auftraege.to_contract(db, order, user_id=ctx.user.id, ip=_ip(request))
    if problem:
        db.rollback()
        return redirect(f"{BASE}/{order.id}", problem, "fehler")
    db.commit()
    return redirect(f"/vertraege/{contract.id}", f"Vertrag aus Auftrag {order.number} angelegt – bitte Beginn und "
                                                 "Abrechnungsart prüfen.")


@router.post("/{order_id}/loeschen")
def delete(request: Request, order_id: int, ctx: TenantContext = Depends(o_edit), db: Session = Depends(get_db)):
    order = _order(db, order_id)
    number = order.number
    problem = auftraege.delete(db, order, user_id=ctx.user.id, ip=_ip(request))
    if problem:
        db.rollback()
        return redirect(f"{BASE}/{order_id}", problem, "fehler")
    db.commit()
    return redirect(BASE, f"Auftrag {number} gelöscht.")
