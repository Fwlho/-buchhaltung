"""Eingangsrechnungen und Prüfbereich.

- /eingangsrechnungen/...  geprüfte Belege (Modul „rechnungen_eingang“)
- /pruefbereich/...        neue, noch ungeprüfte Belege (Modul „pruefbereich“)
Beide nutzen dieselbe Detailseite; welcher Bereich gilt, entscheidet ``doc.pending``.
"""
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import TenantContext, require_module
from app.formatting import format_date, format_decimal
from app.models import AuditLog, Document, Supplier, User
from app.services import ablage, ablage_ordner, anmeldung, belege, einstellungen, ki, ki_auslese, konten, kontierung, ocr, steuerschuld
from app.services import stammdaten as stamm
from app.templating import redirect, render

router = APIRouter()

in_read = require_module("rechnungen_eingang")
in_edit = require_module("rechnungen_eingang", edit=True)
review_read = require_module("pruefbereich")
review_edit = require_module("pruefbereich", edit=True)

AREA_IN = "/eingangsrechnungen"
UPLOAD_KI_NOW = 3   # so viele Belege liest die KI direkt beim Hochladen, den Rest im Hintergrund
AREA_REVIEW = "/pruefbereich"


def _doc(db: Session, doc_id: int, *, pending: bool) -> Document:
    doc = belege.get(db, doc_id)
    if doc is None or doc.pending != pending or doc.direction != "eingang":
        raise HTTPException(404, "Beleg nicht gefunden.")
    return doc


def _ip(request: Request) -> str:
    return anmeldung.client_ip(request)


def _require_extra(ctx: TenantContext, right: str) -> None:
    if not ctx.has_extra(right):
        raise HTTPException(403, "Dafür fehlt Ihnen das Sonderrecht „Festschreiben“.")


# ----------------------------------------------------------- Detailseite
def _values_from_doc(doc: Document) -> tuple[dict, list[dict]]:
    v = {
        "number": doc.number or "", "doc_date": format_date(doc.doc_date), "due_date": format_date(doc.due_date),
        "partner_name": doc.partner_name or "", "supplier_id": str(doc.supplier_id or ""),
        "account": doc.account or "", "notes": doc.notes or "",
        "skonto_percent": format_decimal(doc.skonto_percent, 2).rstrip("0").rstrip(",") if doc.skonto_percent else "",
        "skonto_until": format_date(doc.skonto_until),
        "reverse_charge": doc.reverse_charge or "",
        "reverse_charge_rate": f"{doc.reverse_charge_rate.normalize():f}" if doc.reverse_charge_rate else "19",
    }
    rows = [{"satz": r["rate"], "netto": format_decimal(r["net"]) if r["net"] is not None else "",
             "ust": format_decimal(r["vat"]) if r["vat"] is not None else "",
             "brutto": format_decimal(r["gross"]) if r["gross"] is not None else ""}
            for r in belege.line_rows(doc)]
    return v, rows


def _values_from_form(form) -> tuple[dict, list[dict]]:
    keys = ("number", "doc_date", "due_date", "partner_name", "supplier_id", "account", "notes", "skonto_percent",
            "skonto_until", "reverse_charge", "reverse_charge_rate")
    v = {k: form.get(k) or "" for k in keys}
    rows = []
    for i in range(belege.MAX_LINES):
        if f"zeile-{i}-satz" not in form:
            break
        rows.append({k: form.get(f"zeile-{i}-{k}") or "" for k in ("satz", "netto", "ust", "brutto")})
    return v, rows


def _history(db: Session, ctx: TenantContext, doc: Document) -> list[tuple[AuditLog, str]]:
    stmt = (select(AuditLog).where(AuditLog.tenant_id == ctx.tenant_id, AuditLog.entity == "eingangsrechnung",
                                   AuditLog.entity_id == str(doc.id)).order_by(AuditLog.id.desc()).limit(50))
    entries = list(db.scalars(stmt))
    names = {u.id: u.name for u in (db.get(User, e.user_id) for e in entries if e.user_id) if u}
    return [(e, names.get(e.user_id, "–")) for e in entries]


def _detail(request: Request, db: Session, ctx: TenantContext, doc: Document, *, form=None, errors=None,
            status_code: int = 200, payment_errors=None):
    area = AREA_REVIEW if doc.pending else AREA_IN
    module = "pruefbereich" if doc.pending else "rechnungen_eingang"
    v, rows = _values_from_form(form) if form is not None else _values_from_doc(doc)
    chart = einstellungen.get(db, "konten.rahmen") or "SKR03"
    suppliers = list(db.scalars(select(Supplier).where(Supplier.is_active.is_(True)).order_by(Supplier.name)))
    einvoice = einvoice_error = None
    if doc.file is not None and doc.file.content_type == "application/xml":
        from app.services import erechnung

        try:  # XRechnung als lesbare Sichtansicht (ein Browser zeigt XML nur als Quelltext)
            einvoice = erechnung.parse(ablage.read_bytes(doc.file, db))
        except (erechnung.EInvoiceError, OSError) as exc:
            einvoice_error = str(exc)
    return render(
        request, "beleg.html", status_code=status_code, doc=doc, area=area, v=v, rows=rows,
        errors=errors or {}, payment_errors=payment_errors or {},
        can_edit=ctx.can(module, edit=True), can_finalize=ctx.has_extra("festschreiben"),
        can_create_supplier=ctx.can("kunden", edit=True),
        suppliers=suppliers, accounts=konten.accounts_for(chart), chart=chart,
        account_label=konten.account_label(chart, doc.account),
        ki_available=ki.enabled(db) and kontierung.needs_ki(doc, force=True),
        **_ki_context(db, ctx, doc),
        duplicates=belege.duplicates_of(db, doc),
        missing_final=belege.missing_for_finalize(doc), skonto_offer=belege.skonto_offer(doc),
        rc_tax=steuerschuld.incoming_tax(doc), small_business=bool(einstellungen.get(db, "steuer.kleinunternehmer")),
        vat_rates=belege.VAT_RATES, history=_history(db, ctx, doc), today=format_date(date.today()),
        einvoice=einvoice, einvoice_error=einvoice_error,
        **(_steps([d.id for d in belege.pending_documents(db)], doc.id, AREA_REVIEW) if doc.pending else {}),
    )


def _ki_context(db: Session, ctx: TenantContext, doc: Document) -> dict:
    """KI-Auslese: letzter Vorschlag mit Prüfergebnissen, Vergleich mit dem aktuellen/übernommenen Stand."""
    reading = ki_auslese.latest(db, doc.id) if doc.direction == "eingang" else None
    return {"ki_reading": reading, "ki_compare": ki_auslese.comparison(reading, doc) if reading and reading.proposal else [],
            "ki_states": doc.field_states or {}, "ki_state_labels": ki_auslese.STATE_LABELS,
            "ki_can_read": ki.enabled(db) and ki_auslese.can_read(doc) and ctx.can("pruefbereich", edit=True)}


def _steps(ids: list[int], current: int, base: str) -> dict:
    """Pfeile „vorherige/nächste“ in einem Prüfbereich (gleiche Reihenfolge wie die Liste)."""
    if current not in ids or len(ids) < 2:
        return {}
    i = ids.index(current)
    return {"step_prev": f"{base}/{ids[i - 1]}" if i > 0 else None,
            "step_next": f"{base}/{ids[i + 1]}" if i + 1 < len(ids) else None,
            "step_pos": i + 1, "step_total": len(ids)}


# ===================================================================== Upload
@router.get("/eingangsrechnungen/hochladen")
def upload_form(request: Request, ctx: TenantContext = Depends(in_edit)):
    return render(request, "beleg_upload.html", ocr_status=ocr.status_text())


@router.post("/eingangsrechnungen/hochladen")
async def upload(request: Request, dateien: list[UploadFile] = File(default=[]),
                 ctx: TenantContext = Depends(in_edit), db: Session = Depends(get_db)):
    created: list[Document] = []
    problems: list[str] = []
    for upload_file in dateien:
        if not upload_file.filename:
            continue
        try:
            doc = await belege.create_from_upload(db, upload_file, tenant_id=ctx.tenant_id, user_id=ctx.user.id,
                                                  ip=_ip(request))
            db.commit()
            created.append(doc)
        except belege.DuplicateUpload as exc:
            db.rollback()
            where = "im Prüfbereich" if exc.document.pending else "bei den Eingangsrechnungen"
            problems.append(f"„{upload_file.filename}“ wurde bereits hochgeladen (liegt {where}, "
                            f"{exc.document.partner_name or 'ohne Absender'}).")
        except ablage.UploadRejected as exc:
            db.rollback()
            problems.append(f"„{upload_file.filename}“: {exc}")
    if not created and not problems:
        return render(request, "beleg_upload.html", ocr_status=ocr.status_text(), status_code=400, error="Bitte mindestens eine Datei auswählen.")
    if not created:
        return render(request, "beleg_upload.html", ocr_status=ocr.status_text(), status_code=400, error=" ".join(problems))
    ids = [doc.id for doc in created]
    # KI-Auslese (nur wenn eingeschaltet): die ersten Belege sofort, weitere übernimmt der Hintergrund-Abruf
    ki_auslese.nachtragen(db, ids[:UPLOAD_KI_NOW], user_id=ctx.user.id, ip=_ip(request))
    kontierung.ki_nachtragen(db, ids, user_id=ctx.user.id, ip=_ip(request))  # nur wenn eingeschaltet
    message = f"{len(created)} Beleg(e) hochgeladen und ausgelesen – bitte im Prüfbereich prüfen und übernehmen."
    dup_map = belege.duplicate_map(db)
    doubles = [i for i in ids if i in dup_map]
    if doubles:
        message += f" Achtung, doppelt: {len(doubles)} Beleg(e) – {belege.DUPLICATE_MESSAGE}"
    if problems:
        message += " Nicht übernommen: " + " ".join(problems)
    target = f"{AREA_REVIEW}/{ids[0]}" if len(ids) == 1 else AREA_REVIEW
    return redirect(target, message, "ok" if not problems and not doubles else "hinweis")


# ================================================================ Prüfbereich
@router.get("/pruefbereich")
def review_list(request: Request, ctx: TenantContext = Depends(review_read), db: Session = Depends(get_db)):
    from app.services import email_import, imap

    docs = belege.pending_documents(db)
    return render(request, "pruefbereich.html", docs=docs, dup_ids=set(belege.duplicate_map(db)),
                  ki_status=ki_auslese.status_map(db, [d.id for d in docs]),
                  can_edit=ctx.can("pruefbereich", edit=True), can_upload=ctx.can("rechnungen_eingang", edit=True),
                  mail_configured=imap.config_for(db) is not None, mail_status=email_import.last_status(db),
                  mail_log=email_import.recent_log(db, 10), can_settings=ctx.can("einstellungen", edit=True),
                  auto_fetch=bool(einstellungen.get(db, "imap.aktiv")),
                  interval=einstellungen.get(db, "imap.intervall_minuten"), **_folder_context(db, "eingang"))


def _folder_context(db: Session, kind: str) -> dict:
    from app.services import eingangsordner

    from app.config import get_settings

    return {"folder": eingangsordner.folders(db).get(kind), "folder_auto": bool(einstellungen.get(db, "ordner.aktiv")),
            "folder_status": eingangsordner.last_status(db), "folder_local": get_settings().is_local}


@router.post("/pruefbereich/email-abruf")
def review_fetch_mail(request: Request, ctx: TenantContext = Depends(review_edit), db: Session = Depends(get_db)):
    from app.services import email_import

    result = email_import.run(db, ctx.tenant_id)
    return redirect(AREA_REVIEW, result.message, "ok" if result.ok and not result.errors else
                    ("hinweis" if result.ok else "fehler"))


@router.post("/pruefbereich/ordner-einlesen")
def review_scan_folder(request: Request, ctx: TenantContext = Depends(review_edit), db: Session = Depends(get_db)):
    from app.services import eingangsordner

    result = eingangsordner.scan(db, ctx.tenant_id, kinds=("eingang",))
    if result.imported:
        ki_auslese.nachtragen(db, result.imported[:UPLOAD_KI_NOW], user_id=ctx.user.id, ip=_ip(request))
        kontierung.ki_nachtragen(db, result.imported, user_id=ctx.user.id, ip=_ip(request))
    return redirect(AREA_REVIEW, result.message, "hinweis" if result.problems or result.busy else "ok")


@router.get("/pruefbereich/{doc_id}")
def review_detail(request: Request, doc_id: int, ctx: TenantContext = Depends(review_read),
                  db: Session = Depends(get_db)):
    return _detail(request, db, ctx, _doc(db, doc_id, pending=True))


@router.post("/pruefbereich/{doc_id}")
async def review_save(request: Request, doc_id: int, ctx: TenantContext = Depends(review_edit),
                      db: Session = Depends(get_db)):
    doc = _doc(db, doc_id, pending=True)
    form = await request.form()
    errors = belege.save_form(db, doc, form, user_id=ctx.user.id, ip=_ip(request))
    if errors:
        db.rollback()
        return _detail(request, db, ctx, _doc(db, doc_id, pending=True), form=form, errors=errors, status_code=400)
    if form.get("aktion") == "uebernehmen":
        missing = belege.take_over(db, doc, user_id=ctx.user.id, ip=_ip(request))
        if missing:
            db.commit()
            return redirect(f"{AREA_REVIEW}/{doc.id}",
                            "Gespeichert, aber noch nicht übernommen. Es fehlt: " + ", ".join(missing), "fehler")
        db.commit()
        warn = _duplicate_warning(db, doc)
        if not ctx.can("rechnungen_eingang"):
            return redirect(AREA_REVIEW, "Beleg übernommen." + warn, "hinweis" if warn else "ok")
        return redirect(f"{AREA_IN}/{doc.id}", "Beleg geprüft und zu den Eingangsrechnungen übernommen." + warn,
                        "hinweis" if warn else "ok")
    db.commit()
    warn = _duplicate_warning(db, doc)
    return redirect(f"{AREA_REVIEW}/{doc.id}", "Gespeichert." + warn, "hinweis" if warn else "ok")


def _duplicate_warning(db: Session, doc: Document) -> str:
    """Zusatz für die Meldung nach dem Speichern, wenn es denselben Beleg schon gibt."""
    others = belege.duplicates_of(db, doc)
    if not others:
        return ""
    names = ", ".join(f"{d.partner_name or 'ohne Absender'} {d.number or ''}".strip() for d in others[:3])
    return f" {belege.DUPLICATE_MESSAGE} (gleich wie: {names})"


@router.post("/pruefbereich/{doc_id}/verwerfen")
def review_discard(request: Request, doc_id: int, ctx: TenantContext = Depends(review_edit),
                   db: Session = Depends(get_db)):
    doc = _doc(db, doc_id, pending=True)
    belege.delete_draft(db, doc, user_id=ctx.user.id, ip=_ip(request))
    db.commit()
    return redirect(AREA_REVIEW, "Beleg verworfen.")


@router.get("/pruefbereich/{doc_id}/datei")
def review_file(doc_id: int, ctx: TenantContext = Depends(review_read), db: Session = Depends(get_db)):
    return _file_response(_doc(db, doc_id, pending=True))


# =========================================================== Eingangsrechnungen
@router.get("/eingangsrechnungen")
def incoming_list(request: Request, ctx: TenantContext = Depends(in_read), db: Session = Depends(get_db)):
    p = request.query_params
    year = int(p["jahr"]) if p.get("jahr", "").isdigit() else None
    docs = belege.incoming(db, q=p.get("q", ""), status=p.get("status", ""), year=year, review=p.get("pruefen") == "1")
    chart = einstellungen.get(db, "konten.rahmen") or "SKR03"
    sums = {
        "gross": sum((d.gross_total or 0 for d in docs if d.status != "storniert"), 0),
        "open": sum((d.open_amount for d in docs), 0),
    }
    return render(request, "eingangsrechnungen.html", docs=docs, years=belege.years(db), q=p.get("q", ""),
                  status=p.get("status", ""), year=year, review=p.get("pruefen") == "1", sums=sums,
                  chart=chart, account_label=konten.account_label, can_edit=ctx.can("rechnungen_eingang", edit=True),
                  today_date=date.today(), dup_ids=set(belege.duplicate_map(db)),
                  can_finalize=ctx.has_extra("festschreiben"), today=format_date(date.today()),
                  list_url=AREA_IN + (f"?{request.url.query}" if request.url.query else ""),
                  pending=belege.pending_count(db) if ctx.can("pruefbereich") else 0)


@router.post("/eingangsrechnungen/auswahl")
async def bulk_action(request: Request, ctx: TenantContext = Depends(in_read), db: Session = Depends(get_db)):
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    docs = [d for d in (belege.get(db, i) for i in ids) if d is not None and not d.pending and d.direction == "eingang"]
    if not docs:
        return redirect(AREA_IN, "Bitte mindestens einen Beleg auswählen.", "fehler")
    action = form.get("aktion")
    if action == "zip":
        data = belege.zip_files(docs)
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="eingangsrechnungen.zip"'})
    if action == "drucken":
        data, skipped = belege.merged_pdf(docs)
        if skipped == len(docs):
            return redirect(AREA_IN, "Die ausgewählten Belege enthalten keine PDF-Dateien.", "fehler")
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition": 'inline; filename="drucken.pdf"'})
    if action == "duplizieren":
        if not ctx.can("rechnungen_eingang", edit=True):
            raise HTTPException(403, "Dafür fehlt Ihnen die Berechtigung.")
        for doc in docs:
            belege.duplicate(db, doc, user_id=ctx.user.id, ip=_ip(request))
        db.commit()
        return redirect(AREA_REVIEW, f"{len(docs)} Beleg(e) dupliziert – die Kopien liegen im Prüfbereich.")
    raise HTTPException(400, "Unbekannte Aktion.")


@router.get("/eingangsrechnungen/{doc_id}")
def incoming_detail(request: Request, doc_id: int, ctx: TenantContext = Depends(in_read),
                    db: Session = Depends(get_db)):
    return _detail(request, db, ctx, _doc(db, doc_id, pending=False))


@router.post("/eingangsrechnungen/{doc_id}")
async def incoming_save(request: Request, doc_id: int, ctx: TenantContext = Depends(in_edit),
                        db: Session = Depends(get_db)):
    doc = _doc(db, doc_id, pending=False)
    form = await request.form()
    errors = belege.save_form(db, doc, form, user_id=ctx.user.id, ip=_ip(request))
    if errors:
        db.rollback()
        return _detail(request, db, ctx, _doc(db, doc_id, pending=False), form=form, errors=errors, status_code=400)
    db.commit()
    warn = _duplicate_warning(db, doc)
    return redirect(f"{AREA_IN}/{doc.id}", "Gespeichert." + warn, "hinweis" if warn else "ok")


def _save_header_first(request: Request, db: Session, ctx: TenantContext, doc: Document, form):
    """Zahlung/Festschreiben bei Entwürfen: zuerst die oben eingetragenen Angaben speichern (sie kommen mit
    demselben Formular). Gibt bei Eingabefehlern die Fehlerseite zurück, sonst None."""
    if not doc.is_draft or "partner_name" not in form:
        return None
    errors = belege.save_form(db, doc, form, user_id=ctx.user.id, ip=_ip(request))
    if errors:
        db.rollback()
        return _detail(request, db, ctx, _doc(db, doc.id, pending=False), form=form, errors=errors, status_code=400)
    db.commit()
    return None


def _back(form, doc: Document) -> str:
    """Zurück zur Liste (mit Filtern) oder zum Beleg – nur Ziele unter /eingangsrechnungen."""
    target = str(form.get("zurueck") or "")
    if target.startswith(AREA_IN) and not target.startswith("//") and "\\" not in target:
        return target
    return f"{AREA_IN}/{doc.id}"


@router.post("/eingangsrechnungen/{doc_id}/bezahlt")
async def incoming_mark_paid(request: Request, doc_id: int, ctx: TenantContext = Depends(in_edit),
                             db: Session = Depends(get_db)):
    """Häkchen „Bezahlt“ in der Übersicht: offenen Betrag zum angegebenen Tag buchen (Entwurf wird festgeschrieben)."""
    doc = _doc(db, doc_id, pending=False)
    form = await request.form()
    problem = belege.mark_paid(db, doc, str(form.get("paid_on") or ""), user_id=ctx.user.id, ip=_ip(request))
    if problem:
        db.rollback()
        return redirect(_back(form, doc), f"{doc.partner_name or 'Beleg'} {doc.number or ''}: {problem}", "fehler")
    db.commit()
    warning = ablage_ordner.nach_festschreiben(db)
    return redirect(_back(form, doc), f"{doc.partner_name or 'Beleg'} {doc.number or ''} als bezahlt markiert."
                    + (f" {warning}" if warning else ""), "hinweis" if warning else "ok")


@router.post("/eingangsrechnungen/{doc_id}/unbezahlt")
async def incoming_unmark_paid(request: Request, doc_id: int, ctx: TenantContext = Depends(in_edit),
                               db: Session = Depends(get_db)):
    _require_extra(ctx, "festschreiben")
    doc = _doc(db, doc_id, pending=False)
    form = await request.form()
    problem = belege.unmark_paid(db, doc, user_id=ctx.user.id, ip=_ip(request))
    if problem:
        db.rollback()
        return redirect(_back(form, doc), problem, "fehler")
    db.commit()
    return redirect(_back(form, doc), f"Häkchen entfernt – {doc.partner_name or 'Beleg'} {doc.number or ''} ist wieder offen.")


@router.post("/eingangsrechnungen/{doc_id}/festschreiben")
async def incoming_finalize(request: Request, doc_id: int, ctx: TenantContext = Depends(in_edit),
                            db: Session = Depends(get_db)):
    _require_extra(ctx, "festschreiben")
    doc = _doc(db, doc_id, pending=False)
    failed = _save_header_first(request, db, ctx, doc, await request.form())
    if failed:
        return failed
    missing = belege.finalize(db, doc, user_id=ctx.user.id, ip=_ip(request))
    if missing:
        return redirect(f"{AREA_IN}/{doc.id}", "Festschreiben nicht möglich. Es fehlt: " + ", ".join(missing), "fehler")
    db.commit()
    warning = ablage_ordner.nach_festschreiben(db)
    return redirect(f"{AREA_IN}/{doc.id}", "Beleg festgeschrieben – er kann nicht mehr geändert werden."
                    + (f" {warning}" if warning else ""), "hinweis" if warning else "ok")


@router.post("/eingangsrechnungen/{doc_id}/zahlung")
async def incoming_payment(request: Request, doc_id: int, ctx: TenantContext = Depends(in_edit),
                           db: Session = Depends(get_db)):
    doc = _doc(db, doc_id, pending=False)
    form = await request.form()
    failed = _save_header_first(request, db, ctx, doc, form)
    if failed:
        return failed
    errors = belege.add_payment(db, doc, form, user_id=ctx.user.id, ip=_ip(request))
    if errors:
        db.rollback()
        return _detail(request, db, ctx, _doc(db, doc_id, pending=False), payment_errors=errors, status_code=400)
    db.commit()
    message = "Zahlung erfasst." + (" Der Beleg ist jetzt vollständig bezahlt." if doc.status == "bezahlt" else "")
    warning = ablage_ordner.nach_festschreiben(db)      # die erste Zahlung schreibt den Beleg fest
    return redirect(f"{AREA_IN}/{doc.id}", message + (f" {warning}" if warning else ""))


@router.post("/eingangsrechnungen/{doc_id}/zahlung/{payment_id}/loeschen")
def incoming_payment_delete(request: Request, doc_id: int, payment_id: int, ctx: TenantContext = Depends(in_edit),
                            db: Session = Depends(get_db)):
    _require_extra(ctx, "festschreiben")
    doc = _doc(db, doc_id, pending=False)
    if not belege.delete_payment(db, doc, payment_id, user_id=ctx.user.id, ip=_ip(request)):
        raise HTTPException(404, "Zahlung nicht gefunden.")
    db.commit()
    return redirect(f"{AREA_IN}/{doc.id}", "Zahlung entfernt.")


@router.post("/eingangsrechnungen/{doc_id}/stornieren")
def incoming_cancel(request: Request, doc_id: int, grund: str = Form(""), ctx: TenantContext = Depends(in_edit),
                    db: Session = Depends(get_db)):
    _require_extra(ctx, "festschreiben")
    doc = _doc(db, doc_id, pending=False)
    problem = belege.cancel(db, doc, grund, user_id=ctx.user.id, ip=_ip(request))
    if problem:
        return redirect(f"{AREA_IN}/{doc.id}", problem, "fehler")
    db.commit()
    return redirect(f"{AREA_IN}/{doc.id}", "Beleg storniert.")


@router.post("/eingangsrechnungen/{doc_id}/loeschen")
def incoming_delete(request: Request, doc_id: int, ctx: TenantContext = Depends(in_edit),
                    db: Session = Depends(get_db)):
    doc = _doc(db, doc_id, pending=False)
    problem = belege.delete_draft(db, doc, user_id=ctx.user.id, ip=_ip(request))
    if problem:
        return redirect(f"{AREA_IN}/{doc.id}", problem, "fehler")
    db.commit()
    return redirect(AREA_IN, "Entwurf gelöscht.")


@router.get("/eingangsrechnungen/{doc_id}/datei")
def incoming_file(doc_id: int, ctx: TenantContext = Depends(in_read), db: Session = Depends(get_db)):
    return _file_response(_doc(db, doc_id, pending=False))


# ------------------------------------------------------------ Gemeinsam
def _file_response(doc: Document):
    if doc.file is None:
        raise HTTPException(404, "Zu diesem Beleg gibt es keine Datei.")
    path = ablage.absolute_path(doc.file)
    if not path.exists():
        raise HTTPException(404, "Die Datei fehlt im Ablageordner.")
    # Die Vorschau wird auf der eigenen Detailseite eingebettet; fremde Dateien nie mit Skripten (ablage.serve_type)
    return ablage.response(doc.file, headers={"X-Frame-Options": "SAMEORIGIN"})


@router.post("/belege/{doc_id}/konto-vorschlagen")
def suggest_account_ki(request: Request, doc_id: int, ctx: TenantContext = Depends(in_read),
                       db: Session = Depends(get_db)):
    """Knopf „Konto per KI vorschlagen“ (auch erneut, wenn die KI vorher nichts gefunden hat)."""
    doc = belege.get(db, doc_id)
    if doc is None or doc.direction != "eingang":
        raise HTTPException(404, "Beleg nicht gefunden.")
    module = "pruefbereich" if doc.pending else "rechnungen_eingang"
    if not ctx.can(module, edit=True) or not kontierung.needs_ki(doc, force=True):
        raise HTTPException(403, "Für diesen Beleg ist kein Kontovorschlag möglich.")
    back = f"{AREA_REVIEW if doc.pending else AREA_IN}/{doc.id}"
    if not ki.enabled(db):
        return redirect(back, "KI-Vorschläge sind in den Einstellungen nicht eingeschaltet.", "fehler")
    found = kontierung.ki_nachtragen(db, [doc.id], user_id=ctx.user.id, ip=_ip(request), force=True)
    db.refresh(doc)
    if found:
        return redirect(back, "Die KI hat ein Konto vorgeschlagen – bitte prüfen.")
    return redirect(back, doc.account_hint or "Kein Vorschlag.", "hinweis")


@router.post("/belege/{doc_id}/ki-auslesen")
def read_with_ki(request: Request, doc_id: int, ctx: TenantContext = Depends(review_edit),
                 db: Session = Depends(get_db)):
    """Knopf „Mit KI auslesen“ im Prüfbereich – auch erneut; überschreibt die Felder mit dem KI-Vorschlag."""
    doc = _doc(db, doc_id, pending=True)
    back = f"{AREA_REVIEW}/{doc.id}"
    if not ki.enabled(db):
        return redirect(back, "KI-Vorschläge sind in den Einstellungen nicht eingeschaltet.", "fehler")
    if not ki_auslese.can_read(doc):
        return redirect(back, "Dieser Beleg kann nicht per KI ausgelesen werden (z. B. E-Rechnung – die Werte "
                              "sind dort schon exakt).", "hinweis")
    ki_auslese.nachtragen(db, [doc.id], user_id=ctx.user.id, ip=_ip(request), force=True)
    reading = ki_auslese.latest(db, doc.id)
    if reading is None or reading.status == "fehler":
        return redirect(back, (reading.error if reading else None) or "Die KI konnte den Beleg nicht lesen.", "fehler")
    if reading.status == "kein_beleg":
        return redirect(back, "Die KI hat in dieser Datei keinen Beleg erkannt – bitte prüfen.", "hinweis")
    return redirect(back, "Von der KI ausgelesen – grün = sicher, gelb = bitte prüfen, rot = fehlt oder "
                          "widersprüchlich. Bitte mit dem Original vergleichen und übernehmen.")


@router.post("/belege/{doc_id}/lieferant-anlegen")
def create_supplier(request: Request, doc_id: int, ctx: TenantContext = Depends(require_module("kunden", edit=True)),
                    db: Session = Depends(get_db)):
    """Aus dem erkannten Absender einen neuen Lieferanten anlegen und verknüpfen."""
    doc = belege.get(db, doc_id)
    if doc is None or doc.direction != "eingang":
        raise HTTPException(404, "Beleg nicht gefunden.")
    module = "pruefbereich" if doc.pending else "rechnungen_eingang"
    if not ctx.can(module, edit=True) or not doc.is_draft:
        raise HTTPException(403, "Dieser Beleg kann nicht geändert werden.")
    back = f"{AREA_REVIEW if doc.pending else AREA_IN}/{doc.id}"
    if not doc.partner_name:
        return redirect(back, "Bitte zuerst den Absender eintragen.", "fehler")
    supplier, errors = stamm.save(db, stamm.SUPPLIER, None, {"name": doc.partner_name, "country": "Deutschland"},
                                  user_id=ctx.user.id, ip=_ip(request))
    if errors:
        db.rollback()
        return redirect(back, "Lieferant konnte nicht angelegt werden.", "fehler")
    doc.supplier_id = supplier.id
    db.commit()
    return redirect(back, f"Lieferant {supplier.number} „{supplier.name}“ angelegt und verknüpft. "
                          "Weitere Angaben (Adresse, IBAN) unter Lieferanten ergänzen.")
