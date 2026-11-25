"""Export für die Steuerberatung (Modul „export“): DATEV-Buchungsstapel + Belege als ZIP."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import TenantContext, require_module
from app.routers.auswertung import _period_context, period_from_query
from app.services import datev
from app.templating import redirect, render

router = APIRouter(prefix="/export")
export_read = require_module("export")


@router.get("")
def overview(request: Request, ctx: TenantContext = Depends(export_read), db: Session = Depends(get_db)):
    period, error = period_from_query(request, default_kind="monat")
    result = None
    if error is None:
        try:
            result = datev.collect(db, period.start, period.end)
        except datev.ExportError as exc:
            error = str(exc)
    return render(request, "export.html", result=result, error=error, config_problem=datev.config_problem(db),
                  query=str(request.query_params), action="/export", **_period_context(request, period))


@router.get("/datev.zip")
def download(request: Request, ctx: TenantContext = Depends(export_read), db: Session = Depends(get_db)):
    period, error = period_from_query(request, default_kind="monat")
    problem = error or datev.config_problem(db)
    if problem:
        return redirect(f"/export?{request.query_params}", problem, "fehler")
    try:
        result = datev.collect(db, period.start, period.end)
    except datev.ExportError as exc:
        return redirect(f"/export?{request.query_params}", str(exc), "fehler")
    data = datev.build_zip(db, result, exported_by=ctx.user.name)
    name = f"Export_Steuerberatung_{period.start.isoformat()}_{period.end.isoformat()}.zip"
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})
