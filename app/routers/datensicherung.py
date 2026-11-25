"""Datensicherung (lokal) und Firmen-Export – erreichbar über Einstellungen."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import TenantContext, require_module
from app.permissions import ROLE_OWNER
from app.services import anmeldung, audit, einstellungen, firmen_export, sicherung
from app.templating import redirect, render

router = APIRouter(prefix="/datensicherung")
settings_read = require_module("einstellungen")
settings_edit = require_module("einstellungen", edit=True)


@router.get("")
def overview(request: Request, ctx: TenantContext = Depends(settings_read)):
    local = sicherung.sqlite_path() is not None
    return render(request, "datensicherung.html", groups=einstellungen.GROUPS, local=local,
                  backups=sicherung.backups() if local and ctx.user.is_platform_admin else [],
                  backup_dir=sicherung.backup_dir(), keep=sicherung.KEEP, is_owner=ctx.role == ROLE_OWNER,
                  is_admin=ctx.user.is_platform_admin)


@router.post("/jetzt")
def backup_now(ctx: TenantContext = Depends(settings_edit)):
    # Die Sicherung enthält die Daten ALLER Firmen – deshalb nur für den Plattform-Admin
    if not ctx.user.is_platform_admin:
        raise HTTPException(403, "Nur für den Plattform-Admin.")
    path = sicherung.create_local_backup(force=True)
    if path is None:
        return redirect("/datensicherung", "Auf dem Server erfolgt die Sicherung über deploy/sicherung.sh.", "hinweis")
    return redirect("/datensicherung", f"Sicherung „{path.name}“ angelegt.")


@router.post("/firmen-export")
def company_export(request: Request, ctx: TenantContext = Depends(settings_edit), db: Session = Depends(get_db)):
    if ctx.role != ROLE_OWNER:
        raise HTTPException(403, "Den vollständigen Datenexport darf nur der Inhaber bzw. die Inhaberin erstellen.")
    data = firmen_export.build(db, ctx.tenant_id)
    audit.log(db, action="firmen_export", entity="firma", entity_id=ctx.tenant_id, tenant_id=ctx.tenant_id,
              user_id=ctx.user.id, ip=anmeldung.client_ip(request), changes={"groesse": len(data)})
    db.commit()
    name = f"Firmenexport_{date.today().isoformat()}.zip"
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})
