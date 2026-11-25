"""Firmen anlegen und Mitglieder hinzufügen."""
from sqlalchemy.orm import Session

from app.database import tenant_scope
from app.models import Membership, Tenant, User
from app.permissions import role_template
from app.services import einstellungen


def create_tenant(db: Session, name: str) -> Tenant:
    tenant = Tenant(name=name.strip())
    db.add(tenant)
    db.flush()
    with tenant_scope(db, tenant.id):
        einstellungen.set_values(db, {"firma.name": tenant.name})
        db.flush()
    return tenant


def add_membership(
    db: Session, user: User, tenant_id: int, role: str,
    permissions: dict | None = None, extra_rights: list[str] | None = None,
) -> Membership:
    """Legt die Zugehörigkeit an oder aktiviert/aktualisiert eine bestehende."""
    default_perms, default_extras = role_template(role)
    membership = db.query(Membership).filter_by(user_id=user.id, tenant_id=tenant_id).one_or_none()
    if membership is None:
        membership = Membership(user_id=user.id, tenant_id=tenant_id)
        db.add(membership)
    membership.role = role
    membership.permissions = permissions if permissions is not None else default_perms
    membership.extra_rights = extra_rights if extra_rights is not None else default_extras
    membership.is_active = True
    return membership
