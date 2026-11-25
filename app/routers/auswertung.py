"""EÜR (Modul „euer“) und Umsatzsteuer (Modul „steuern“)."""
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import TenantContext, require_module
from app.formatting import parse_date
from app.services import auswertung, konten
from app.services.auswertung import Period
from app.templating import render

router = APIRouter()
euer_read = require_module("euer")
steuern_read = require_module("steuern")


def _int(value: str | None, default: int) -> int:
    return int(value) if value and value.isdigit() else default


def period_from_query(request: Request, default_kind: str = "jahr") -> tuple[Period, str | None]:
    """Zeitraum aus der Adresszeile: zeitraum=jahr|quartal|monat|frei, jahr, nr, von, bis."""
    p = request.query_params
    today = date.today()
    kind = p.get("zeitraum") or default_kind
    year = _int(p.get("jahr"), today.year)
    if not 2000 <= year <= today.year + 1:
        year = today.year
    if kind == "frei":
        try:
            start, end = parse_date(p.get("von")), parse_date(p.get("bis"))
        except ValueError as exc:
            return auswertung.period("jahr", year), str(exc)
        if not start or not end or end < start:
            return auswertung.period("jahr", year), "Bitte einen gültigen Zeitraum „von – bis“ angeben."
        return auswertung.period("frei", start.year, start=start, end=end), None
    default_nr = today.month if kind == "monat" else (today.month - 1) // 3 + 1
    return auswertung.period(kind, year, _int(p.get("nr"), default_nr)), None


def _period_context(request: Request, period: Period) -> dict:
    today = date.today()
    return {"period": period, "years": list(range(today.year + 1, today.year - 8, -1)),
            "months": auswertung.MONTHS, "q": request.query_params}


# =================================================================== EÜR
@router.get("/euer")
def euer_page(request: Request, ctx: TenantContext = Depends(euer_read), db: Session = Depends(get_db)):
    period, error = period_from_query(request)
    result = auswertung.euer(db, period)
    return render(request, "euer.html", result=result, error=error, vat_received_label=konten.EUER_VAT_RECEIVED,
                  vat_paid_label=konten.EUER_VAT_PAID, query=str(request.query_params),
                  **_period_context(request, period))


@router.get("/euer/export.csv")
def euer_csv(request: Request, ctx: TenantContext = Depends(euer_read), db: Session = Depends(get_db)):
    period, _ = period_from_query(request)
    data = auswertung.euer_csv(auswertung.euer(db, period))
    name = f"EUER_{period.start.isoformat()}_{period.end.isoformat()}.csv"
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/euer/export.pdf")
def euer_pdf(request: Request, ctx: TenantContext = Depends(euer_read), db: Session = Depends(get_db)):
    from app.services import pdf_euer

    period, _ = period_from_query(request)
    data = pdf_euer.render(db, auswertung.euer(db, period))
    name = f"EUER_{period.start.isoformat()}_{period.end.isoformat()}.pdf"
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"'})


# ================================================================ Steuern
@router.get("/steuern")
def steuern_page(request: Request, ctx: TenantContext = Depends(steuern_read), db: Session = Depends(get_db)):
    today = date.today()
    kind = auswertung.vat_period_kind(db)
    year = _int(request.query_params.get("jahr"), today.year)
    if not 2000 <= year <= today.year + 1:
        year = today.year
    periods = auswertung.vat_periods(kind, year)
    current = auswertung.period_containing(kind, today)
    default_nr = current.nr if current.year == year and kind != "jahr" else 1
    nr = _int(request.query_params.get("nr"), default_nr)
    whole_year = auswertung.period("jahr", year)
    # nr=0: ganzes Jahr (Übersicht aller Zeiträume, Grundlage der Umsatzsteuer-Jahreserklärung)
    selected = whole_year if nr == 0 or len(periods) == 1 else periods[min(max(nr, 1), len(periods)) - 1]
    small_business = auswertung.small_business(db)
    overview = [(p, r) for p in periods for r in [auswertung.ust(db, p)] if not small_business or r.rc_in]
    year_total = auswertung.ust(db, whole_year) if len(periods) > 1 else None
    return render(
        request, "steuern.html", year=year, years=list(range(today.year + 1, today.year - 8, -1)), kind=kind,
        selected=selected, result=auswertung.ust(db, selected), overview=overview, small_business=small_business,
        ku=auswertung.ku_monitor(db, year) if small_business else None,
        whole_euro_kz=auswertung.WHOLE_EURO_KZ,
        year_total=year_total, whole_year=whole_year,
    )
