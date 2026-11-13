"""The runner's network check at start, for the Node screen (SPEC.md, section 14.1).

Revision ID: 0005
Revises: 0004
"""

from alembic import op
import sqlalchemy as sa
import store.types


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('node_status') as batch:
        batch.add_column(sa.Column('network_checked_at', store.types.UtcDateTime(),
                                   nullable=True))
        batch.add_column(sa.Column('network_check_ok', sa.Boolean(), nullable=True))
        batch.add_column(sa.Column('network_check_detail', sa.String(), nullable=True))


def downgrade():
    with op.batch_alter_table('node_status') as batch:
        batch.drop_column('network_check_detail')
        batch.drop_column('network_check_ok')
        batch.drop_column('network_checked_at')
