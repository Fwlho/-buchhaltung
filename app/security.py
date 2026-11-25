"""Passwörter, Tokens, Verschlüsselung von Zugangsdaten."""
import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def password_problem(password: str, repeat: str | None = None) -> str | None:
    """Gibt eine Fehlermeldung zurück, wenn das Passwort nicht taugt, sonst None."""
    if len(password or "") < MIN_PASSWORD_LENGTH:
        return f"Das Passwort muss mindestens {MIN_PASSWORD_LENGTH} Zeichen lang sein."
    if repeat is not None and password != repeat:
        return "Die beiden Passwörter stimmen nicht überein."
    return None


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def code_hash(code: str) -> str:
    """HMAC statt einfacher Prüfsumme: ein 6-stelliger Code ließe sich sonst aus
    einer gestohlenen Datenbank sofort zurückrechnen."""
    key = get_settings().secret_key.encode()
    return hmac.new(key, code.encode(), hashlib.sha256).hexdigest()


def constant_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# --------------------------------------------------------- Verschlüsselung
def _fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        raise RuntimeError("ENCRYPTION_KEY fehlt in der .env – bitte run.py einmal starten.")
    return Fernet(key.encode())


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError(
            "Gespeichertes Passwort lässt sich nicht entschlüsseln (ENCRYPTION_KEY geändert?)."
        ) from exc
