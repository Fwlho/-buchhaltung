"""Mahnwesen (Modul „mahnwesen“): überfällige Rechnungen, Mahnungen als PDF und E-Mail-Entwurf."""
import re
from dataclasses import replace

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import TenantContext, require_module
from app.models import Dunning
from app.services import ablage, anmeldung, mahnwesen, rechnungen
from app.templating import redirect, render

router = APIRouter(prefix="/mahnwesen")
dun_read = require_module("mahnwesen")
dun_edit = require_module("mahnwesen", edit=True)
BASE = "/mahnwesen"


def _dunning(db: Session, dunning_id: int) -> Dunning:
    dunning = mahnwesen.get(db, dunning_id)
    if dunning is None:
        raise HTTPException(404, "Mahnung nicht gefunden.")
    return dunning


@router.get("")
def overview(request: Request, ctx: TenantContext = Depends(dun_read), db: Session = Depends(get_db)):
    recent = list(db.scalars(select(Dunning).order_by(Dunning.id.desc()).limit(30)))
    return render(request, "mahnwesen.html", candidates=mahnwesen.overdue(db), recent=recent,
                  can_edit=ctx.can("mahnwesen", edit=True))


@router.post("/rechnung/{doc_id}")
def create(request: Request, doc_id: int, ctx: TenantContext = Depends(dun_edit), db: Session = Depends(get_db)):
    doc = rechnungen.get(db, doc_id)
    if doc is None:
        raise HTTPException(404, "Rechnung nicht gefunden.")
    dunning, problem = mahnwesen.create(db, doc, user_id=ctx.user.id, ip=anmeldung.client_ip(request))
    if problem:
        db.rollback()
        return redirect(BASE, problem, "fehler")
    db.commit()
    return redirect(f"{BASE}/{dunning.id}", f"{dunning.level_name} {dunning.number} erstellt – bitte das PDF "
                                            "prüfen und versenden.")


def _mail_context(db: Session, dunning: Dunning, mail=None, mail_errors=None) -> dict:
    from app.services import email_entwurf, imap

    doc = dunning.document
    if mail is None:
        values = mahnwesen.placeholders(db, doc, _calc_of(db, dunning))
        mail = email_entwurf.default_draft(db, "mahnung", values, doc.customer.email if doc.customer else None)
    return {"mail": mail, "mail_errors": mail_errors or {}, "mail_action": f"{BASE}/{dunning.id}/email",
            "mail_configured": imap.config_for(db) is not None, "draft_at": dunning.email_draft_at}


def _calc_of(db: Session, dunning: Dunning) -> mahnwesen.Calculation:
    """Gespeicherte Werte einer Mahnung (für Platzhalter) – nicht neu berechnen."""
    level = mahnwesen.levels(db)[dunning.level - 1]
    return mahnwesen.Calculation(replace(level, name=dunning.level_name), dunning.dunning_date, dunning.pay_until,
                                 dunning.open_amount, dunning.fees, dunning.interest, 0, None)


@router.get("/{dunning_id}")
def detail(request: Request, dunning_id: int, ctx: TenantContext = Depends(dun_read), db: Session = Depends(get_db)):
    dunning = _dunning(db, dunning_id)
    return _page(request, db, ctx, dunning)


def _page(request, db, ctx, dunning, mail=None, mail_errors=None, status_code: int = 200):
    return render(request, "mahnung.html", status_code=status_code, dunning=dunning, doc=dunning.document,
                  others=mahnwesen.dunnings_of(db, dunning.document), can_edit=ctx.can("mahnwesen", edit=True),
                  **_mail_context(db, dunning, mail, mail_errors))


@router.get("/{dunning_id}/pdf")
def pdf(dunning_id: int, ctx: TenantContext = Depends(dun_read), db: Session = Depends(get_db)):
    dunning = _dunning(db, dunning_id)
    if dunning.file is None or not ablage.absolute_path(dunning.file).exists():
        raise HTTPException(404, "Die PDF-Datei fehlt im Ablageordner.")
    return ablage.response(dunning.file, headers={"X-Frame-Options": "SAMEORIGIN"})


@router.post("/{dunning_id}/email")
async def email_draft(request: Request, dunning_id: int, ctx: TenantContext = Depends(dun_edit),
                      db: Session = Depends(get_db)):
    """E-Mail-Entwurf im Postfach anlegen, im Mailprogramm öffnen oder als .eml – nie automatisch versenden."""
    from app.services import email_entwurf, imap, mailprogramm

    dunning = _dunning(db, dunning_id)
    form = await request.form()
    draft = email_entwurf.Draft(to=(form.get("to") or "").strip(), subject=(form.get("subject") or "").strip(),
                                body=form.get("body") or "")
    errors = email_entwurf.check(draft)
    if form.get("aktion") == "entwurf" and not draft.to:
        errors["to"] = "Bitte eine Empfänger-Adresse angeben."
    if errors:
        return _page(request, db, ctx, dunning, mail=draft, mail_errors=errors, status_code=400)
    attachment = ablage.read_bytes(dunning.file, db)
    msg = email_entwurf.build(db, draft, attachment, dunning.file.original_name)
    if form.get("aktion") == "eml":
        name = re.sub(r"[^A-Za-z0-9._-]", "_", dunning.file.original_name.rsplit(".", 1)[0]) + ".eml"
        return Response(bytes(msg), media_type="message/rfc822",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    program = mailprogramm.chosen(form)
    try:
        folder = mailprogramm.open_compose(program, msg) if program else email_entwurf.append_to_drafts(db, msg)
    except (imap.ImapError, mailprogramm.MailProgramError) as exc:
        return _page(request, db, ctx, dunning, mail=draft, mail_errors={"_": str(exc)}, status_code=400)
    email_entwurf.mark_created(db, dunning, entity="mahnung", user_id=ctx.user.id, ip=anmeldung.client_ip(request),
                               folder=folder, to=draft.to)
    db.commit()
    if program:
        return redirect(f"{BASE}/{dunning.id}", mailprogramm.opened_message(folder))
    return redirect(f"{BASE}/{dunning.id}", f"Entwurf im Ordner „{folder}“ angelegt – bitte im Mailprogramm prüfen "
                                            "und selbst senden.")
