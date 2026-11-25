"""firmenschluessel

Eigener Schlüssel je Firma für die verschlüsselte Datei-Ablage (app/services/tresor.py).

Revision ID: 0013
Revises: 0012
Erstellt: 2026-11-11
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0013'
down_revision: str | None = '0012'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.add_column(sa.Column('file_key', sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.drop_column('file_key')
