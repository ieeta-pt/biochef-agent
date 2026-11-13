"""Which TES API listener holds the database, so a second one refuses to start (SPEC.md, section 14.4).

Revision ID: 0007
Revises: 0006
"""

from alembic import op
import sqlalchemy as sa
import store.types


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('tes_listener',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('holder', sa.String(), nullable=False),
    sa.Column('host', sa.String(), nullable=False),
    sa.Column('pid', sa.Integer(), nullable=False),
    sa.Column('started_at', store.types.UtcDateTime(), nullable=False),
    sa.Column('heartbeat_at', store.types.UtcDateTime(), nullable=False),
    sa.CheckConstraint('id = 1', name=op.f('ck_tes_listener_single_row')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tes_listener'))
    )


def downgrade():
    op.drop_table('tes_listener')
