"""Who may see a listing. A leaf: models only, so moderation can use it.

The rule is the post rule. Anyone signed in may open a listing by link — sold and
expired ones included, so a shared link does not break. Browse decides what is
*surfaced*. A listing hidden pending review is seen only by its seller, and one
removed by a moderator by nobody — the same as a post. Anyone blocked either way,
and anything deleted, is a 404.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenError, NotFoundError
from app.modules.marketplace.models import Listing
from app.modules.moderation.models import ContentStatus
from app.modules.users.service import is_blocked_between


def listing_not_found() -> NotFoundError:
    return NotFoundError("Listing not found")


async def visible_listing(
    session: AsyncSession, *, listing_id: uuid.UUID, viewer_id: uuid.UUID, lock: bool = False
) -> Listing:
    statement = select(Listing).where(Listing.id == listing_id, Listing.deleted_at.is_(None))
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    listing = (await session.execute(statement)).scalar_one_or_none()
    if listing is None:
        raise listing_not_found()
    if listing.status == ContentStatus.removed:
        raise listing_not_found()
    if listing.status == ContentStatus.hidden_pending_review and listing.seller_id != viewer_id:
        raise listing_not_found()
    if listing.seller_id != viewer_id and await is_blocked_between(
        session, viewer_id, listing.seller_id
    ):
        raise listing_not_found()
    return listing


async def own_listing(
    session: AsyncSession, *, listing_id: uuid.UUID, seller_id: uuid.UUID
) -> Listing:
    """A listing the caller is selling, locked for a change. Somebody else's, which
    they can see, is a 403 — as for editing someone else's post."""
    listing = await visible_listing(session, listing_id=listing_id, viewer_id=seller_id, lock=True)
    if listing.seller_id != seller_id:
        raise ForbiddenError("Only the seller can change a listing")
    return listing
