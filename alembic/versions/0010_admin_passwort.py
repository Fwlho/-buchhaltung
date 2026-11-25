"""admin_passwort

Zweites Passwort für Admin-Bereiche (Einstellungen, Benutzer, Datensicherung, Firmenverwaltung)
und Zeitpunkt, bis zu dem die Admin-Freigabe einer Sitzung gilt.

Revision ID: 0010
Revises: 0009
Erstellt: 2026-10-29
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('admin_password_hash', sa.String(length=255), nullable=True))
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('admin_until', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.drop_column('admin_until')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('admin_password_hash')
