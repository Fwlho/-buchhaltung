"""Alembic-Umgebung: nimmt die Datenbank-Adresse aus .env (DATABASE_URL)."""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, event

import app.models  # noqa: F401 – registriert alle Tabellen
from app.config import get_settings
from app.database import Base, _make_engine

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    url = _url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def _migration_engine(url: str):
    """Für SQLite: Fremdschlüssel-Prüfung während der Migration AUS.

    SQLite kann Tabellen oft nur ändern, indem Alembic sie neu aufbaut (kopieren, alte Tabelle löschen,
    umbenennen). Mit eingeschalteter Prüfung würde das Löschen der alten Tabelle abhängige Zeilen per
    ON DELETE CASCADE mitlöschen (z. B. alle Rechnungspositionen) bzw. die Migration abbrechen.
    Nach der Migration wird die Stimmigkeit ausdrücklich geprüft (siehe unten)."""
    if not url.startswith("sqlite"):
        return _make_engine(url)
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def _foreign_keys_off(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.close()

    return engine


def run_migrations_online() -> None:
    url = _url()
    sqlite = url.startswith("sqlite")
    engine = _migration_engine(url)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=sqlite,  # SQLite kann ALTER TABLE nur eingeschränkt
        )
        with context.begin_transaction():
            context.run_migrations()
            if sqlite:
                broken = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                if broken:
                    raise RuntimeError(f"Migration hat fehlerhafte Verknüpfungen hinterlassen: {broken[:10]}")
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
