"""Per-user session version, so logging out revokes the user's sessions.

Session tokens carry the version they were issued at; bumping it (logout,
account deletion) makes every earlier token invalid.

Revision ID: 0017
Revises: 0016
"""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"


def upgrade():
    op.add_column("users", sa.Column("session_version", sa.Integer(), server_default="0", nullable=False))


def downgrade():
    op.drop_column("users", "session_version")
