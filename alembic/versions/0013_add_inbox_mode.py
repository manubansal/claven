"""Add inbox_mode (label_only / gatekeeper / sunset) and the Sunset timer.

Existing users keep their behaviour: auto_archive_unknown=true becomes
gatekeeper, false becomes label_only. auto_archive_unknown is kept (and kept
in sync by the app) so a rollback to the previous release still works; drop
it in a later migration.

Revision ID: 0013
Revises: 0012
"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"


def upgrade():
    op.add_column("scan_state", sa.Column("inbox_mode", sa.Text(), server_default="gatekeeper", nullable=False))
    op.add_column("scan_state", sa.Column("sunset_hours", sa.Integer(), server_default="24", nullable=False))
    op.add_column("scan_state", sa.Column("inbox_mode_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE scan_state SET inbox_mode = 'label_only' WHERE NOT auto_archive_unknown")
    op.create_check_constraint(
        "scan_state_inbox_mode_check", "scan_state",
        "inbox_mode IN ('label_only', 'gatekeeper', 'sunset')",
    )


def downgrade():
    op.drop_constraint("scan_state_inbox_mode_check", "scan_state")
    op.drop_column("scan_state", "inbox_mode_changed_at")
    op.drop_column("scan_state", "sunset_hours")
    op.drop_column("scan_state", "inbox_mode")
