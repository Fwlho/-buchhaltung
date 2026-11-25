"""Pflicht-Test: Ein Benutzer von Firma A sieht/ändert unter keinen Umständen Daten von Firma B."""
import pytest
from sqlalchemy import Boolean, Integer, create_engine, func, inspect, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.database import Base, PendingMixin, TenantContextMissing, TenantMixin, TenantViolation, tenant_scope
from app.models import Document, Membership, StoredFile, Tenant, TenantSetting, User
from app.services import einstellungen
from tests.conftest import (
    PASSWORD,
    admin_freigabe,
    complete_wizard,
    ersteinrichtung,
    link_from,
    new_client,
    post,
)
from tests.test_einrichtung import PNG


def _two_tenants(client, outbox, db):
    """Firma A (Chefin, per Ersteinrichtung) und Firma B (Bodo, per Admin-Einladung)."""
    ersteinrichtung(client)
    complete_wizard(client)
    post(client, "/einstellungen/logo", {"zurueck": "/einstellungen/design"},
         files={"datei": ("a.png", PNG, "image/png")})

    r = post(client, "/admin/firmen", {"firma": "Firma B", "inhaber_name": "Bodo", "inhaber_email": "bodo@example.org"})
    assert "Einladungs-Link" in r.text
    client_b = new_client()
    r = client_b.get(link_from(outbox, "bodo@example.org"))
    assert "Einladung zu Firma B" in r.text
    r = post(client_b, link_from(outbox, "bodo@example.org"),
             {"name": "Bodo", "password": PASSWORD, "password2": PASSWORD})
    assert "Einrichtung Ihrer Firma" in r.text
    complete_wizard(client_b, {"firma": {"firma.name": "Bodo GmbH", "firma.rechtsform": "GmbH"}})
    admin_freigabe(client_b)

    tenant_a = db.scalar(select(Tenant).where(Tenant.name == "Muster Service"))
    tenant_b = db.scalar(select(Tenant).where(Tenant.name == "Bodo GmbH"))
    return client_b, tenant_a, tenant_b


def test_firmen_sehen_nur_eigene_daten(client, outbox, db):
    client_b, tenant_a, tenant_b = _two_tenants(client, outbox, db)

    assert "Muster Service" in client.get("/einstellungen/firma").text
    page_b = client_b.get("/einstellungen/firma").text
    assert "Bodo GmbH" in page_b
    assert "Muster Service" not in page_b
    assert "DE89" in client.get("/einstellungen/bank").text

    # Firma B hat kein Logo – das Logo von A ist für B nicht abrufbar
    assert client.get("/dateien/logo").status_code == 200
    assert client_b.get("/dateien/logo").status_code == 404

    # B kann keine Firma-A-Benutzer bearbeiten (Mitgliedschafts-ID von A)
    m_a = db.scalar(select(Membership).where(Membership.tenant_id == tenant_a.id))
    assert client_b.get(f"/benutzer/{m_a.id}").status_code == 404
    r = post(client_b, f"/benutzer/{m_a.id}", {"role": "mitarbeiter", "is_active": ""})
    assert r.status_code == 404
    db.refresh(m_a)
    assert m_a.is_active and m_a.role == "inhaber"

    # B sieht keine Firmenverwaltung und kann nicht zu Firma A wechseln
    assert client_b.get("/admin").status_code == 403
    r = post(client_b, "/firma-wechseln", {"tenant_id": str(tenant_a.id)})
    assert r.status_code == 403


def test_orm_filter_greift_automatisch(client, outbox, db):
    _, tenant_a, tenant_b = _two_tenants(client, outbox, db)

    with tenant_scope(db, tenant_a.id):
        keys_a = {s.tenant_id for s in db.scalars(select(TenantSetting))}
        assert keys_a == {tenant_a.id}
        assert einstellungen.get(db, "firma.name") == "Muster Service"
        logo_a = db.scalar(select(StoredFile))
        assert logo_a is not None
    db.expunge_all()
    with tenant_scope(db, tenant_b.id):
        assert einstellungen.get(db, "firma.name") == "Bodo GmbH"
        assert db.scalar(select(StoredFile)) is None
        assert db.get(StoredFile, logo_a.id) is None  # auch per ID nicht erreichbar


def test_abfrage_ohne_firma_wird_abgebrochen(db):
    with pytest.raises(TenantContextMissing):
        db.scalars(select(TenantSetting)).all()
    with pytest.raises(TenantContextMissing):
        db.scalar(select(func.count()).select_from(TenantSetting))
    with pytest.raises(TenantContextMissing):
        db.execute(select(TenantSetting.__table__)).all()


def test_zaehlen_und_tabellen_abfragen_werden_gefiltert(client, outbox, db):
    """Abfragen ohne ORM-Klasse (z. B. ``select(count()).select_from(...)``) müssen genauso
    gefiltert werden – hier gab es eine Lücke, die mit diesem Test abgesichert ist."""
    _, tenant_a, tenant_b = _two_tenants(client, outbox, db)
    with tenant_scope(db, tenant_a.id):
        db.add(Document(direction="eingang", pending=True))
        db.add(Document(direction="eingang", pending=False))
        db.flush()
        own_settings = db.scalar(select(func.count()).select_from(TenantSetting))
        assert own_settings > 0
        assert db.scalar(select(func.count()).select_from(Document)) == 1  # ohne Prüfbereich
        assert db.scalar(select(func.count()).select_from(Document).execution_options(include_pending=True)) == 2
    with tenant_scope(db, tenant_b.id):
        assert db.scalar(select(func.count()).select_from(Document).execution_options(include_pending=True)) == 0
        rows = db.execute(select(TenantSetting.__table__)).all()
        assert rows and {r.tenant_id for r in rows} == {tenant_b.id}
        joined = select(func.count()).select_from(
            TenantSetting.__table__.join(Tenant.__table__, Tenant.__table__.c.id == TenantSetting.__table__.c.tenant_id))
        assert db.scalar(joined) == len(rows)
        with pytest.raises(TenantContextMissing):
            sub = select(TenantSetting.__table__).subquery()
            db.scalar(select(func.count()).select_from(sub))
    db.rollback()


def test_speichern_fuer_fremde_firma_wird_abgebrochen(client, outbox, db):
    _, tenant_a, tenant_b = _two_tenants(client, outbox, db)
    with tenant_scope(db, tenant_a.id):
        db.add(TenantSetting(tenant_id=tenant_b.id, key="firma.name", value="Übernommen!"))
        with pytest.raises(TenantViolation):
            db.flush()
        db.rollback()
    with tenant_scope(db, tenant_a.id):
        db.add(TenantSetting(key="test.neu", value=1))  # tenant_id wird automatisch gesetzt
        db.flush()
        assert db.scalar(select(TenantSetting).where(TenantSetting.key == "test.neu")).tenant_id == tenant_a.id
        db.rollback()


def test_alle_firmen_tabellen_haben_tenant_id():
    """Neue fachliche Tabelle = muss TenantMixin nutzen (Pflichtspalte + Index)."""
    tenant_tables = [m.class_ for m in Base.registry.mappers if issubclass(m.class_, TenantMixin)]
    assert {c.__tablename__ for c in tenant_tables} >= {
        "tenant_settings", "files", "customers", "customer_addresses", "suppliers", "catalog_items",
    }
    for cls in tenant_tables:
        col = cls.__table__.c.tenant_id
        assert not col.nullable, cls
        assert col.index, cls
        assert [fk.column.table.name for fk in col.foreign_keys] == ["tenants"], cls


def test_plattform_tabellen_ausdruecklich_bekannt():
    """Tabellen OHNE TenantMixin müssen hier bewusst eingetragen sein – fachliche
    Tabellen gehören nicht in diese Liste."""
    platform = {"tenants", "users", "memberships", "sessions", "invitations", "login_codes", "login_throttle",
                "audit_log"}
    without_mixin = {m.class_.__tablename__ for m in Base.registry.mappers if not issubclass(m.class_, TenantMixin)}
    assert without_mixin == platform


# ------------------------------------------------------ Prüfbereich-Filter
class _TestBase(DeclarativeBase):
    pass


class _Beleg(PendingMixin, _TestBase):
    __tablename__ = "test_belege"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bezahlt: Mapped[bool] = mapped_column(Boolean, default=False)


def test_ungepruefte_belege_zaehlen_nicht_mit():
    engine = create_engine("sqlite://")
    _TestBase.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all([_Beleg(pending=False), _Beleg(pending=True), _Beleg(pending=False)])
        s.commit()
        assert len(s.scalars(select(_Beleg)).all()) == 2
        alle = s.scalars(select(_Beleg).execution_options(include_pending=True)).all()
        assert len(alle) == 3
    assert inspect(engine).has_table("test_belege")
