"""Kunden (mit Objekt-Adressen), Lieferanten und Artikel-/Leistungskatalog.

Rechte: Kunden und Lieferanten über das Modul „kunden“, der Katalog über „katalog“.
Alle Datensätze werden über db.get/select geladen – der Firmen-Filter greift dabei
automatisch (app/database.py); fremde IDs ergeben „nicht gefunden“.
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import TenantContext, require_module
from app.forms import display_values, posted_values
from app.models import CatalogItem, Customer, CustomerAddress, Supplier
from app.services import anmeldung, einstellungen
from app.services import stammdaten as svc
from app.services.stammdaten import CATALOG, CUSTOMER, CUSTOMER_ADDRESS, SUPPLIER, Kind, Section
from app.templating import redirect, render

router = APIRouter()

customers_read = require_module("kunden")
customers_edit = require_module("kunden", edit=True)
catalog_read = require_module("katalog")
catalog_edit = require_module("katalog", edit=True)

NUMBER_CONFLICT = "Die Nummer wurde gerade anderweitig vergeben. Bitte erneut speichern."


def _get(db: Session, model, obj_id: int):
    obj = db.get(model, obj_id)
    if obj is None:
        raise HTTPException(404, "Nicht gefunden.")
    return obj


def _form_page(
    request: Request, *, kind: Kind, title: str, action: str, back_url: str, values: dict, errors: dict,
    sections: tuple[Section, ...] | None = None, disabled: bool = False, status_code: int = 200,
    archive_url: str | None = None, obj=None,
):
    return render(
        request, "stammdaten_form.html", status_code=status_code, kind=kind, title=title, action=action,
        back_url=back_url, values=values, errors=errors, sections=sections or kind.sections, disabled=disabled,
        archive_url=archive_url, obj=obj,
    )


async def _save(request: Request, db: Session, ctx: TenantContext, kind: Kind, obj, *, sections=None, extra=None):
    """Formular speichern. Gibt (obj, errors, form) zurück; commit bei Erfolg."""
    form = await request.form()
    fields = tuple(f for s in (sections or kind.sections) for f in s.fields)
    saved, errors = svc.save(db, kind, obj, form, user_id=ctx.user.id, ip=anmeldung.client_ip(request),
                             fields=fields, extra=extra)
    if errors:
        db.rollback()
        return None, errors, form
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None, {"number": NUMBER_CONFLICT}, form
    return saved, {}, form


def _list_params(request: Request) -> tuple[str, bool]:
    return request.query_params.get("q", ""), request.query_params.get("archiv") == "1"


def _toggle(request: Request, db: Session, ctx: TenantContext, kind: Kind, obj, aktiv: str) -> bool:
    active = aktiv == "1"
    svc.set_active(db, kind, obj, active, user_id=ctx.user.id, ip=anmeldung.client_ip(request))
    db.commit()
    return active


# ===================================================================== Kunden
@router.get("/kunden")
def customer_list(request: Request, ctx: TenantContext = Depends(customers_read), db: Session = Depends(get_db)):
    q, archived = _list_params(request)
    rows, more = svc.search(db, CUSTOMER, q, archived)
    return render(request, "kunden_liste.html", rows=rows, more=more, q=q, archived=archived,
                  can_edit=ctx.can("kunden", edit=True))


@router.get("/kunden/neu")
def customer_new(request: Request, ctx: TenantContext = Depends(customers_edit), db: Session = Depends(get_db)):
    values = display_values(CUSTOMER.fields, svc.form_source(None, CUSTOMER))
    return _form_page(request, kind=CUSTOMER, title="Neuer Kunde", action="/kunden/neu", back_url="/kunden",
                      values=values, errors={})


@router.post("/kunden/neu")
async def customer_create(request: Request, ctx: TenantContext = Depends(customers_edit),
                          db: Session = Depends(get_db)):
    obj, errors, form = await _save(request, db, ctx, CUSTOMER, None)
    if errors:
        return _form_page(request, kind=CUSTOMER, title="Neuer Kunde", action="/kunden/neu", back_url="/kunden",
                          values=posted_values(CUSTOMER.fields, form), errors=errors, status_code=400)
    return redirect(f"/kunden/{obj.id}", f"Kunde {obj.number} „{obj.name}“ angelegt.")


@router.get("/kunden/{customer_id}")
def customer_detail(request: Request, customer_id: int, ctx: TenantContext = Depends(customers_read),
                    db: Session = Depends(get_db)):
    customer = _get(db, Customer, customer_id)
    address_values = display_values(CUSTOMER_ADDRESS.fields, svc.form_source(None, CUSTOMER_ADDRESS))
    return render(request, "kunde.html", c=customer, can_edit=ctx.can("kunden", edit=True),
                  address_kind=CUSTOMER_ADDRESS, address_values=address_values, address_errors={},
                  default_terms=einstellungen.get(db, "design.zahlungsziel_tage"), **_invoice_info(db, ctx, customer))


def _invoice_info(db: Session, ctx: TenantContext, customer: Customer) -> dict:
    """Angebote, Rechnungen, Umsatz und offene Beträge – jeweils nur mit dem passenden Recht."""
    from app.services import angebote, rechnungen

    info: dict = {"invoices": None, "quotes": None}
    if ctx.can("rechnungen_ausgang"):
        info["invoices"] = rechnungen.customer_summary(db, customer.id)
        info["can_invoice"] = ctx.can("rechnungen_ausgang", edit=True)
    if ctx.can("angebote"):
        info["quotes"] = angebote.listing(db, customer_id=customer.id, limit=50)
        info["can_quote"] = ctx.can("angebote", edit=True)
    return info


@router.get("/kunden/{customer_id}/bearbeiten")
def customer_edit_form(request: Request, customer_id: int, ctx: TenantContext = Depends(customers_edit),
                       db: Session = Depends(get_db)):
    customer = _get(db, Customer, customer_id)
    return _form_page(request, kind=CUSTOMER, title=f"Kunde {customer.number} bearbeiten",
                      action=f"/kunden/{customer.id}/bearbeiten", back_url=f"/kunden/{customer.id}",
                      values=display_values(CUSTOMER.fields, svc.form_source(customer, CUSTOMER)), errors={},
                      archive_url=f"/kunden/{customer.id}/archivieren", obj=customer)


@router.post("/kunden/{customer_id}/bearbeiten")
async def customer_update(request: Request, customer_id: int, ctx: TenantContext = Depends(customers_edit),
                          db: Session = Depends(get_db)):
    customer = _get(db, Customer, customer_id)
    obj, errors, form = await _save(request, db, ctx, CUSTOMER, customer)
    if errors:
        return _form_page(request, kind=CUSTOMER, title=f"Kunde {customer.number} bearbeiten",
                          action=f"/kunden/{customer.id}/bearbeiten", back_url=f"/kunden/{customer.id}",
                          values=posted_values(CUSTOMER.fields, form), errors=errors, status_code=400,
                          archive_url=f"/kunden/{customer.id}/archivieren", obj=customer)
    return redirect(f"/kunden/{obj.id}", "Änderungen gespeichert.")


@router.post("/kunden/{customer_id}/archivieren")
def customer_archive(request: Request, customer_id: int, aktiv: str = Form("0"),
                     ctx: TenantContext = Depends(customers_edit), db: Session = Depends(get_db)):
    customer = _get(db, Customer, customer_id)
    active = _toggle(request, db, ctx, CUSTOMER, customer, aktiv)
    return redirect(f"/kunden/{customer.id}", "Kunde wieder aktiviert." if active else "Kunde archiviert.")


# ---------------------------------------------------------- Objekt-Adressen
@router.post("/kunden/{customer_id}/adressen")
async def address_create(request: Request, customer_id: int, ctx: TenantContext = Depends(customers_edit),
                         db: Session = Depends(get_db)):
    customer = _get(db, Customer, customer_id)
    obj, errors, form = await _save(request, db, ctx, CUSTOMER_ADDRESS, None, extra={"customer_id": customer.id})
    if errors:
        customer = _get(db, Customer, customer_id)
        return render(request, "kunde.html", status_code=400, c=customer, can_edit=True,
                      address_kind=CUSTOMER_ADDRESS, address_values=posted_values(CUSTOMER_ADDRESS.fields, form),
                      address_errors=errors, default_terms=einstellungen.get(db, "design.zahlungsziel_tage"),
                      **_invoice_info(db, ctx, customer))
    return redirect(f"/kunden/{customer.id}", f"Objektadresse „{obj.label}“ hinzugefügt.")


def _get_address(db: Session, customer_id: int, address_id: int) -> CustomerAddress:
    address = _get(db, CustomerAddress, address_id)
    if address.customer_id != customer_id:
        raise HTTPException(404, "Nicht gefunden.")
    return address


@router.get("/kunden/{customer_id}/adressen/{address_id}")
def address_edit_form(request: Request, customer_id: int, address_id: int,
                      ctx: TenantContext = Depends(customers_edit), db: Session = Depends(get_db)):
    address = _get_address(db, customer_id, address_id)
    return _form_page(request, kind=CUSTOMER_ADDRESS, title=f"Objektadresse – {address.customer.name}",
                      action=f"/kunden/{customer_id}/adressen/{address_id}", back_url=f"/kunden/{customer_id}",
                      values=display_values(CUSTOMER_ADDRESS.fields, svc.form_source(address, CUSTOMER_ADDRESS)),
                      errors={}, archive_url=f"/kunden/{customer_id}/adressen/{address_id}/archivieren", obj=address)


@router.post("/kunden/{customer_id}/adressen/{address_id}")
async def address_update(request: Request, customer_id: int, address_id: int,
                         ctx: TenantContext = Depends(customers_edit), db: Session = Depends(get_db)):
    address = _get_address(db, customer_id, address_id)
    obj, errors, form = await _save(request, db, ctx, CUSTOMER_ADDRESS, address)
    if errors:
        return _form_page(request, kind=CUSTOMER_ADDRESS, title="Objektadresse",
                          action=f"/kunden/{customer_id}/adressen/{address_id}", back_url=f"/kunden/{customer_id}",
                          values=posted_values(CUSTOMER_ADDRESS.fields, form), errors=errors, status_code=400,
                          archive_url=f"/kunden/{customer_id}/adressen/{address_id}/archivieren", obj=address)
    return redirect(f"/kunden/{customer_id}", "Objektadresse gespeichert.")


@router.post("/kunden/{customer_id}/adressen/{address_id}/archivieren")
def address_archive(request: Request, customer_id: int, address_id: int, aktiv: str = Form("0"),
                    ctx: TenantContext = Depends(customers_edit), db: Session = Depends(get_db)):
    address = _get_address(db, customer_id, address_id)
    active = _toggle(request, db, ctx, CUSTOMER_ADDRESS, address, aktiv)
    return redirect(f"/kunden/{customer_id}",
                    "Objektadresse wieder aktiviert." if active else "Objektadresse archiviert.")


# ================================================================ Lieferanten
@router.get("/lieferanten")
def supplier_list(request: Request, ctx: TenantContext = Depends(customers_read), db: Session = Depends(get_db)):
    q, archived = _list_params(request)
    rows, more = svc.search(db, SUPPLIER, q, archived)
    return render(request, "lieferanten_liste.html", rows=rows, more=more, q=q, archived=archived,
                  can_edit=ctx.can("kunden", edit=True))


@router.get("/lieferanten/neu")
def supplier_new(request: Request, ctx: TenantContext = Depends(customers_edit)):
    return _form_page(request, kind=SUPPLIER, title="Neuer Lieferant", action="/lieferanten/neu",
                      back_url="/lieferanten",
                      values=display_values(SUPPLIER.fields, svc.form_source(None, SUPPLIER)), errors={})


@router.post("/lieferanten/neu")
async def supplier_create(request: Request, ctx: TenantContext = Depends(customers_edit),
                          db: Session = Depends(get_db)):
    obj, errors, form = await _save(request, db, ctx, SUPPLIER, None)
    if errors:
        return _form_page(request, kind=SUPPLIER, title="Neuer Lieferant", action="/lieferanten/neu",
                          back_url="/lieferanten", values=posted_values(SUPPLIER.fields, form), errors=errors,
                          status_code=400)
    return redirect("/lieferanten", f"Lieferant {obj.number} „{obj.name}“ angelegt.")


@router.get("/lieferanten/{supplier_id}")
def supplier_detail(request: Request, supplier_id: int, ctx: TenantContext = Depends(customers_read),
                    db: Session = Depends(get_db)):
    supplier = _get(db, Supplier, supplier_id)
    can_edit = ctx.can("kunden", edit=True)
    return _form_page(request, kind=SUPPLIER, title=f"Lieferant {supplier.number} – {supplier.name}",
                      action=f"/lieferanten/{supplier.id}", back_url="/lieferanten",
                      values=display_values(SUPPLIER.fields, svc.form_source(supplier, SUPPLIER)), errors={},
                      disabled=not can_edit, archive_url=f"/lieferanten/{supplier.id}/archivieren" if can_edit else None,
                      obj=supplier)


@router.post("/lieferanten/{supplier_id}")
async def supplier_update(request: Request, supplier_id: int, ctx: TenantContext = Depends(customers_edit),
                          db: Session = Depends(get_db)):
    supplier = _get(db, Supplier, supplier_id)
    obj, errors, form = await _save(request, db, ctx, SUPPLIER, supplier)
    if errors:
        return _form_page(request, kind=SUPPLIER, title=f"Lieferant {supplier.number}",
                          action=f"/lieferanten/{supplier.id}", back_url="/lieferanten",
                          values=posted_values(SUPPLIER.fields, form), errors=errors, status_code=400,
                          archive_url=f"/lieferanten/{supplier.id}/archivieren", obj=supplier)
    return redirect("/lieferanten", f"Lieferant „{obj.name}“ gespeichert.")


@router.post("/lieferanten/{supplier_id}/archivieren")
def supplier_archive(request: Request, supplier_id: int, aktiv: str = Form("0"),
                     ctx: TenantContext = Depends(customers_edit), db: Session = Depends(get_db)):
    supplier = _get(db, Supplier, supplier_id)
    active = _toggle(request, db, ctx, SUPPLIER, supplier, aktiv)
    return redirect(f"/lieferanten/{supplier.id}", "Lieferant wieder aktiviert." if active else "Lieferant archiviert.")


# ==================================================================== Katalog
def _catalog_sections(ctx: TenantContext):
    return svc.visible_sections(CATALOG, ctx.has_extra("kalkulation_intern"))


def _catalog_values(db: Session, item: CatalogItem | None, sections) -> dict:
    fields = tuple(f for s in sections for f in s.fields)
    source = svc.form_source(item, CATALOG)
    if item is None:
        source["vat_rate"] = einstellungen.get(db, "design.ust_satz") or "19"
    return display_values(fields, source)


@router.get("/katalog")
def catalog_list(request: Request, ctx: TenantContext = Depends(catalog_read), db: Session = Depends(get_db)):
    q, archived = _list_params(request)
    rows, more = svc.search(db, CATALOG, q, archived, order_by="title")
    return render(request, "katalog_liste.html", rows=rows, more=more, q=q, archived=archived,
                  can_edit=ctx.can("katalog", edit=True), show_costs=ctx.has_extra("kalkulation_intern"))


@router.get("/katalog/neu")
def catalog_new(request: Request, ctx: TenantContext = Depends(catalog_edit), db: Session = Depends(get_db)):
    sections = _catalog_sections(ctx)
    return _form_page(request, kind=CATALOG, title="Neuer Katalog-Eintrag", action="/katalog/neu",
                      back_url="/katalog", values=_catalog_values(db, None, sections), errors={}, sections=sections)


@router.post("/katalog/neu")
async def catalog_create(request: Request, ctx: TenantContext = Depends(catalog_edit),
                         db: Session = Depends(get_db)):
    sections = _catalog_sections(ctx)
    obj, errors, form = await _save(request, db, ctx, CATALOG, None, sections=sections)
    if errors:
        fields = tuple(f for s in sections for f in s.fields)
        return _form_page(request, kind=CATALOG, title="Neuer Katalog-Eintrag", action="/katalog/neu",
                          back_url="/katalog", values=posted_values(fields, form), errors=errors,
                          sections=sections, status_code=400)
    return redirect("/katalog", f"„{obj.title}“ angelegt.")


@router.get("/katalog/{item_id}")
def catalog_detail(request: Request, item_id: int, ctx: TenantContext = Depends(catalog_read),
                   db: Session = Depends(get_db)):
    item = _get(db, CatalogItem, item_id)
    sections = _catalog_sections(ctx)
    can_edit = ctx.can("katalog", edit=True)
    return _form_page(request, kind=CATALOG, title=item.title, action=f"/katalog/{item.id}", back_url="/katalog",
                      values=_catalog_values(db, item, sections), errors={}, sections=sections,
                      disabled=not can_edit, archive_url=f"/katalog/{item.id}/archivieren" if can_edit else None,
                      obj=item)


@router.post("/katalog/{item_id}")
async def catalog_update(request: Request, item_id: int, ctx: TenantContext = Depends(catalog_edit),
                         db: Session = Depends(get_db)):
    item = _get(db, CatalogItem, item_id)
    sections = _catalog_sections(ctx)
    obj, errors, form = await _save(request, db, ctx, CATALOG, item, sections=sections)
    if errors:
        fields = tuple(f for s in sections for f in s.fields)
        return _form_page(request, kind=CATALOG, title=item.title, action=f"/katalog/{item.id}",
                          back_url="/katalog", values=posted_values(fields, form), errors=errors, sections=sections,
                          status_code=400, archive_url=f"/katalog/{item.id}/archivieren", obj=item)
    return redirect("/katalog", f"„{obj.title}“ gespeichert.")


@router.post("/katalog/{item_id}/archivieren")
def catalog_archive(request: Request, item_id: int, aktiv: str = Form("0"),
                    ctx: TenantContext = Depends(catalog_edit), db: Session = Depends(get_db)):
    item = _get(db, CatalogItem, item_id)
    active = _toggle(request, db, ctx, CATALOG, item, aktiv)
    return redirect(f"/katalog/{item.id}", "Eintrag wieder aktiviert." if active else "Eintrag archiviert.")
