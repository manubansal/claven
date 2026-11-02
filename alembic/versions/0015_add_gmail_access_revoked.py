"""Remember when Google rejected a user's refresh token.

Set when a token refresh fails with invalid_grant (the user revoked access,
or the grant expired); background jobs then skip the user without calling
Google. Cleared when the user signs in again and new tokens are stored.

Revision ID: 0015
Revises: 0014
"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"


def upgrade():
    op.add_column("gmail_tokens", sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade():
    op.drop_column("gmail_tokens", "revoked_at")
