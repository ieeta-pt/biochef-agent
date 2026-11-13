"""Signed messages waiting for the coordinator (SPEC.md, section 6.5).

Revision ID: 0004
Revises: 0003
"""

from alembic import op
import sqlalchemy as sa
import store.types


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('outgoing_message',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.String(), nullable=False),
    sa.Column('kind', sa.String(), nullable=False),
    sa.Column('document', sa.LargeBinary(), nullable=False),
    sa.Column('created_at', store.types.UtcDateTime(), nullable=False),
    sa.Column('sent_at', store.types.UtcDateTime(), nullable=True),
    sa.CheckConstraint("kind IN ('withheld_result')", name=op.f('ck_outgoing_message_kind')),
    sa.ForeignKeyConstraint(['run_id'], ['run_summary.run_id'], name=op.f('fk_outgoing_message_run_id_run_summary')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_outgoing_message')),
    sa.UniqueConstraint('run_id', 'kind', name='one_message_per_kind')
    )


def downgrade():
    op.drop_table('outgoing_message')
