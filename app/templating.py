"""Jinja2-Vorlagen, deutsche Filter, Hinweis-Meldungen (Flash) und CSRF-Feld."""
from urllib.parse import quote, unquote, urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from app.config import BASE_DIR
from app.formatting import (
    format_date,
    format_datetime,
    format_decimal,
    format_money,
    format_percent,
    format_price,
)

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
env = templates.env
env.filters["geld"] = format_money
env.filters["zahl"] = format_decimal
env.filters["prozent"] = format_percent
env.filters["preis"] = format_price
env.filters["datum"] = format_date
env.filters["datumzeit"] = format_datetime


def money_text(value) -> str:
    """Betrag aus Text (z. B. KI-Protokoll „12.34“) deutsch anzeigen; Unlesbares unverändert."""
    from decimal import Decimal, InvalidOperation

    try:
        return format_money(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return str(value or "")


env.filters["geld_text"] = money_text

_STATIC_DIR = BASE_DIR / "app" / "static"


def static_version(name: str = "style.css") -> int:
    """Änderungsstand einer statischen Datei – als ?v=… an die Adresse gehängt, damit der Browser nach einem
    Update die neue Datei lädt statt einer alten aus seinem Zwischenspeicher."""
    try:
        return int((_STATIC_DIR / name).stat().st_mtime)
    except OSError:
        return 0


env.globals["static_version"] = static_version


def legal_links() -> dict[str, str]:
    """Impressum/Datenschutz des Betreibers (IMPRESSUM_URL/DATENSCHUTZ_URL in der .env) für Seitenfuß und Anmeldung."""
    from app.config import get_settings

    s = get_settings()
    return {k: v for k, v in (("Impressum", s.impressum_url), ("Datenschutz", s.datenschutz_url)) if v}


env.globals["legal_links"] = legal_links


def mail_programs_available() -> dict[str, str]:
    """Installierte Mailprogramme für die Knöpfe „In … öffnen“ (nur lokal, siehe services/mailprogramm.py)."""
    from app.services import mailprogramm

    return mailprogramm.available()


env.globals["mail_programs_available"] = mail_programs_available

# § 13b-Fälle für Formulare und Anzeigen (services/steuerschuld.py)
from app.services import steuerschuld as _steuerschuld  # noqa: E402

env.globals["rc_kinds_in"] = _steuerschuld.KINDS_IN
env.globals["rc_kinds_out"] = _steuerschuld.KINDS_OUT
env.globals["rc_rates"] = _steuerschuld.RATES

FLASH_COOKIE = "flash"
CSRF_COOKIE = "csrftoken"
CSRF_FIELD = "csrf_token"


def csrf_input(request: Request) -> Markup:
    token = getattr(request.state, "csrf_token", "")
    return Markup(f'<input type="hidden" name="{CSRF_FIELD}" value="{escape(token)}">')


def _refresh(request: Request) -> tuple[int, str]:
    """Automatisches Neuladen, sobald die Anmeldung (30 Min.) bzw. die Admin-Freigabe (5 Min.) ohne Aktivität
    abläuft – dann steht die Anmelde- bzw. Admin-Passwort-Seite da statt der Daten (ohne JavaScript)."""
    from app.services import admin_freigabe, anmeldung

    admin = bool(getattr(request.state, "admin_area", False))
    seconds = int((admin_freigabe.IDLE if admin else anmeldung.SESSION_IDLE).total_seconds()) + 5
    target = "/"
    if request.method == "GET":
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    params = {"weiter": target, **({"bereich": "admin"} if admin else {})}
    return seconds, "/abgelaufen?" + urlencode(params)


def render(request: Request, template: str, status_code: int = 200, **context):
    """Seite rendern; ergänzt Anmelde-Kontext, CSRF und Hinweis-Meldung."""
    flash = None
    raw = request.cookies.get(FLASH_COOKIE)
    if raw:
        kind, _, text = unquote(raw).partition("|")
        flash = {"kind": kind, "text": text}
    context.setdefault("ctx", getattr(request.state, "ctx", None))
    context.setdefault("nav_user", getattr(request.state, "user", None))
    if context["nav_user"] is not None:
        context.setdefault("nav_refresh", _refresh(request))
    response = templates.TemplateResponse(
        request, template, {"flash": flash, "csrf_input": csrf_input(request), **context}, status_code=status_code
    )
    if raw:
        response.delete_cookie(FLASH_COOKIE, path="/")
    return response


def redirect(url: str, message: str | None = None, kind: str = "ok") -> RedirectResponse:
    """Weiterleitung nach dem Speichern (303), optional mit Hinweis-Meldung."""
    response = RedirectResponse(url, status_code=303)
    if message:
        response.set_cookie(FLASH_COOKIE, quote(f"{kind}|{message}"), max_age=60, httponly=True,
                            samesite="lax", path="/")
    return response
