"""Datenbank-Anbindung und zentrale Mandanten-Trennung.

So funktioniert die Trennung der Firmen (Mandanten):
- Jede fachliche Tabelle erbt von ``TenantMixin`` und hat damit eine Pflichtspalte
  ``tenant_id``.
- Pro Anfrage wird die aktive Firma in ``session.info["tenant_id"]`` hinterlegt
  (siehe ``app.deps.tenant_context``).
- Ein Ereignis-Haken hängt an JEDE ORM-Abfrage automatisch den Filter
  ``tenant_id = aktive Firma`` an. Ist keine Firma gesetzt, wird die Abfrage
  mit einem Fehler abgebrochen – so kann der Filter nicht vergessen werden.
- Beim Speichern wird geprüft, dass neue/geänderte Datensätze zur aktiven Firma
  gehören; neue Datensätze bekommen die tenant_id automatisch.

Genauso für den Prüfbereich: Tabellen mit ``PendingMixin`` liefern standardmäßig
nur geprüfte Datensätze (``pending = false``). Nur der Prüfbereich selbst fragt
ausdrücklich mit ``.execution_options(include_pending=True)`` ab.
"""
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Join, Table, create_engine, event, false
from sqlalchemy.engine import Engine
from sqlalchemy.sql.util import find_tables
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    declared_attr,
    mapped_column,
    sessionmaker,
    with_loader_criteria,
)

from app.config import get_settings


def utcnow() -> datetime:
    """Aktuelle Zeit in UTC ohne Zeitzonen-Angabe (einheitlich für SQLite und PostgreSQL)."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class TenantMixin(TimestampMixin):
    """Für alle fachlichen Tabellen: Pflicht-Spalte tenant_id + automatischer Filter."""

    @declared_attr
    def tenant_id(cls) -> Mapped[int]:  # noqa: N805
        return mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, index=True)


class PendingMixin:
    """Für Belege, die automatisch importiert werden können (Prüfbereich)."""

    pending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)


class TenantContextMissing(RuntimeError):
    """Abfrage auf eine Firmen-Tabelle ohne gesetzte aktive Firma."""


class TenantViolation(RuntimeError):
    """Versuch, Daten einer anderen Firma zu speichern."""


# ---------------------------------------------------------------- Ereignis-Haken
_mixin_cache: dict[tuple[type, int], frozenset[Table]] = {}


def _mixin_tables(mixin: type) -> frozenset[Table]:
    # Zwischengespeichert je Anzahl Modelle – kommen später Modelle hinzu, wird neu berechnet
    mappers = Base.registry.mappers
    key = (mixin, len(mappers))
    if key not in _mixin_cache:
        _mixin_cache[key] = frozenset(m.local_table for m in mappers if issubclass(m.class_, mixin))
    return _mixin_cache[key]


def _deannotate(table):
    """ORM-Abfragen enthalten „annotierte“ Kopien der Tabellen – für Vergleiche das Original nehmen."""
    return table._deannotate() if hasattr(table, "_deannotate") else table


def _plain_froms(statement) -> tuple[list[Table], bool]:
    """Tabellen, die eine SELECT-Abfrage direkt (ohne ORM-Klasse) anspricht, z. B. bei
    ``select(func.count()).select_from(Document)``. Zweiter Wert: True, wenn Firmen-
    Tabellen in Unterabfragen/Aliasen stecken, die hier nicht sicher gefiltert werden können."""
    tables: list[Table] = []
    hidden = False

    def walk(item) -> None:
        nonlocal hidden
        if isinstance(item, Table):
            tables.append(item)
        elif isinstance(item, Join):
            walk(item.left)
            walk(item.right)
        else:
            hidden = hidden or bool(set(find_tables(item, include_aliases=True)) & _mixin_tables(TenantMixin))

    for item in statement.get_final_froms():
        walk(item)
    return tables, hidden


@event.listens_for(Session, "do_orm_execute")
def _apply_global_filters(state) -> None:
    if not (state.is_select or state.is_update or state.is_delete):
        return
    # Nachladen einzelner Spalten eines bereits geladenen Objekts – das Objekt
    # wurde schon beim ersten Laden gefiltert.
    if state.is_select and state.is_column_load:
        return

    opts = state.execution_options
    # Tabellen, die über ORM-Klassen angesprochen werden (dort greift with_loader_criteria)
    covered = {m.local_table for m in state.all_mappers}
    plain, hidden = [], False
    if state.is_select:
        # Schnelle Vorprüfung (ohne SQL-Übersetzung): Nur wenn eine Firmen-/Prüfbereichs-Tabelle ohne
        # ORM-Klasse vorkommt, wird die genaue (teure) Analyse der FROM-Klauseln gemacht.
        present = {_deannotate(t) for t in find_tables(state.statement, include_aliases=True, check_columns=True)
                   if t is not None}
        special = _mixin_tables(TenantMixin) | _mixin_tables(PendingMixin)
        if (present & special) - covered:
            plain, hidden = _plain_froms(state.statement)

    if not opts.get("all_tenants", False):
        tenant_tables = _mixin_tables(TenantMixin)
        uncovered = [t for t in plain if t in tenant_tables and t not in covered]
        touches_tenant_table = uncovered or hidden or any(issubclass(m.class_, TenantMixin) for m in state.all_mappers)
        if touches_tenant_table:
            tenant_id = state.session.info.get("tenant_id")
            if tenant_id is None:
                raise TenantContextMissing(
                    "Abfrage auf Firmen-Daten ohne aktive Firma – bitte tenant_context/tenant_scope verwenden."
                )
            if hidden:
                raise TenantContextMissing(
                    "Firmen-Tabelle in Unterabfrage/Alias ohne ORM-Klasse – Firmenfilter nicht sicher anwendbar."
                )
            state.statement = state.statement.options(
                with_loader_criteria(
                    TenantMixin,
                    lambda cls: cls.tenant_id == tenant_id,
                    include_aliases=True,
                )
            )
            for table in uncovered:
                state.statement = state.statement.where(table.c.tenant_id == tenant_id)

    if not opts.get("include_pending", False):
        pending_tables = _mixin_tables(PendingMixin)
        for table in plain:
            if table in pending_tables and table not in covered:
                state.statement = state.statement.where(table.c.pending == false())
        if any(issubclass(m.class_, PendingMixin) for m in state.all_mappers):
            state.statement = state.statement.options(
                with_loader_criteria(PendingMixin, lambda cls: cls.pending == false(), include_aliases=True)
            )


@event.listens_for(Session, "before_flush")
def _check_tenant_on_flush(session: Session, flush_context, instances) -> None:
    tenant_id = session.info.get("tenant_id")
    for obj in session.new:
        if isinstance(obj, TenantMixin):
            if obj.tenant_id is None:
                if tenant_id is None:
                    raise TenantContextMissing("Neuer Datensatz ohne aktive Firma.")
                obj.tenant_id = tenant_id
            elif tenant_id is not None and obj.tenant_id != tenant_id:
                raise TenantViolation("Datensatz gehört zu einer anderen Firma.")
    for obj in list(session.dirty) + list(session.deleted):
        if isinstance(obj, TenantMixin) and tenant_id is not None and obj.tenant_id != tenant_id:
            raise TenantViolation("Datensatz gehört zu einer anderen Firma.")


@contextmanager
def tenant_scope(db: Session, tenant_id: int | None) -> Iterator[Session]:
    """Setzt vorübergehend die aktive Firma (z. B. für System-Aufgaben)."""
    previous = db.info.get("tenant_id")
    db.info["tenant_id"] = tenant_id
    try:
        yield db
    finally:
        db.info["tenant_id"] = previous


# ---------------------------------------------------------------- Engine/Session
_engine: Engine | None = None
_session_factory: sessionmaker | None = None


def _make_engine(url: str) -> Engine:
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    return engine


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        _engine = _make_engine(get_settings().database_url)
        _session_factory = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def SessionLocal() -> Session:  # noqa: N802 – üblicher Name
    get_engine()
    assert _session_factory is not None
    return _session_factory()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
