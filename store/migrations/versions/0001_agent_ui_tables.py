"""The tables of the agent UI (docs/agent-ui/SPEC.md, section 6.1).

Revision ID: 0001
Revises: none
"""

from alembic import op
import sqlalchemy as sa
import sqlmodel
import store.types


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('dataset_binding',
    sa.Column('dataset_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('location', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('bound_by', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('bound_at', store.types.UtcDateTime(), nullable=False),
    sa.CheckConstraint("source IN ('config', 'page')", name=op.f('ck_dataset_binding_source')),
    sa.PrimaryKeyConstraint('dataset_id', name=op.f('pk_dataset_binding'))
    )
    op.create_table('decision',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('subject_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('subject_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('subject_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('action', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('user', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('comment', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('dac_confirmed', sa.Boolean(), nullable=True),
    sa.Column('reason', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('at', store.types.UtcDateTime(), nullable=False),
    sa.Column('object_raw', sa.LargeBinary(), nullable=False),
    sa.Column('signature', sa.Text(), nullable=False),
    sa.Column('posting_started_at', store.types.UtcDateTime(), nullable=True),
    sa.Column('posted_to_coordinator_at', store.types.UtcDateTime(), nullable=True),
    sa.CheckConstraint("action IN ('approve', 'refuse', 'withdraw', 'release', 'withhold', 'bind', 'unbind', 'cancel')", name=op.f('ck_decision_action')),
    sa.CheckConstraint("subject_type IN ('study_version', 'workflow_version', 'request', 'run', 'egress', 'dataset_binding')", name=op.f('ck_decision_subject_type')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_decision'))
    )
    with op.batch_alter_table('decision', schema=None) as batch_op:
        batch_op.create_index('decision_subject', ['subject_type', 'subject_id'], unique=False)

    op.create_table('federation_list',
    sa.Column('version', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('raw', sa.LargeBinary(), nullable=False),
    sa.Column('signature', sa.Text(), nullable=False),
    sa.Column('nodes', sa.JSON(), nullable=False),
    sa.Column('received_at', store.types.UtcDateTime(), nullable=False),
    sa.PrimaryKeyConstraint('version', name=op.f('pk_federation_list'))
    )
    op.create_table('node_status',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('last_poll_at', store.types.UtcDateTime(), nullable=True),
    sa.Column('last_success_at', store.types.UtcDateTime(), nullable=True),
    sa.Column('last_error', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.CheckConstraint('id = 1', name=op.f('ck_node_status_single_row')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_node_status'))
    )
    op.create_table('request',
    sa.Column('request_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('study_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('study_version', sa.Integer(), nullable=False),
    sa.Column('study_digest', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('workflow_digest', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('member', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('node', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('datasets', sa.JSON(), nullable=False),
    sa.Column('not_after', store.types.UtcDateTime(), nullable=False),
    sa.Column('payload_digest', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('raw', sa.LargeBinary(), nullable=False),
    sa.Column('received_at', store.types.UtcDateTime(), nullable=False),
    sa.Column('verdict', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('failed_check', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('joined_request_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('run_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.CheckConstraint("failed_check IN ('bad_signature', 'wrong_node', 'study_not_approved', 'workflow_not_approved', 'expired', 'replay', 'cannot_decrypt', 'hash_mismatch', 'visa_missing', 'dataset_not_bound')", name=op.f('ck_request_failed_check')),
    sa.CheckConstraint("verdict IN ('accepted', 'refused', 'joined')", name=op.f('ck_request_verdict')),
    sa.CheckConstraint("(COALESCE(verdict, '') = 'refused') = (failed_check IS NOT NULL)", name=op.f('ck_request_refusal_names_a_check')),
    sa.CheckConstraint("(COALESCE(verdict, '') = 'joined') = (joined_request_id IS NOT NULL)", name=op.f('ck_request_join_names_a_request')),
    sa.ForeignKeyConstraint(['joined_request_id'], ['request.request_id'], name=op.f('fk_request_joined_request_id_request')),
    sa.PrimaryKeyConstraint('request_id', name=op.f('pk_request'))
    )
    with op.batch_alter_table('request', schema=None) as batch_op:
        batch_op.create_index('request_payload', ['payload_digest'], unique=False)
        batch_op.create_index('request_received', ['received_at'], unique=False)

    op.create_table('study',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('title', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('created_at', store.types.UtcDateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_study'))
    )
    op.create_table('run_summary',
    sa.Column('run_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('request_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('state', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('steps', sa.JSON(), nullable=False),
    sa.Column('started_at', store.types.UtcDateTime(), nullable=True),
    sa.Column('ended_at', store.types.UtcDateTime(), nullable=True),
    sa.Column('exit_code', sa.Integer(), nullable=True),
    sa.Column('workspace_path', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('manifest_path', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('log_stdout', sa.Text(), nullable=False),
    sa.Column('log_stderr', sa.Text(), nullable=False),
    sa.Column('node_logs', sa.JSON(), nullable=False),
    sa.Column('cancel_requested', sa.Boolean(), nullable=False),
    sa.Column('egress_state', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('egress_files', sa.JSON(), nullable=False),
    sa.Column('airlock_ticket', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('airlock_at', store.types.UtcDateTime(), nullable=True),
    sa.Column('retain_until', store.types.UtcDateTime(), nullable=True),
    sa.CheckConstraint("egress_state IN ('not_ready', 'rules_pending', 'awaiting_airlock', 'awaiting_review', 'released', 'withheld', 'sent')", name=op.f('ck_run_summary_egress_state')),
    sa.CheckConstraint("state IN ('CANCELED', 'CANCELING', 'COMPLETE', 'EXECUTOR_ERROR', 'INITIALIZING', 'QUEUED', 'RUNNING', 'SYSTEM_ERROR')", name=op.f('ck_run_summary_state')),
    sa.ForeignKeyConstraint(['request_id'], ['request.request_id'], name=op.f('fk_run_summary_request_id_request')),
    sa.PrimaryKeyConstraint('run_id', name=op.f('pk_run_summary'))
    )
    op.create_table('study_version',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('study_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('digest', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('previous_digest', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('raw', sa.LargeBinary(), nullable=False),
    sa.Column('goal', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('proposer', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('members', sa.JSON(), nullable=False),
    sa.Column('nodes', sa.JSON(), nullable=False),
    sa.Column('datasets', sa.JSON(), nullable=False),
    sa.Column('output_rules', sa.JSON(), nullable=False),
    sa.Column('result_recipients', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('received_at', store.types.UtcDateTime(), nullable=False),
    sa.Column('state', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('state_changed_at', store.types.UtcDateTime(), nullable=False),
    sa.CheckConstraint("result_recipients IN ('submitter')", name=op.f('ck_study_version_result_recipients')),
    sa.CheckConstraint("state IN ('pending', 'approved', 'refused', 'withdrawn', 'superseded')", name=op.f('ck_study_version_state')),
    sa.ForeignKeyConstraint(['study_id'], ['study.id'], name=op.f('fk_study_version_study_id_study')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_study_version')),
    sa.UniqueConstraint('digest', name=op.f('uq_study_version_digest')),
    sa.UniqueConstraint('study_id', 'version', name=op.f('uq_study_version_study_id_version'))
    )
    with op.batch_alter_table('study_version', schema=None) as batch_op:
        batch_op.create_index('one_approved_version', ['study_id'], unique=True, sqlite_where=sa.text("state = 'approved'"), postgresql_where=sa.text("state = 'approved'"))

    op.create_table('member_key',
    sa.Column('study_version_id', sa.Integer(), nullable=False),
    sa.Column('member', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('ls_aai_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('signing_key', sa.JSON(), nullable=False),
    sa.Column('result_key', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('binding_token', sa.Text(), nullable=False),
    sa.Column('identity_verified', sa.Boolean(), nullable=False),
    sa.Column('identity_error', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('display_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.ForeignKeyConstraint(['study_version_id'], ['study_version.id'], name=op.f('fk_member_key_study_version_id_study_version')),
    sa.PrimaryKeyConstraint('study_version_id', 'member', name=op.f('pk_member_key'))
    )
    op.create_table('workflow_version',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('study_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('study_version_id', sa.Integer(), nullable=False),
    sa.Column('digest', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('raw', sa.LargeBinary(), nullable=False),
    sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('version', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('workflowhub_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('steps', sa.JSON(), nullable=False),
    sa.Column('received_at', store.types.UtcDateTime(), nullable=False),
    sa.Column('state', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('state_changed_at', store.types.UtcDateTime(), nullable=False),
    sa.CheckConstraint("state IN ('pending', 'approved', 'refused', 'withdrawn', 'superseded')", name=op.f('ck_workflow_version_state')),
    sa.ForeignKeyConstraint(['study_id'], ['study.id'], name=op.f('fk_workflow_version_study_id_study')),
    sa.ForeignKeyConstraint(['study_version_id'], ['study_version.id'], name=op.f('fk_workflow_version_study_version_id_study_version')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_workflow_version')),
    sa.UniqueConstraint('study_version_id', 'digest', name=op.f('uq_workflow_version_study_version_id_digest'))
    )


def downgrade():
    op.drop_table('workflow_version')
    op.drop_table('member_key')
    with op.batch_alter_table('study_version', schema=None) as batch_op:
        batch_op.drop_index('one_approved_version', sqlite_where=sa.text("state = 'approved'"), postgresql_where=sa.text("state = 'approved'"))

    op.drop_table('study_version')
    op.drop_table('run_summary')
    op.drop_table('study')
    with op.batch_alter_table('request', schema=None) as batch_op:
        batch_op.drop_index('request_received')
        batch_op.drop_index('request_payload')

    op.drop_table('request')
    op.drop_table('node_status')
    op.drop_table('federation_list')
    with op.batch_alter_table('decision', schema=None) as batch_op:
        batch_op.drop_index('decision_subject')

    op.drop_table('decision')
    op.drop_table('dataset_binding')
