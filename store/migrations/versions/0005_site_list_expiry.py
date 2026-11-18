"""A site list carries an expiry (SPEC.md, section 7.4).

A list kept before this has none, so it is given the moment it was received:
a node holding one waits for a current list rather than trusting it for ever.

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
    with op.batch_alter_table('federation_list') as batch:
        batch.add_column(sa.Column('expires_at', store.types.UtcDateTime(),
                                   nullable=True))
    op.execute("UPDATE federation_list SET expires_at = received_at")
    with op.batch_alter_table('federation_list') as batch:
        batch.alter_column('expires_at', existing_type=store.types.UtcDateTime(),
                           nullable=False)


def downgrade():
    with op.batch_alter_table('federation_list') as batch:
        batch.drop_column('expires_at')
