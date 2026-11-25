"""paragraph_13b

§ 13b UStG (Steuerschuldnerschaft des Leistungsempfängers): Fall und Steuersatz je Beleg,
Vorauswahl je Kunde (Häkchen) und je Lieferant (Fall).

Revision ID: 0016
Revises: 0015
Erstellt: 2026-11-12
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0016'
down_revision: str | None = '0015'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('documents') as batch:
        batch.add_column(sa.Column('reverse_charge', sa.String(length=10), nullable=True))
        batch.add_column(sa.Column('reverse_charge_rate', sa.Numeric(precision=5, scale=2), nullable=True))
    with op.batch_alter_table('customers') as batch:
        batch.add_column(sa.Column('reverse_charge', sa.Boolean(), server_default=sa.false(), nullable=False))
    with op.batch_alter_table('suppliers') as batch:
        batch.add_column(sa.Column('reverse_charge', sa.String(length=10), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('suppliers') as batch:
        batch.drop_column('reverse_charge')
    with op.batch_alter_table('customers') as batch:
        batch.drop_column('reverse_charge')
    with op.batch_alter_table('documents') as batch:
        batch.drop_column('reverse_charge_rate')
        batch.drop_column('reverse_charge')
