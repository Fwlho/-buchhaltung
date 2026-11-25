"""ki_auslese

KI-gestützte Belegprüfung: Protokoll je KI-Aufruf (ki_readings) und Ampel je Feld (documents.field_states).

Revision ID: 0015
Revises: 0014
Erstellt: 2026-11-12
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0015'
down_revision: str | None = '0014'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'ki_readings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('model', sa.String(length=60), nullable=True),
        sa.Column('mode', sa.String(length=10), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('proposal', sa.JSON(), nullable=False),
        sa.Column('raw', sa.Text(), nullable=True),
        sa.Column('checks', sa.JSON(), nullable=False),
        sa.Column('applied', sa.JSON(), nullable=False),
        sa.Column('final', sa.JSON(), nullable=True),
        sa.Column('error', sa.String(length=300), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('decided_at', sa.DateTime(), nullable=True),
        sa.Column('decided_by_id', sa.Integer(), nullable=True),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['decided_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('ki_readings', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ki_readings_document_id'), ['document_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_ki_readings_tenant_id'), ['tenant_id'], unique=False)
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('field_states', sa.JSON(), nullable=False, server_default='{}'))


def downgrade() -> None:
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_column('field_states')
    with op.batch_alter_table('ki_readings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ki_readings_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_ki_readings_document_id'))
    op.drop_table('ki_readings')
