"""Einstellungen aus der Datei .env (bzw. Umgebungsvariablen).

Hier stehen nur technische Werte der Installation – nichts Firmenspezifisches.
Firmendaten liegen in der Tabelle tenant_settings.
"""
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "ja", "yes", "on")


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    secret_key: str
    encryption_key: str
    setup_token: str
    cookie_secure: bool
    data_dir: Path
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    smtp_ssl: bool
    background_jobs: bool
    impressum_url: str = ""
    datenschutz_url: str = ""

    @property
    def is_local(self) -> bool:
        return self.app_env == "lokal"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def mail_outbox_dir(self) -> Path:
        return self.data_dir / "mail_ausgang"


def _resolve_sqlite(url: str) -> str:
    """Relative SQLite-Pfade auf das Projektverzeichnis beziehen, damit der Start
    aus einem anderen Arbeitsverzeichnis nicht eine neue, leere DB anlegt."""
    prefix = "sqlite:///"
    if url.startswith(prefix) and not url.startswith(prefix + "/"):
        rest = url[len(prefix):]
        if rest and rest != ":memory:" and not Path(rest).is_absolute():
            return prefix + (BASE_DIR / rest).as_posix()
    return url


@lru_cache
def get_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env", override=False)
    env = os.environ
    return Settings(
        app_env=env.get("APP_ENV", "lokal").strip().lower(),
        database_url=_resolve_sqlite(env.get("DATABASE_URL", "sqlite:///data/app.db")),
        secret_key=env.get("SECRET_KEY", ""),
        encryption_key=env.get("ENCRYPTION_KEY", ""),
        setup_token=env.get("SETUP_TOKEN", "").strip(),
        cookie_secure=_bool(env.get("COOKIE_SECURE"), False),
        data_dir=Path(env.get("DATA_DIR") or (BASE_DIR / "data")),
        smtp_host=env.get("SMTP_HOST", "").strip(),
        smtp_port=int(env.get("SMTP_PORT") or 465),
        smtp_user=env.get("SMTP_USER", "").strip(),
        smtp_password=env.get("SMTP_PASSWORD", ""),
        smtp_from=env.get("SMTP_FROM", "").strip(),
        smtp_ssl=_bool(env.get("SMTP_SSL"), True),
        background_jobs=_bool(env.get("BACKGROUND_JOBS"), True),
        # Pflichtangaben für den Betrieb im Internet (§ 5 DDG, Art. 13 DSGVO): Links auf die Seiten des Betreibers
        impressum_url=_https(env.get("IMPRESSUM_URL")),
        datenschutz_url=_https(env.get("DATENSCHUTZ_URL")),
    )


def _https(value: str | None) -> str:
    """Nur vollständige https-Adressen übernehmen (kein javascript:, keine Tippfehler ohne Protokoll)."""
    value = (value or "").strip()
    return value if value.startswith("https://") and " " not in value else ""
