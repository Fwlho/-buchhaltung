"""Verschlüsselung gespeicherter Dateien (Belege, Rechnungs-PDFs, Logos, Kontoauszüge …) – je Firma eigener Schlüssel.

Entschieden 11.11.2026 (Server-Betrieb mit mehreren Firmen). Aufbau („Umschlag-Verfahren“):
- Hauptschlüssel = HKDF-SHA256 aus ENCRYPTION_KEY der .env – steht nie in der Datenbank oder in Sicherungen.
- Firmenschlüssel = 32 Zufallsbytes je Firma, mit dem Hauptschlüssel verschlüsselt in ``tenants.file_key``.
- Datei auf der Platte = MAGIC + Nonce (12 Bytes) + AES-256-GCM(Firmenschlüssel, Inhalt). Zusätzlich an den
  Ablagepfad gebunden: Eine Datei lässt sich weder unbemerkt verändern noch einer anderen Firma unterschieben.
Ohne die .env sind Dateien (und Sicherungen davon) nicht lesbar – die .env getrennt und sicher aufbewahren!
SHA-256 und Größe in ``files`` beziehen sich weiter auf den ORIGINAL-Inhalt (GoBD-Nachweis bleibt gleich).
Ältere, noch unverschlüsselte Dateien werden weiter gelesen; ``encrypt_existing`` verschlüsselt sie einmalig
(lokal beim Start in run.py, auf dem Server vor dem Programmstart: ``python -m app.services.tresor``).
"""
import base64
import logging
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import StoredFile, Tenant

log = logging.getLogger("app.tresor")

MAGIC = b"BHTRESOR1\n"
_NONCE = 12
_master_cache: dict[str, AESGCM] = {}
_key_cache: dict[str, AESGCM] = {}


class TresorError(Exception):
    """Datei lässt sich nicht entschlüsseln (falscher Schlüssel in der .env oder Datei verändert)."""


def _master() -> AESGCM:
    secret = get_settings().encryption_key
    if not secret:
        raise TresorError("ENCRYPTION_KEY fehlt in der .env – ohne ihn können keine Dateien gespeichert werden.")
    if secret not in _master_cache:
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                   info=b"buchhaltung/dateien/v1").derive(secret.encode())
        _master_cache[secret] = AESGCM(key)
    return _master_cache[secret]


def _aad_tenant(tenant_id: int) -> bytes:
    return f"firmenschluessel:{tenant_id}".encode()


def _unwrap(tenant_id: int, wrapped: str) -> AESGCM:
    cache_key = f"{get_settings().encryption_key}|{tenant_id}|{wrapped}"
    if cache_key not in _key_cache:
        raw = base64.b64decode(wrapped)
        try:
            key = _master().decrypt(raw[:_NONCE], raw[_NONCE:], _aad_tenant(tenant_id))
        except InvalidTag as exc:
            raise TresorError("Der Firmenschlüssel passt nicht zum ENCRYPTION_KEY der .env.") from exc
        _key_cache[cache_key] = AESGCM(key)
    return _key_cache[cache_key]


def _tenant_key(db: Session, tenant_id: int, *, create: bool) -> AESGCM | None:
    wrapped = db.scalar(select(Tenant.file_key).where(Tenant.id == tenant_id))
    if wrapped is None and create:
        nonce = os.urandom(_NONCE)
        candidate = base64.b64encode(nonce + _master().encrypt(nonce, AESGCM.generate_key(256), _aad_tenant(tenant_id)))
        # Nur setzen, wenn noch keiner da ist – zwei Prozesse (Programm und Hintergrund) dürfen sich nicht überschreiben
        db.execute(update(Tenant).where(Tenant.id == tenant_id, Tenant.file_key.is_(None))
                   .values(file_key=candidate.decode()).execution_options(synchronize_session=False))
        wrapped = db.scalar(select(Tenant.file_key).where(Tenant.id == tenant_id))
    return _unwrap(tenant_id, wrapped) if wrapped else None


def is_encrypted(blob: bytes) -> bool:
    return blob.startswith(MAGIC)


def encrypt(db: Session, tenant_id: int, relative_path: str, data: bytes) -> bytes:
    key = _tenant_key(db, tenant_id, create=True)
    nonce = os.urandom(_NONCE)
    return MAGIC + nonce + key.encrypt(nonce, data, relative_path.encode())


def decrypt(db: Session, tenant_id: int, relative_path: str, blob: bytes) -> bytes:
    if not is_encrypted(blob):
        return blob  # alte, noch unverschlüsselte Datei
    key = _tenant_key(db, tenant_id, create=False)
    if key is None:
        raise TresorError("Für diese Firma gibt es keinen Schlüssel – die Datei gehört nicht hierher.")
    body = blob[len(MAGIC):]
    try:
        return key.decrypt(body[:_NONCE], body[_NONCE:], relative_path.encode())
    except InvalidTag as exc:
        raise TresorError(f"Die Datei {relative_path} lässt sich nicht entschlüsseln (verändert oder falscher "
                          "Schlüssel).") from exc


def encrypt_existing(db: Session) -> int:
    """Alle noch unverschlüsselten Dateien aller Firmen verschlüsseln (atomar je Datei). Gibt die Anzahl zurück."""
    import hashlib

    files_dir = get_settings().files_dir
    done = 0
    rows = list(db.scalars(select(StoredFile).execution_options(all_tenants=True).order_by(StoredFile.id)))
    for stored in rows:
        path = files_dir / stored.path
        if not path.is_file():
            continue
        blob = path.read_bytes()
        if is_encrypted(blob):
            continue
        if hashlib.sha256(blob).hexdigest() != stored.sha256:
            log.warning("Datei %s weicht vom gespeicherten Prüfwert ab – wird trotzdem verschlüsselt", stored.path)
        encrypted = encrypt(db, stored.tenant_id, stored.path, blob)
        db.commit()  # Firmenschlüssel sichern, BEVOR die Datei ersetzt wird
        partial = path.with_name(path.name + ".teil")
        partial.write_bytes(encrypted)
        os.replace(partial, path)
        done += 1
    return done


def main() -> None:
    from app.database import SessionLocal

    logging.basicConfig(level=logging.INFO)
    with SessionLocal() as db:
        count = encrypt_existing(db)
    print(f"Dateien verschlüsselt: {count}")


if __name__ == "__main__":
    main()
