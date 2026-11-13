"""The TES API's tasks, kept across a restart (SPEC.md, section 14.4).

Revision ID: 0006
Revises: 0005
"""

from alembic import op
import sqlalchemy as sa
import store.types


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('tes_task',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('owner', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('tags', sa.JSON(), nullable=False),
    sa.Column('image', sa.String(), nullable=False),
    sa.Column('command', sa.JSON(), nullable=False),
    sa.Column('inputs', sa.JSON(), nullable=False),
    sa.Column('outputs', sa.JSON(), nullable=False),
    sa.Column('state', sa.String(), nullable=False),
    sa.Column('created_at', store.types.UtcDateTime(), nullable=False),
    sa.Column('started_at', store.types.UtcDateTime(), nullable=True),
    sa.Column('ended_at', store.types.UtcDateTime(), nullable=True),
    sa.Column('exit_code', sa.Integer(), nullable=True),
    sa.Column('stdout', sa.Text(), nullable=False),
    sa.Column('stderr', sa.Text(), nullable=False),
    sa.Column('system_logs', sa.JSON(), nullable=False),
    sa.Column('sizes', sa.JSON(), nullable=False),
    sa.CheckConstraint("state IN ('QUEUED', 'INITIALIZING', 'RUNNING', 'CANCELING', 'COMPLETE', 'EXECUTOR_ERROR', 'SYSTEM_ERROR', 'CANCELED')", name=op.f('ck_tes_task_state')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tes_task'))
    )


def downgrade():
    op.drop_table('tes_task')
