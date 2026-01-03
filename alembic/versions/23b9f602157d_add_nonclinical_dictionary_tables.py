# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""add nonclinical dictionary tables

Revision ID: 23b9f602157d
Revises: 
Create Date: 2025-12-05 16:34:10.111171

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "23b9f602157d"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
