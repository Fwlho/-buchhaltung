"""authenticator_app

Anmeldung per Authenticator-App (TOTP) als Alternative zum E-Mail-Code (app/services/totp.py).

Revision ID: 0014
Revises: 0013
Erstellt: 2026-11-11
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0014'
down_revision: str | None = '0013'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('totp_secret', sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column('totp_enabled_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('totp_last_step', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('totp_recovery', sa.JSON(), nullable=False, server_default='[]'))


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('totp_recovery')
        batch_op.drop_column('totp_last_step')
        batch_op.drop_column('totp_enabled_at')
        batch_op.drop_column('totp_secret')
