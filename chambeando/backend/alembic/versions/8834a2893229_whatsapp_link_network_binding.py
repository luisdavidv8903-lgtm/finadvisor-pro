"""whatsapp link network binding

Revision ID: 8834a2893229
Revises: 0db65689ff43
Create Date: 2026-09-25 12:33:05.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8834a2893229'
down_revision: Union[str, Sequence[str], None] = '0db65689ff43'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Multichain refactor: binds a wallet-link token to the network_id it was
    # issued for, so a query-string `&network=` on the deep link can never be
    # swapped by whoever opens it -- consume_link_token() rejects a mismatch.
    # Nullable: existing rows (and any in-flight token issued before this
    # migration) predate the concept and are simply un-bound.
    op.add_column('whatsapp_links', sa.Column('link_token_network_id', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('whatsapp_links', 'link_token_network_id')
