"""auftraege

Aufträge (Projektübersicht): Tabellen orders, order_lines, order_tasks und documents.order_id.
Bestehende Benutzer und offene Einladungen bekommen für den neuen Bereich „auftraege“ dieselbe
Rechtestufe wie für „angebote“ (sonst sähe zunächst nur die Inhaberin den neuen Menüpunkt).

Revision ID: 0017
Revises: 0016
Erstellt: 2026-11-17
"""
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0017'
down_revision: str | None = '0016'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [sa.Column('tenant_id', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False)]


def upgrade() -> None:
    op.create_table('orders',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('number', sa.String(length=40), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('address_id', sa.Integer(), nullable=True),
    sa.Column('object_address', sa.Text(), nullable=True),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('quote_id', sa.Integer(), nullable=True),
    sa.Column('contract_id', sa.Integer(), nullable=True),
    sa.Column('created_by_id', sa.Integer(), nullable=True),
    *_timestamps(),
    sa.ForeignKeyConstraint(['address_id'], ['customer_addresses.id'], ),
    sa.ForeignKeyConstraint(['contract_id'], ['recurring_contracts.id'], ),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ),
    sa.ForeignKeyConstraint(['quote_id'], ['quotes.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'number', name='uq_orders_number')
    )
    with op.batch_alter_table('orders', schema=None) as batch_op:
        for col in ('address_id', 'contract_id', 'customer_id', 'quote_id', 'tenant_id'):
            batch_op.create_index(batch_op.f(f'ix_orders_{col}'), [col], unique=False)

    for table in ('order_lines', 'order_tasks'):
        extra = ([sa.Column('title', sa.String(length=300), nullable=False),
                  sa.Column('description', sa.Text(), nullable=True),
                  sa.Column('quantity', sa.Numeric(precision=12, scale=4), nullable=False),
                  sa.Column('unit', sa.String(length=20), nullable=True),
                  sa.Column('unit_price', sa.Numeric(precision=12, scale=4), nullable=False),
                  sa.Column('vat_rate', sa.Numeric(precision=5, scale=2), nullable=False)]
                 if table == 'order_lines' else
                 [sa.Column('text', sa.String(length=300), nullable=False),
                  sa.Column('done', sa.Boolean(), nullable=False)])
        op.create_table(table,
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        *extra,
        *_timestamps(),
        sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id')
        )
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.create_index(batch_op.f(f'ix_{table}_order_id'), ['order_id'], unique=False)
            batch_op.create_index(batch_op.f(f'ix_{table}_tenant_id'), ['tenant_id'], unique=False)

    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('order_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_documents_order_id'), ['order_id'], unique=False)
        batch_op.create_foreign_key('fk_documents_order_id', 'orders', ['order_id'], ['id'])

    # Rechte: „auftraege“ wie „angebote“
    bind = op.get_bind()
    for table_name in ('memberships', 'invitations'):
        table = sa.table(table_name, sa.column('id', sa.Integer), sa.column('permissions', sa.JSON))
        for row_id, perms in bind.execute(sa.select(table.c.id, table.c.permissions)).all():
            if isinstance(perms, str):
                perms = json.loads(perms or "{}")
            perms = dict(perms or {})
            if "auftraege" in perms:
                continue
            perms["auftraege"] = perms.get("angebote", "keine")
            bind.execute(table.update().where(table.c.id == row_id).values(permissions=perms))


def downgrade() -> None:
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_constraint('fk_documents_order_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_documents_order_id'))
        batch_op.drop_column('order_id')
    op.drop_table('order_tasks')
    op.drop_table('order_lines')
    op.drop_table('orders')
