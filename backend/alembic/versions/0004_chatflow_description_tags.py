"""Add description and tags columns to chatflows.

Revision ID: 0004
Revises: 0003
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_chatflow_description_tags"
down_revision = "0003_folder_nesting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chatflows", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("chatflows", sa.Column("tags", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("chatflows", "tags")
    op.drop_column("chatflows", "description")
