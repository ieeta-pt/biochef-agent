"""A repeat is the same request_id, not the same payload (SPEC.md, section 6.3).

A request delivered again is counted on its own row, so the `joined` verdict, the
column naming the request joined and the index for finding one by payload go.

Revision ID: 0003
Revises: 0002
"""

from alembic import op
import sqlalchemy as sa
import store.types


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    # A joined row was a second request_id carrying a payload already seen. Under
    # this rule it is a request of its own, which was never checked as one.
    op.execute("DELETE FROM request WHERE verdict = 'joined'")
    with op.batch_alter_table('request') as batch:
        batch.drop_index('request_payload')
        batch.drop_constraint(op.f('ck_request_join_names_a_request'), type_='check')
        batch.drop_constraint(op.f('fk_request_joined_request_id_request'),
                              type_='foreignkey')
        batch.drop_column('joined_request_id')
        batch.drop_constraint(op.f('ck_request_verdict'), type_='check')
        batch.create_check_constraint(op.f('ck_request_verdict'),
                                      "verdict IN ('accepted', 'refused')")
        batch.add_column(sa.Column('repeats', sa.Integer(), server_default='0',
                                   nullable=False))
        batch.add_column(sa.Column('last_repeat_at', store.types.UtcDateTime(),
                                   nullable=True))


def downgrade():
    with op.batch_alter_table('request') as batch:
        batch.drop_column('last_repeat_at')
        batch.drop_column('repeats')
        batch.drop_constraint(op.f('ck_request_verdict'), type_='check')
        batch.create_check_constraint(op.f('ck_request_verdict'),
                                      "verdict IN ('accepted', 'refused', 'joined')")
        batch.add_column(sa.Column('joined_request_id', sa.String(), nullable=True))
        batch.create_foreign_key(op.f('fk_request_joined_request_id_request'), 'request',
                                 ['joined_request_id'], ['request_id'])
        batch.create_check_constraint(
            op.f('ck_request_join_names_a_request'),
            "(COALESCE(verdict, '') = 'joined') = (joined_request_id IS NOT NULL)")
        batch.create_index('request_payload', ['payload_digest'], unique=False)
