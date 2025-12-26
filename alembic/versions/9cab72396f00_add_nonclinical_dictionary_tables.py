# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""add nonclinical dictionary tables

Revision ID: 9cab72396f00
Revises: 23b9f602157d
Create Date: 2025-12-05 16:43:08.787619

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9cab72396f00'
down_revision: Union[str, Sequence[str], None] = '23b9f602157d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
