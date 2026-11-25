"""ablage_ordner

Festgeschriebene Ein- und Ausgangsrechnungen werden in <Ablage>\\<Jahr>\\<MM Monat> einsortiert
(app/services/ablage_ordner.py). ``documents.ablage_at``/``ablage_pfad`` merken, ob und wohin ein Beleg kopiert wurde.

- Die bisherige Einstellung „rechnung.ablage_ordner“ (Design, mit Kunden-Unterordner) wird durch
  „ordner.ablage_ausgang“ ersetzt; ein eingetragener Pfad wird übernommen.
- Schon festgeschriebene Belege gelten als erledigt – einsortiert wird nur, was ab jetzt festgeschrieben wird.

Revision ID: 0019
Revises: 0018
Erstellt: 2026-11-25
"""
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op


revision: str = '0019'
down_revision: str | None = '0018'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('documents') as batch:
        batch.add_column(sa.Column('ablage_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('ablage_pfad', sa.String(length=500), nullable=True))

    bind = op.get_bind()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    documents = sa.table('documents', sa.column('state', sa.String), sa.column('ablage_at', sa.DateTime))
    bind.execute(documents.update().where(documents.c.state == 'festgeschrieben').values(ablage_at=now))

    settings = sa.table('tenant_settings', sa.column('id', sa.Integer), sa.column('tenant_id', sa.Integer),
                        sa.column('key', sa.String), sa.column('value', sa.JSON),
                        sa.column('created_at', sa.DateTime), sa.column('updated_at', sa.DateTime))
    old_rows = bind.execute(sa.select(settings.c.id, settings.c.tenant_id, settings.c.value)
                            .where(settings.c.key == 'rechnung.ablage_ordner')).all()
    for row_id, tenant_id, value in old_rows:
        exists = bind.execute(sa.select(settings.c.id).where(settings.c.tenant_id == tenant_id,
                                                             settings.c.key == 'ordner.ablage_ausgang')).first()
        if value and not exists:
            bind.execute(settings.insert().values(tenant_id=tenant_id, key='ordner.ablage_ausgang', value=value,
                                                  created_at=now, updated_at=now))
        bind.execute(settings.delete().where(settings.c.id == row_id))


def downgrade() -> None:
    with op.batch_alter_table('documents') as batch:
        batch.drop_column('ablage_pfad')
        batch.drop_column('ablage_at')
