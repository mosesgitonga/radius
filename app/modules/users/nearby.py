"""People nearby: neighbours who chose to be found (decision M.8).

Three rules, all on the server:

- **Opt-in.** Only people who turned on `show_in_nearby` are listed.
- **Proven on both sides.** Only someone who may contact strangers (M.7) can look,
  and only such people are listed. A home set by a faked location, not yet kept
  up for four days of a week, finds no one and is found by no one.
- **The feed's reach, from home.** Listed people live within the viewer's feed
  radius of home, never of an explore location. They are shown by an area label
  and a distance band, as everywhere; never a last-seen time or online status.
"""

from __future__ import annotations

from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import RadiusFeature, Settings
from app.core.pagination import PageParams, paginate
from app.modules.location import bands
from app.modules.location.radius import RadiusEngine
from app.modules.location.service import resolve_anchor
from app.modules.users import contact
from app.modules.users.models import User, UserStatus
from app.modules.users.service import blocked_user_ids


async def people_nearby(
    session: AsyncSession,
    redis: Redis,
    *,
    viewer: User,
    params: PageParams,
    settings: Settings,
) -> tuple[list[User], str | None]:
    """Newest members first. Raises 403 `contact_locked` for an unproven viewer."""
    contact.require_can_contact_strangers(viewer, settings=settings, now=datetime.now(UTC))
    anchor = await resolve_anchor(redis, user=viewer, mode="home")
    if anchor is None:
        return [], None
    radius = await RadiusEngine(session, redis, settings).radius_for(
        RadiusFeature.feed, anchor.cell_res8
    )
    blocked = await blocked_user_ids(session, viewer.id)

    statement: Select[User] = select(User).where(
        User.id != viewer.id,
        User.show_in_nearby.is_(True),
        User.status == UserStatus.active,
        User.deleted_at.is_(None),
        User.deletion_requested_at.is_(None),
        # Proven, as `contact.can_contact_strangers` decides it.
        or_(User.is_verified_neighbour.is_(True), User.contact_unlocked_at.is_not(None)),
        bands.within(User.home_centroid, anchor, radius.radius_m),
    )
    if blocked:
        statement = statement.where(User.id.not_in(blocked))
    return await paginate(
        session, statement, sort_column=User.created_at, id_column=User.id, params=params
    )
