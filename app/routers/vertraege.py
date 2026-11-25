"""Verträge über wiederkehrende Leistungen (Teil von „rechnungen_ausgang“, nur mit Modul „Wiederkehrende Leistungen“)."""
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import TenantContext, require_module
from app.models import Customer, RecurringContract
from app.models.stammdaten import UNITS
from app.models.vertraege import BILLING_MODES, CONTRACT_STATUS_LABELS
from app.services import anmeldung, angebote, rechnungen, vertraege
from app.templating import redirect, render

router = APIRouter(prefix="/vertraege")
contracts_read = require_module("rechnungen_ausgang")
contracts_edit = require_module("rechnungen_ausgang", edit=True)
BASE = "/vertraege"


def _ip(request: Request) -> str:
    return anmeldung.client_ip(request)


def _require_enabled(db: Session) -> None:
    if not vertraege.enabled(db):
        raise HTTPException(404, "Das Modul „Wiederkehrende Leistungen“ ist ausgeschaltet "
                                 "(Einstellungen → Angebote).")


def _contract(db: Session, contract_id: int) -> RecurringContract:
    _require_enabled(db)
    contract = vertraege.get(db, contract_id)
    if contract is None:
        raise HTTPException(404, "Vertrag nicht gefunden.")
    return contract


@router.get("")
def overview(request: Request, ctx: TenantContext = Depends(contracts_read), db: Session = Depends(get_db)):
    _require_enabled(db)
    contracts = vertraege.listing(db)
    customers = list(db.scalars(select(Customer).where(Customer.is_active.is_(True)).order_by(Customer.name)))
    today = date.today()
    due = {c.id: vertraege.due_months(c, today) for c in contracts}
    return render(request, "vertraege.html", contracts=contracts, customers=customers, due=due,
                  due_total=sum(len(m) for m in due.values()), drafts=vertraege.draft_count(db),
                  month_label=vertraege.month_label, can_edit=ctx.can("rechnungen_ausgang", edit=True))


@router.post("/neu")
def create(request: Request, customer_id: str = Form(""), ctx: TenantContext = Depends(contracts_edit),
           db: Session = Depends(get_db)):
    _require_enabled(db)
    customer = db.get(Customer, int(customer_id)) if customer_id.isdigit() else None
    if customer is None or not customer.is_active:
        return redirect(BASE, "Bitte einen Kunden auswählen.", "fehler")
    contract = vertraege.create(db, customer, user_id=ctx.user.id, ip=_ip(request))
    db.commit()
    return redirect(f"{BASE}/{contract.id}", "Vertrag angelegt – bitte Positionen und Beginn eintragen.")


@router.post("/aus-angebot/{quote_id}")
def from_quote(request: Request, quote_id: int, ctx: TenantContext = Depends(contracts_edit),
               db: Session = Depends(get_db)):
    _require_enabled(db)
    quote = angebote.get(db, quote_id)
    if quote is None:
        raise HTTPException(404, "Angebot nicht gefunden.")
    contract, problem = vertraege.from_quote(db, quote, user_id=ctx.user.id, ip=_ip(request))
    if problem:
        db.rollback()
        return redirect(f"/angebote/{quote_id}", problem, "fehler")
    if quote.status != "angenommen":
        angebote.set_status(db, quote, "angenommen", user_id=ctx.user.id, ip=_ip(request))
    db.commit()
    return redirect(f"{BASE}/{contract.id}", "Vertrag angelegt – bitte Beginn und Abrechnungsart prüfen.")


@router.post("/abrechnen")
def run_now(request: Request, ctx: TenantContext = Depends(contracts_edit), db: Session = Depends(get_db)):
    _require_enabled(db)
    created = vertraege.run_due(db, user_id=ctx.user.id, ip=_ip(request))
    db.commit()
    if not created:
        return redirect(BASE, "Zurzeit ist kein Monat abzurechnen.")
    return redirect("/rechnungen?status=entwurf", f"{len(created)} Rechnungsentwürfe angelegt – bitte prüfen und "
                                                  "festschreiben.")


def _page(request: Request, db: Session, ctx: TenantContext, contract: RecurringContract, *, form=None,
          errors=None, status_code: int = 200):
    if form is not None:
        values = {k: form.get(k) or "" for k in vertraege.header_values(contract)}
        rows = [{k: form.get(f"pos-{i}-{k}") or "" for k in ("titel", "beschreibung", "menge", "einheit", "preis",
                                                              "satz")}
                for i in range(vertraege.MAX_LINES) if f"pos-{i}-titel" in form]
    else:
        values, rows = vertraege.header_values(contract), vertraege.line_rows(contract)
    return render(request, "vertrag.html", status_code=status_code, contract=contract, v=values, rows=rows,
                  errors=errors or {}, modes=BILLING_MODES, statuses=CONTRACT_STATUS_LABELS, units=UNITS,
                  vat_rates=rechnungen.VAT_RATES, invoices=vertraege.invoices_of(db, contract),
                  next_label=vertraege.month_label(contract.next_month),
                  next_due=vertraege.billable_from(contract, contract.next_month),
                  can_edit=ctx.can("rechnungen_ausgang", edit=True))


@router.get("/{contract_id}")
def detail(request: Request, contract_id: int, ctx: TenantContext = Depends(contracts_read),
           db: Session = Depends(get_db)):
    return _page(request, db, ctx, _contract(db, contract_id))


@router.post("/{contract_id}")
async def save(request: Request, contract_id: int, ctx: TenantContext = Depends(contracts_edit),
               db: Session = Depends(get_db)):
    contract = _contract(db, contract_id)
    form = await request.form()
    errors = vertraege.save_form(db, contract, form, user_id=ctx.user.id, ip=_ip(request))
    if errors:
        db.rollback()
        return _page(request, db, ctx, _contract(db, contract_id), form=form, errors=errors, status_code=400)
    db.commit()
    return redirect(f"{BASE}/{contract.id}", "Vertrag gespeichert.")
