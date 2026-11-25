"""email_status

Status je abgerufener Mail im E-Mail-Protokoll: „importiert“ (Beleg angelegt bzw. schon vorhanden),
„ohne_anhang“ (keine PDF/XML-Datei, bleibt im Postfach) oder „abgelehnt“ (Anhang unbrauchbar, bleibt im
Postfach und wird im Prüfbereich gemeldet). So muss der Abruf bekannte Mails nicht jedes Mal neu laden.

Revision ID: 0012
Revises: 0011
Erstellt: 2026-11-04
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0012'
down_revision: str | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('email_import_log', schema=None) as batch_op:
        batch_op.add_column(sa.Column('status', sa.String(length=20), nullable=False, server_default='importiert'))


def downgrade() -> None:
    with op.batch_alter_table('email_import_log', schema=None) as batch_op:
        batch_op.drop_column('status')
