"""Drop scan_state.auto_archive_unknown.

inbox_mode (0013) replaced it; Gatekeeper is derived from the mode and no
code reads or writes the column since #129.

Revision ID: 0016
Revises: 0015
"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"


def upgrade():
    op.drop_column("scan_state", "auto_archive_unknown")


def downgrade():
    op.add_column("scan_state", sa.Column("auto_archive_unknown", sa.Boolean(), server_default="true", nullable=False))
    op.execute("UPDATE scan_state SET auto_archive_unknown = (inbox_mode = 'gatekeeper')")
