"""The value a visa carries for a dataset (docs/agent-ui/SPEC.md, section 6.6).

Revision ID: 0002
Revises: 0001
"""

from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('dataset_binding') as batch:
        batch.add_column(sa.Column('visa_value', sa.String(), nullable=True))


def downgrade():
    with op.batch_alter_table('dataset_binding') as batch:
        batch.drop_column('visa_value')
