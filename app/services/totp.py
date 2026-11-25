"""Anmeldung per Authenticator-App (TOTP nach RFC 6238 – Google/Microsoft Authenticator, 2FAS, Aegis …).

Entschieden 11.11.2026: als Alternative zum E-Mail-Code. Wer die App eingerichtet hat, meldet sich NUR noch mit
App-Code (oder einem der 8 Notfall-Codes) an – ein gehacktes E-Mail-Postfach reicht dann nicht mehr.
- Geheimnis: 20 Zufallsbytes, verschlüsselt gespeichert (``users.totp_secret``, Fernet aus der .env).
- Jeder Code gilt nur einmal (``users.totp_last_step``), erlaubt ist ±30 Sekunden Uhrabweichung.
- Notfall-Codes: nur als SHA-256-Prüfwert gespeichert, je einmal verwendbar.
- Fehlversuche: 5 → 15 Minuten Sperre (``login_throttle``, Schlüssel ``totp:<Benutzer>``).
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import timedelta
from urllib.parse import quote

from sqlalchemy.orm import Session

from app.database import utcnow
from app.models import LoginThrottle, User
from app.security import constant_equal, decrypt, encrypt

PERIOD = 30
DIGITS = 6
WINDOW = 1                 # ±1 Zeitschritt (= ±30 Sekunden Uhrabweichung)
RECOVERY_COUNT = 8
MAX_FAILURES = 5
LOCK = timedelta(minutes=15)


# ------------------------------------------------------------ reine Funktionen
def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    return base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10 ** DIGITS).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // PERIOD)


def match(secret: str, code: str, *, last_step: int | None = None, now: float | None = None) -> int | None:
    """Zeitschritt des passenden Codes oder None. Schon benutzte Schritte (≤ last_step) zählen nicht (kein Wiederholen)."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != DIGITS:
        return None
    step = current_step(now)
    for candidate in range(step - WINDOW, step + WINDOW + 1):
        if last_step is not None and candidate <= last_step:
            continue
        if constant_equal(code_at(secret, candidate), code):
            return candidate
    return None


def uri(secret: str, account: str, issuer: str) -> str:
    """otpauth://-Adresse für den QR-Code."""
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={PERIOD}"


def qr_svg(text: str) -> str:
    """QR-Code als SVG (reportlab ist ohnehin vorhanden – kein Zusatzpaket, kein externer Dienst)."""
    from reportlab.graphics import renderSVG
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing

    widget = QrCodeWidget(text, barLevel="M")
    widget.barWidth = widget.barHeight = 220
    drawing = Drawing(220, 220)
    drawing.add(widget)
    return renderSVG.drawToString(drawing)


def _hash(code: str) -> str:
    return hashlib.sha256(code.replace("-", "").replace(" ", "").lower().encode()).hexdigest()


def new_recovery_codes() -> list[str]:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"   # ohne leicht verwechselbare Zeichen (0/o, 1/l/i)
    return ["".join(secrets.choice(alphabet) for _ in range(5)) + "-" + "".join(secrets.choice(alphabet) for _ in range(5))
            for _ in range(RECOVERY_COUNT)]


# ------------------------------------------------------------ mit Datenbank
def enabled(user: User) -> bool:
    return user.totp_enabled_at is not None and bool(user.totp_secret)


def secret_of(user: User) -> str | None:
    return decrypt(user.totp_secret) if user.totp_secret else None


def start_setup(user: User) -> str:
    """Neues (noch unbestätigtes) Geheimnis anlegen – erst ``confirm_setup`` schaltet die App-Anmeldung ein."""
    secret = new_secret()
    user.totp_secret = encrypt(secret)
    user.totp_enabled_at = None
    user.totp_last_step = None
    user.totp_recovery = []
    return secret


def confirm_setup(user: User, code: str) -> list[str] | None:
    """Mit dem ersten Code aus der App bestätigen. Gibt die Notfall-Codes (nur jetzt sichtbar) oder None zurück."""
    secret = secret_of(user)
    step = match(secret, code) if secret else None
    if step is None:
        return None
    codes = new_recovery_codes()
    user.totp_enabled_at = utcnow()
    user.totp_last_step = step
    user.totp_recovery = [_hash(c) for c in codes]
    return codes


def disable(user: User) -> None:
    user.totp_secret = None
    user.totp_enabled_at = None
    user.totp_last_step = None
    user.totp_recovery = []


def _throttle_key(user: User) -> str:
    return f"totp:{user.id}"


def lock_remaining(db: Session, user: User) -> int:
    row = db.get(LoginThrottle, _throttle_key(user))
    now = utcnow()
    if row and row.locked_until and row.locked_until > now:
        return int((row.locked_until - now).total_seconds()) + 1
    return 0


def check(db: Session, user: User, code: str) -> tuple[bool, str]:
    """App-Code oder Notfall-Code prüfen (für die Anmeldung). Gibt (ok, Meldung) zurück."""
    if lock_remaining(db, user):
        return False, "Zu viele Fehlversuche. Bitte in 15 Minuten erneut versuchen."
    secret = secret_of(user)
    step = match(secret, code, last_step=user.totp_last_step) if secret else None
    if step is not None:
        user.totp_last_step = step
        _clear(db, user)
        return True, ""
    hashed = _hash(code or "")
    remaining = [h for h in (user.totp_recovery or []) if not constant_equal(h, hashed)]
    if len(remaining) < len(user.totp_recovery or []):
        user.totp_recovery = remaining
        _clear(db, user)
        return True, f"Notfall-Code verwendet – noch {len(remaining)} übrig."
    row = db.get(LoginThrottle, _throttle_key(user)) or LoginThrottle(key=_throttle_key(user), failures=0)
    db.add(row)
    row.failures += 1
    if row.failures >= MAX_FAILURES:
        row.locked_until = utcnow() + LOCK
        row.failures = 0
        return False, "Zu viele Fehlversuche. Bitte in 15 Minuten erneut versuchen."
    return False, "Der Code ist falsch oder schon verwendet."


def _clear(db: Session, user: User) -> None:
    row = db.get(LoginThrottle, _throttle_key(user))
    if row:
        db.delete(row)
