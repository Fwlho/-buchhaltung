"""kontierung

Herkunft und Begründung eines automatisch vorgeschlagenen Kontos bei Eingangsrechnungen
(Lieferant, aus eigenen Buchungen gelernt oder KI-Vorschlag).

Revision ID: 0011
Revises: 0010
Erstellt: 2026-11-02
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0011'
down_revision: str | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('account_source', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('account_hint', sa.String(length=300), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_column('account_hint')
        batch_op.drop_column('account_source')
