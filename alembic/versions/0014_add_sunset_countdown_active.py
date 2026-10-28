"""Track whether a user's mailbox has Sunset countdown labels.

Set while the user is in Sunset; when they switch away, the next sweep
deletes the countdown labels and clears the flag.

Revision ID: 0014
Revises: 0013
"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"


def upgrade():
    op.add_column("scan_state", sa.Column("sunset_countdown_active", sa.Boolean(), server_default="false", nullable=False))


def downgrade():
    op.drop_column("scan_state", "sunset_countdown_active")
