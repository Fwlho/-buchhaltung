"""Alte Ausgangsrechnungen hochladen und prüfen (/rechnungen/import, Modul „rechnungen_ausgang“).

Wird in app/main.py VOR dem Rechnungs-Router eingebunden, weil dort /rechnungen/{doc_id} liegt.
Logik in app/services/rechnungs_import.py.
"""
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import TenantContext, require_module
from app.formatting import format_date, format_decimal
from app.models import Document
from app.routers.belege import _file_response, _steps
from app.services import ablage, ablage_ordner, anmeldung, belege, rechnungs_import
from app.templating import redirect, render

router = APIRouter(prefix="/rechnungen/import")

out_read = require_module("rechnungen_ausgang")
out_edit = require_module("rechnungen_ausgang", edit=True)
BASE = "/rechnungen/import"


def _doc(db: Session, doc_id: int) -> Document:
    doc = rechnungs_import.get_pending(db, doc_id)
    if doc is None:
        raise HTTPException(404, "Diese hochgeladene Rechnung gibt es nicht (mehr) im Prüfbereich.")
    return doc


def _values(doc: Document) -> tuple[dict, list[dict]]:
    v = {"number": doc.number or "", "doc_date": format_date(doc.doc_date),
         "service_date": format_date(doc.service_date), "due_date": format_date(doc.due_date),
         "customer_id": str(doc.customer_id or ""), "recipient_name": doc.recipient_name or "",
         "recipient_address": doc.recipient_address or "", "notes": doc.notes or "", "paid_on": ""}
    rows = [{"satz": r["rate"], "netto": format_decimal(r["net"]) if r["net"] is not None else "",
             "ust": format_decimal(r["vat"]) if r["vat"] is not None else "",
             "brutto": format_decimal(r["gross"]) if r["gross"] is not None else ""}
            for r in belege.line_rows(doc)]
    return v, rows


def _posted(form) -> tuple[dict, list[dict]]:
    keys = ("number", "doc_date", "service_date", "due_date", "customer_id", "recipient_name",
            "recipient_address", "notes", "paid_on")
    rows = []
    for i in range(belege.MAX_LINES):
        if f"zeile-{i}-satz" not in form:
            break
        rows.append({k: form.get(f"zeile-{i}-{k}") or "" for k in ("satz", "netto", "ust", "brutto")})
    return {k: form.get(k) or "" for k in keys}, rows


def _detail(request: Request, db: Session, ctx: TenantContext, doc: Document, *, form=None, errors=None,
            status_code: int = 200):
    v, rows = _posted(form) if form is not None else _values(doc)
    return render(request, "rechnung_import.html", status_code=status_code, doc=doc, v=v, rows=rows,
                  errors=errors or {}, customers=rechnungs_import.customers(db), vat_rates=belege.VAT_RATES,
                  can_edit=ctx.can("rechnungen_ausgang", edit=True), can_finalize=ctx.has_extra("festschreiben"),
                  **_steps([d.id for d in rechnungs_import.pending(db)], doc.id, BASE))


@router.get("")
def overview(request: Request, ctx: TenantContext = Depends(out_read), db: Session = Depends(get_db)):
    from app.routers.belege import _folder_context

    return render(request, "rechnungen_import.html", docs=rechnungs_import.pending(db),
                  can_edit=ctx.can("rechnungen_ausgang", edit=True), can_settings=ctx.can("einstellungen", edit=True),
                  **_folder_context(db, "ausgang"))


@router.post("/hochladen")
async def upload(request: Request, dateien: list[UploadFile] = File(default=[]),
                 ctx: TenantContext = Depends(out_edit), db: Session = Depends(get_db)):
    created, problems = [], []
    for upload_file in dateien:
        if not upload_file.filename:
            continue
        data = await upload_file.read(ablage.MAX_UPLOAD_BYTES + 1)
        try:
            doc = rechnungs_import.create_from_bytes(db, data, upload_file.filename, tenant_id=ctx.tenant_id,
                                                     user_id=ctx.user.id, ip=anmeldung.client_ip(request))
            db.commit()
            created.append(doc.id)
        except belege.DuplicateUpload as exc:
            db.rollback()
            where = "im Prüfbereich" if exc.document.pending else f"als Rechnung {exc.document.number or ''}"
            problems.append(f"„{upload_file.filename}“ wurde bereits hochgeladen (liegt {where}).")
        except ablage.UploadRejected as exc:
            db.rollback()
            problems.append(f"„{upload_file.filename}“: {exc}")
    if not created:
        return render(request, "rechnungen_import.html", status_code=400, docs=rechnungs_import.pending(db),
                      can_edit=True, error=" ".join(problems) or "Bitte mindestens eine PDF-Datei auswählen.")
    message = f"{len(created)} Rechnung(en) hochgeladen und ausgelesen – bitte prüfen und übernehmen."
    if problems:
        message += " Nicht übernommen: " + " ".join(problems)
    return redirect(f"{BASE}/{created[0]}" if len(created) == 1 else BASE, message, "hinweis" if problems else "ok")


@router.post("/ordner-einlesen")
def scan_folder(request: Request, ctx: TenantContext = Depends(out_edit), db: Session = Depends(get_db)):
    from app.services import eingangsordner

    result = eingangsordner.scan(db, ctx.tenant_id, kinds=("ausgang",))
    return redirect(BASE, result.message, "hinweis" if result.problems or result.busy else "ok")


@router.get("/{doc_id}")
def detail(request: Request, doc_id: int, ctx: TenantContext = Depends(out_read), db: Session = Depends(get_db)):
    return _detail(request, db, ctx, _doc(db, doc_id))


@router.post("/{doc_id}")
async def save(request: Request, doc_id: int, ctx: TenantContext = Depends(out_edit), db: Session = Depends(get_db)):
    doc = _doc(db, doc_id)
    form = await request.form()
    take_over = form.get("aktion") == "uebernehmen"
    if take_over and not ctx.has_extra("festschreiben"):
        raise HTTPException(403, "Zum Übernehmen (= Festschreiben) fehlt Ihnen das Sonderrecht „Festschreiben“.")
    if take_over:
        errors = rechnungs_import.take_over(db, doc, form, user_id=ctx.user.id, ip=anmeldung.client_ip(request))
    else:
        errors = rechnungs_import.save_form(db, doc, form, user_id=ctx.user.id, ip=anmeldung.client_ip(request))
    if errors:
        db.rollback()
        return _detail(request, db, ctx, _doc(db, doc_id), form=form, errors=errors, status_code=400)
    db.commit()
    if not take_over:
        return redirect(f"{BASE}/{doc.id}", "Gespeichert.")
    warning = ablage_ordner.nach_festschreiben(db)
    paid = " und als bezahlt gebucht" if doc.payments else ""
    rest = rechnungs_import.pending_count(db)
    message = f"Rechnung {doc.number} übernommen{paid} – sie zählt jetzt in den Auswertungen." + (
        f" {warning}" if warning else "")
    if rest:
        return redirect(BASE, message + f" Noch {rest} zu prüfen.")
    return redirect(f"/rechnungen/{doc.id}", message)


@router.post("/{doc_id}/verwerfen")
def discard(request: Request, doc_id: int, ctx: TenantContext = Depends(out_edit), db: Session = Depends(get_db)):
    doc = _doc(db, doc_id)
    belege.delete_draft(db, doc, user_id=ctx.user.id, ip=anmeldung.client_ip(request))
    db.commit()
    return redirect(BASE, "Hochgeladene Rechnung verworfen.")


@router.get("/{doc_id}/datei")
def original(doc_id: int, ctx: TenantContext = Depends(out_read), db: Session = Depends(get_db)):
    return _file_response(_doc(db, doc_id))

