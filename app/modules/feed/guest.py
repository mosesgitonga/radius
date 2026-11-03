"""The feed for someone who has not signed up yet (decision M.9).

A visitor sees what a member standing in the same spot would see in their feed,
so they can tell whether the neighbourhood is worth joining, and nothing that
names anyone:

- **Where:** a GPS reading of where the phone is, under the same rules as
  setting home (accurate to `LOCATION_MAX_ACCURACY_METRES`, never marked as
  faked). The coordinates become a cell and are discarded, and nothing about the
  visitor is stored.
- **What:** visible posts outside groups, within the feed radius of that cell,
  newest first. Each one shows no author: no name, no photo, no account ID. A
  list of who lives where must not be collectable without an account.
"""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import RadiusFeature, Settings
from app.core.pagination import PageParams, paginate
from app.modules.feed.models import Post
from app.modules.location import bands, cells
from app.modules.location.radius import RadiusEngine, RadiusResult
from app.modules.location.service import (
    Anchor,
    AreaLabel,
    area_label_for_cell,
    require_trustworthy_reading,
)
from app.modules.moderation.models import ContentStatus


@dataclass(frozen=True, slots=True)
class GuestPage:
    posts: list[Post]
    next_cursor: str | None
    radius: RadiusResult
    anchor: Anchor
    area: AreaLabel


async def guest_feed_page(
    session: AsyncSession,
    redis: Redis,
    *,
    latitude: float,
    longitude: float,
    accuracy_metres: float,
    is_mocked: bool,
    params: PageParams,
    settings: Settings,
) -> GuestPage:
    require_trustworthy_reading(
        accuracy_metres=accuracy_metres, is_mocked=is_mocked, settings=settings
    )
    located = cells.locate(latitude, longitude)
    # Not a home and never saved: "explore" is the anchor kind for a place that
    # is not the viewer's home.
    anchor = Anchor(mode="explore", cell_res9=located.cell_res9, cell_res8=located.cell_res8)
    radius = await RadiusEngine(session, redis, settings).radius_for(
        RadiusFeature.feed, anchor.cell_res8
    )
    statement: Select[Post] = select(Post).where(
        Post.status == ContentStatus.visible,
        Post.deleted_at.is_(None),
        Post.group_id.is_(None),
        bands.within(Post.centroid, anchor, radius.radius_m),
    )
    posts, next_cursor = await paginate(
        session, statement, sort_column=Post.created_at, id_column=Post.id, params=params
    )
    area = await area_label_for_cell(session, redis, cell_res9=anchor.cell_res9, settings=settings)
    return GuestPage(posts=posts, next_cursor=next_cursor, radius=radius, anchor=anchor, area=area)
