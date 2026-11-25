"""Bankabgleich (Modul „bank“): Kontoauszug importieren, Umsätze Rechnungen zuordnen."""
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import TenantContext, require_module
from app.formatting import parse_decimal
from app.models import BankImport, BankTransaction
from app.models.bank import CATEGORIES
from app.services import anmeldung, bank, belege, kontoauszug
from app.services.kontoauszug import StatementError
from app.templating import redirect, render

router = APIRouter(prefix="/bank")
bank_read = require_module("bank")
bank_edit = require_module("bank", edit=True)
BASE = "/bank"


def _ip(request: Request) -> str:
    return anmeldung.client_ip(request)


def _tx(db: Session, tx_id: int) -> BankTransaction:
    tx = db.get(BankTransaction, tx_id)
    if tx is None:
        raise HTTPException(404, "Umsatz nicht gefunden.")
    return tx


def _import(db: Session, import_id: int) -> BankImport:
    imp = db.get(BankImport, import_id)
    if imp is None:
        raise HTTPException(404, "Kontoauszug nicht gefunden.")
    return imp


# ===================================================================== Liste
@router.get("")
def overview(request: Request, ctx: TenantContext = Depends(bank_read), db: Session = Depends(get_db)):
    show = request.query_params.get("zeigen", "offen")
    q = request.query_params.get("q", "")
    txs = bank.transactions(db, show=show if show in ("offen", "erledigt", "alle") else "offen", q=q)
    docs_in, docs_out = bank.open_documents(db, True), bank.open_documents(db, False)
    best = {tx.id: bank.best(db, tx, docs_in if tx.amount > 0 else docs_out) for tx in txs if tx.remaining > 0}
    imports = list(db.scalars(select(BankImport).order_by(BankImport.id.desc()).limit(10)))
    return render(request, "bank.html", txs=txs, best=best, imports=imports, show=show, q=q,
                  open_count=bank.open_count(db), sure_count=sum(1 for s in best.values() if s and s.sure),
                  can_edit=ctx.can("bank", edit=True))


@router.post("/import")
async def upload(request: Request, datei: UploadFile = File(...), ctx: TenantContext = Depends(bank_edit),
                 db: Session = Depends(get_db)):
    data = await datei.read(bank.MAX_BYTES + 1)
    try:
        imp = bank.upload(db, data, datei.filename or "", tenant_id=ctx.tenant_id, user_id=ctx.user.id,
                          ip=_ip(request))
    except StatementError as exc:
        db.rollback()
        return redirect(BASE, str(exc), "fehler")
    db.commit()
    if imp.status == "zuordnung":
        return redirect(f"{BASE}/import/{imp.id}")
    return redirect(BASE, _import_message(imp))


def _import_message(imp: BankImport) -> str:
    text = f"{imp.count_new} Umsätze eingelesen."
    if imp.count_duplicate:
        text += f" {imp.count_duplicate} waren schon vorhanden und wurden übersprungen."
    return text


@router.get("/import/{import_id}")
def mapping_page(request: Request, import_id: int, ctx: TenantContext = Depends(bank_edit),
                 db: Session = Depends(get_db)):
    imp = _import(db, import_id)
    if imp.status != "zuordnung":
        return redirect(BASE)
    return render(request, "bank_zuordnung.html", imp=imp, fields=kontoauszug.CSV_FIELDS,
                  **bank.csv_preview(db, imp))


@router.post("/import/{import_id}")
async def mapping_save(request: Request, import_id: int, ctx: TenantContext = Depends(bank_edit),
                       db: Session = Depends(get_db)):
    imp = _import(db, import_id)
    form = await request.form()
    mapping = {key: str(form.get(key) or "") for key, _ in kontoauszug.CSV_FIELDS}
    problem = bank.apply_mapping(db, imp, mapping, user_id=ctx.user.id, ip=_ip(request))
    if problem:
        db.rollback()
        preview = bank.csv_preview(db, imp)
        preview["mapping"] = mapping
        return render(request, "bank_zuordnung.html", status_code=400, imp=imp, fields=kontoauszug.CSV_FIELDS,
                      error=problem, **preview)
    db.commit()
    return redirect(BASE, _import_message(imp) + " Die Spalten-Zuordnung ist für diese Bank gespeichert.")


@router.post("/import/{import_id}/loeschen")
def import_delete(request: Request, import_id: int, ctx: TenantContext = Depends(bank_edit),
                  db: Session = Depends(get_db)):
    problem = bank.delete_import(db, _import(db, import_id), user_id=ctx.user.id, ip=_ip(request))
    if problem:
        db.rollback()
        return redirect(BASE, problem, "fehler")
    db.commit()
    return redirect(BASE, "Kontoauszug und seine Umsätze wurden entfernt.")


@router.post("/vorschlaege")
def accept_sure(request: Request, ctx: TenantContext = Depends(bank_edit), db: Session = Depends(get_db)):
    count = bank.assign_sure(db, user_id=ctx.user.id, ip=_ip(request))
    db.commit()
    return redirect(BASE, f"{count} Zahlungen zugeordnet." if count else "Es gab keine sicheren Vorschläge.")


# ================================================================== Umsatz
@router.get("/umsatz/{tx_id}")
def detail(request: Request, tx_id: int, ctx: TenantContext = Depends(bank_read), db: Session = Depends(get_db)):
    return _detail(request, db, ctx, _tx(db, tx_id))


def _detail(request: Request, db: Session, ctx: TenantContext, tx: BankTransaction, error: str | None = None,
            status_code: int = 200):
    docs = bank.open_documents(db, tx.amount > 0) if tx.remaining > 0 else []
    payments = [(p, belege.get(db, p.document_id)) for p in tx.payments]
    return render(request, "bank_umsatz.html", status_code=status_code, tx=tx, error=error, docs=docs,
                  suggestions=bank.suggestions(db, tx, docs), payments=payments, categories=CATEGORIES,
                  can_edit=ctx.can("bank", edit=True), can_reset_payments=ctx.has_extra("festschreiben"))


@router.post("/umsatz/{tx_id}/zuordnen")
def assign(request: Request, tx_id: int, beleg: str = Form(""), betrag: str = Form(""), mit_skonto: str = Form(""),
           ctx: TenantContext = Depends(bank_edit), db: Session = Depends(get_db)):
    tx = _tx(db, tx_id)
    doc = belege.get(db, int(beleg)) if beleg.isdigit() else None
    if doc is None or doc.pending:
        return _detail(request, db, ctx, tx, "Bitte eine Rechnung auswählen.", 400)
    try:
        amount = parse_decimal(betrag)
    except ValueError as exc:
        return _detail(request, db, ctx, tx, str(exc), 400)
    problem = bank.assign(db, tx, doc, amount=amount, skonto=bool(mit_skonto), user_id=ctx.user.id, ip=_ip(request))
    if problem:
        db.rollback()
        return _detail(request, db, ctx, _tx(db, tx_id), problem, 400)
    db.commit()
    message = f"Zahlung zu {doc.number or 'Beleg'} erfasst." + (" Rechnung ist bezahlt." if doc.status == "bezahlt" else "")
    if tx.remaining > 0:
        return redirect(f"{BASE}/umsatz/{tx.id}", message + " Vom Umsatz ist noch ein Betrag frei.")
    return redirect(BASE, message)


@router.post("/umsatz/{tx_id}/markieren")
def mark(request: Request, tx_id: int, kategorie: str = Form(""), ctx: TenantContext = Depends(bank_edit),
         db: Session = Depends(get_db)):
    tx = _tx(db, tx_id)
    problem = bank.set_category(db, tx, kategorie, user_id=ctx.user.id, ip=_ip(request))
    if problem:
        db.rollback()
        return _detail(request, db, ctx, tx, problem, 400)
    db.commit()
    return redirect(BASE, f"Umsatz als „{tx.category_label}“ markiert.")


@router.post("/umsatz/{tx_id}/aufheben")
def reset(request: Request, tx_id: int, ctx: TenantContext = Depends(bank_edit), db: Session = Depends(get_db)):
    tx = _tx(db, tx_id)
    if tx.payments and not ctx.has_extra("festschreiben"):
        raise HTTPException(403, "Zahlungen entfernen erfordert das Sonderrecht „Festschreiben“.")
    bank.reset(db, tx, user_id=ctx.user.id, ip=_ip(request))
    db.commit()
    return redirect(f"{BASE}/umsatz/{tx.id}", "Zuordnung aufgehoben – der Umsatz ist wieder offen.")
