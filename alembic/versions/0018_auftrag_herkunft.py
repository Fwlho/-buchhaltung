"""auftrag_herkunft

Herkunft eines Auftrags (``orders.source``): leer = von Hand / aus Angebot, „rechnung“ = automatisch aus einer
Ausgangsrechnung, „vertrag“ = automatisch aus einem Vertrag, „angebot“ = automatisch aus einem angenommenen Angebot.

Revision ID: 0018
Revises: 0017
Erstellt: 2026-11-17
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0018'
down_revision: str | None = '0017'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('orders') as batch:
        batch.add_column(sa.Column('source', sa.String(length=20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('orders') as batch:
        batch.drop_column('source')
