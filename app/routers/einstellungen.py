"""Einrichtungs-Assistent (/einrichtung) und Einstellungen (/einstellungen).

Beide benutzen dieselben Gruppen und Felder aus app/services/einstellungen.py.
"""
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as FormFile

from app.database import get_db
from app.deps import TenantContext, require_module
from app.forms import posted_values
from app.models import StoredFile, User
from app.services import ablage, anmeldung, audit, einstellungen, pdf_vorlagen
from app.services.einstellungen import GROUPS, GROUPS_BY_KEY, Group
from app.templating import redirect, render

router = APIRouter()

LOGO_MAX_BYTES = 5 * 1024 * 1024
WIZARD_STEPS = [g.key for g in GROUPS]

settings_read = require_module("einstellungen")
settings_edit = require_module("einstellungen", edit=True)
wizard_edit = require_module("einstellungen", edit=True, allow_unconfigured=True)


def _group(key: str) -> Group:
    group = GROUPS_BY_KEY.get(key)
    if group is None:
        raise HTTPException(404, "Diese Einstellungs-Seite gibt es nicht.")
    return group


def _form_context(db: Session, group: Group, values: dict | None = None, errors: dict | None = None) -> dict:
    stored = einstellungen.load(db)
    logo = None
    if stored.get("design.logo"):
        logo = db.get(StoredFile, stored["design.logo"])
    today = date.today()
    shown = values if values is not None else einstellungen.form_values(db, group)
    return {
        "group": group,
        "values": shown,
        "errors": errors or {},
        "secrets_present": einstellungen.secrets_present(db),
        "logo": logo,
        "examples": einstellungen.number_examples(
            {**stored, **{k: v for k, v in shown.items() if v}}, today.year, today.month
        ),
        "vorlagen": pdf_vorlagen.VORLAGEN,
        "vorlage_arten": pdf_vorlagen.ARTEN,
    }


async def _store_logo(request: Request, db: Session, ctx: TenantContext, upload) -> None:
    """Logo prüfen, ablegen und in den Einstellungen vermerken (ohne commit). Wirft ablage.UploadRejected."""
    stored = await ablage.save_upload(db, upload, tenant_id=ctx.tenant_id, kind="logo",
                                      allowed={"png", "jpg"}, user_id=ctx.user.id, max_bytes=LOGO_MAX_BYTES)
    old = einstellungen.get(db, "design.logo")
    einstellungen.set_values(db, {"design.logo": stored.id})
    audit.log(db, action="geaendert", entity="einstellungen", entity_id="design", tenant_id=ctx.tenant_id,
              user_id=ctx.user.id, changes={"design.logo": {"alt": old, "neu": stored.id}},
              ip=anmeldung.client_ip(request))


async def _save(request: Request, db: Session, ctx: TenantContext, group: Group) -> dict[str, str]:
    form = await request.form()
    errors = einstellungen.save_group(db, group, form, user_id=ctx.user.id, ip=anmeldung.client_ip(request))
    if errors:
        db.rollback()
        return errors
    if group.key == "firma":
        # Name in der Firmenliste (Admin, Firmenwechsel) aktuell halten
        ctx.tenant.name = einstellungen.get(db, "firma.name") or ctx.tenant.name
    # Auf der Design-Seite gehört die Logo-Datei zum selben Formular – „Speichern“ nimmt sie mit.
    # Ein abgelehntes Logo verhindert nicht das Speichern der übrigen Angaben.
    upload = form.get("datei")
    if isinstance(upload, FormFile) and upload.filename:
        try:
            await _store_logo(request, db, ctx, upload)
        except ablage.UploadRejected as exc:
            errors = {"logo": f"Die übrigen Angaben sind gespeichert, das Logo aber nicht: {exc}"}
    db.commit()
    return errors


# ------------------------------------------------------- Einrichtungs-Assistent
@router.get("/einrichtung")
def wizard_start(ctx: TenantContext = Depends(wizard_edit)):
    return redirect(f"/einrichtung/{WIZARD_STEPS[0]}")


@router.get("/einrichtung/abschluss")
def wizard_finish_page(request: Request, ctx: TenantContext = Depends(wizard_edit), db: Session = Depends(get_db)):
    return render(request, "einrichtung_abschluss.html", steps=GROUPS, missing=einstellungen.missing_required(db))


@router.post("/einrichtung/abschluss")
def wizard_finish(request: Request, ctx: TenantContext = Depends(wizard_edit), db: Session = Depends(get_db)):
    missing = einstellungen.missing_required(db)
    if missing:
        return render(request, "einrichtung_abschluss.html", status_code=400, steps=GROUPS, missing=missing)
    if not ctx.tenant.setup_completed:
        ctx.tenant.setup_completed = True
        audit.log(db, action="einrichtung_abgeschlossen", entity="firma", entity_id=ctx.tenant_id,
                  tenant_id=ctx.tenant_id, user_id=ctx.user.id, ip=anmeldung.client_ip(request))
        db.commit()
    return redirect("/", "Die Einrichtung ist abgeschlossen. Alle Angaben können Sie unter Einstellungen ändern.")


@router.get("/einrichtung/{key}")
def wizard_step(request: Request, key: str, ctx: TenantContext = Depends(wizard_edit), db: Session = Depends(get_db)):
    group = _group(key)
    return render(request, "einrichtung.html", steps=GROUPS, step=WIZARD_STEPS.index(key) + 1,
                  missing=einstellungen.missing_required(db), **_form_context(db, group))


@router.post("/einrichtung/{key}")
async def wizard_step_save(request: Request, key: str, ctx: TenantContext = Depends(wizard_edit),
                           db: Session = Depends(get_db)):
    group = _group(key)
    errors = await _save(request, db, ctx, group)
    if errors:
        form = await request.form()
        return render(request, "einrichtung.html", status_code=400, steps=GROUPS,
                      step=WIZARD_STEPS.index(key) + 1, missing=einstellungen.missing_required(db),
                      **_form_context(db, group, posted_values(group.fields, form), errors))
    index =WIZARD_STEPS.index(key)
    if index + 1 < len(WIZARD_STEPS):
        return redirect(f"/einrichtung/{WIZARD_STEPS[index + 1]}")
    return redirect("/einrichtung/abschluss")


# ---------------------------------------------------------------- Logo
_LOGO_RETURN = {"/einrichtung/design", "/einstellungen/design"}


@router.post("/einstellungen/logo")
async def logo_upload(
    request: Request,
    datei: UploadFile = File(...),
    zurueck: str = Form("/einstellungen/design"),
    ctx: TenantContext = Depends(wizard_edit),
    db: Session = Depends(get_db),
):
    back = zurueck if zurueck in _LOGO_RETURN else "/einstellungen/design"
    try:
        await _store_logo(request, db, ctx, datei)
    except ablage.UploadRejected as exc:
        db.rollback()
        return redirect(back, str(exc), "fehler")
    db.commit()
    return redirect(back, "Das Logo wurde gespeichert.")


@router.post("/einstellungen/logo/entfernen")
def logo_remove(
    request: Request,
    zurueck: str = Form("/einstellungen/design"),
    ctx: TenantContext = Depends(wizard_edit),
    db: Session = Depends(get_db),
):
    back = zurueck if zurueck in _LOGO_RETURN else "/einstellungen/design"
    old = einstellungen.get(db, "design.logo")
    if old:
        einstellungen.set_values(db, {"design.logo": None})
        audit.log(db, action="geaendert", entity="einstellungen", entity_id="design", tenant_id=ctx.tenant_id,
                  user_id=ctx.user.id, changes={"design.logo": {"alt": old, "neu": None}},
                  ip=anmeldung.client_ip(request))
        db.commit()
    return redirect(back, "Das Logo wurde entfernt.")


@router.get("/dateien/logo")
def logo_file(ctx: TenantContext = Depends(require_module("dashboard", allow_unconfigured=True)),
              db: Session = Depends(get_db)):
    file_id = einstellungen.get(db, "design.logo")
    stored = db.get(StoredFile, file_id) if file_id else None
    # db.get nutzt die Identitäts-Zuordnung – deshalb zusätzlich ausdrücklich die Firma prüfen
    if stored is None or stored.tenant_id != ctx.tenant_id:
        raise HTTPException(404, "Kein Logo hinterlegt.")
    return ablage.response(stored, media_type=stored.content_type)


# ---------------------------------------------------------- Einstellungen
@router.get("/einstellungen")
def settings_start(ctx: TenantContext = Depends(settings_read)):
    return redirect(f"/einstellungen/{WIZARD_STEPS[0]}")


@router.get("/einstellungen/email/diagnose")
def mail_diagnosis(request: Request, ctx: TenantContext = Depends(settings_edit), db: Session = Depends(get_db)):
    """Verbindung testen: Ordner des Servers mit Mailanzahl, Fähigkeiten, gefundene Ordner."""
    from app.services import imap

    cfg = imap.config_for(db)
    report: dict = {"configured": cfg is not None, "error": None, "folders": [], "capabilities": []}
    if cfg is not None:
        conn = None
        try:
            conn = imap.connect(cfg)
            info = imap.list_folders(conn)
            report["capabilities"] = [c for c in ("MOVE", "UIDPLUS", "SPECIAL-USE") if imap.supports(conn, c)]
            report["delimiter"] = info.delimiter
            report["folders"] = [(name, imap.count_messages(conn, name)) for name in info.names[:200]]
            report["inbox"] = imap.resolve(cfg.folder_inbox, info.names)
            report["inbox_wanted"] = cfg.folder_inbox
            report["done"] = imap.resolve(cfg.folder_done, info.names) if cfg.folder_done else None
            report["done_wanted"] = cfg.folder_done
            report["drafts"] = imap.drafts_folder(conn, cfg.folder_drafts)
        except imap.ImapError as exc:
            report["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            report["error"] = f"Unerwarteter Fehler: {exc}"
        finally:
            if conn is not None:
                imap.logout(conn)
    return render(request, "email_diagnose.html", report=report, groups=GROUPS)


@router.get("/einstellungen/design/muster")
def design_sample(art: str = "rechnung", vorlage: str = "", ctx: TenantContext = Depends(settings_read),
                  db: Session = Depends(get_db)):
    """Muster-PDF einer Vorlage mit den eigenen Firmendaten – zum Vergleichen vor dem Speichern."""
    from app.services import pdf_muster

    if art not in pdf_vorlagen.ARTEN:
        raise HTTPException(404, "Diese Belegart gibt es nicht.")
    data = pdf_muster.render(db, art, vorlage or None)
    return Response(data, media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="Muster-{pdf_vorlagen.ARTEN[art]}.pdf"',
        "X-Frame-Options": "SAMEORIGIN", "Content-Security-Policy": "frame-ancestors 'self'"})


@router.get("/einstellungen/protokoll")
def audit_page(request: Request, ctx: TenantContext = Depends(settings_read), db: Session = Depends(get_db)):
    entries = audit.entries_for_tenant(db, ctx.tenant_id)
    user_ids = {e.user_id for e in entries if e.user_id}
    names = {u.id: u.name for u in (db.get(User, uid) for uid in user_ids) if u}
    return render(request, "protokoll.html", entries=entries, names=names, groups=GROUPS)


@router.get("/einstellungen/{key}")
def settings_page(request: Request, key: str, ctx: TenantContext = Depends(settings_read),
                  db: Session = Depends(get_db)):
    group = _group(key)
    return render(request, "einstellungen.html", groups=GROUPS, can_edit=ctx.can("einstellungen", edit=True),
                  **_form_context(db, group))


@router.post("/einstellungen/{key}")
async def settings_save(request: Request, key: str, ctx: TenantContext = Depends(settings_edit),
                        db: Session = Depends(get_db)):
    group = _group(key)
    errors = await _save(request, db, ctx, group)
    if errors:
        form = await request.form()
        return render(request, "einstellungen.html", status_code=400, groups=GROUPS, can_edit=True,
                      **_form_context(db, group, posted_values(group.fields, form), errors))
    return redirect(f"/einstellungen/{key}", f"{group.title}: gespeichert.")
