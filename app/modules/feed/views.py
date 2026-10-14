"""Turning feed rows into responses.

Everything here is batched. A feed page of twenty posts needs authors, avatars,
images, area labels and the viewer's own reactions; looked up per post, that is a
hundred-odd queries for one screen on a phone that is paying for every byte of the
latency. Batched, it is five, whatever the page size.

This is also where the privacy rules meet the output. A post is placed by an area
label computed from its own cell with k-anonymity, and by a distance band from the
viewer — never by anything a client could turn back into a position.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.providers.storage import StorageProvider
from app.modules.feed.models import Comment, Post, Story, StoryKind
from app.modules.feed.schemas import (
    AuthorOut,
    CommentOut,
    PostImageOut,
    PostOut,
    StoryOut,
)
from app.modules.feed.service import reactions_by
from app.modules.location import cells
from app.modules.location.service import area_label_for_cell
from app.modules.media.models import MediaAsset
from app.modules.media.views import image_variants
from app.modules.users.models import User
from app.modules.users.schemas import AreaLabelOut


async def load_users(session: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, User]:
    wanted = set(ids)
    if not wanted:
        return {}
    rows = (await session.execute(select(User).where(User.id.in_(wanted)))).scalars()
    return {user.id: user for user in rows}


async def load_assets(
    session: AsyncSession, ids: Iterable[uuid.UUID | None]
) -> dict[uuid.UUID, MediaAsset]:
    wanted = {asset_id for asset_id in ids if asset_id is not None}
    if not wanted:
        return {}
    rows = (await session.execute(select(MediaAsset).where(MediaAsset.id.in_(wanted)))).scalars()
    return {asset.id: asset for asset in rows}


def author_view(
    user: User, assets: dict[uuid.UUID, MediaAsset], storage: StorageProvider
) -> AuthorOut:
    return AuthorOut(
        id=user.id,
        display_name=user.display_name,
        avatar=image_variants(
            assets.get(user.avatar_media_id) if user.avatar_media_id else None, storage
        ),
        is_verified_neighbour=user.is_verified_neighbour,
    )


async def _labels(
    session: AsyncSession, redis: Redis, cells_res9: Iterable[int], settings: Settings
) -> dict[int, AreaLabelOut]:
    """One k-anonymous label per distinct cell.

    Posts from one neighbourhood share a cell, and the label lookup is cached in
    Redis, so this is usually a handful of cache hits for a whole page.
    """
    labels: dict[int, AreaLabelOut] = {}
    for cell in set(cells_res9):
        label = await area_label_for_cell(session, redis, cell_res9=cell, settings=settings)
        labels[cell] = AreaLabelOut(label=label.label, precision=label.precision)
    return labels


async def post_views(
    session: AsyncSession,
    redis: Redis,
    storage: StorageProvider,
    *,
    posts: list[Post],
    viewer: User,
    viewer_cell_res8: int | None,
    settings: Settings,
) -> list[PostOut]:
    if not posts:
        return []

    authors = await load_users(session, (post.author_id for post in posts))
    image_ids = [image.media_id for post in posts for image in post.images]
    avatar_ids = [author.avatar_media_id for author in authors.values()]
    assets = await load_assets(session, [*image_ids, *avatar_ids])
    labels = await _labels(session, redis, (post.cell_res9 for post in posts), settings)
    mine = await reactions_by(session, user_id=viewer.id, post_ids=[post.id for post in posts])

    views: list[PostOut] = []
    for post in posts:
        author = authors.get(post.author_id)
        if author is None:  # pragma: no cover — the author row cascades with the post
            continue
        bucket = None
        if viewer_cell_res8 is not None and post.author_id != viewer.id:
            bucket = cells.bucket_between_cells(viewer_cell_res8, post.cell_res8)
        views.append(
            PostOut(
                id=post.id,
                author=author_view(author, assets, storage),
                body=post.body,
                category=post.category,
                images=[
                    PostImageOut(media_id=image.media_id, variants=variants)
                    for image in post.images
                    if (variants := image_variants(assets.get(image.media_id), storage))
                ],
                area_label=labels[post.cell_res9],
                distance_bucket=bucket,
                comment_count=post.comment_count,
                reaction_count=post.reaction_count,
                my_reaction=mine.get(post.id),
                status=post.status,
                created_at=post.created_at,
            )
        )
    return views


async def comment_views(
    session: AsyncSession,
    redis: Redis,
    storage: StorageProvider,
    *,
    comments: list[Comment],
    viewer: User,
    settings: Settings,
) -> list[CommentOut]:
    """Comments with their authors' *home* labels.

    A visitor's comment shows where they live, with the visiting flag, never where
    they were standing when they wrote it — decision 1.1, rule 3.
    """
    if not comments:
        return []

    authors = await load_users(session, (comment.author_id for comment in comments))
    assets = await load_assets(session, (author.avatar_media_id for author in authors.values()))
    home_cells = [
        author.home_cell_res9 for author in authors.values() if author.home_cell_res9 is not None
    ]
    labels = await _labels(session, redis, home_cells, settings)
    mine = await reactions_by(
        session, user_id=viewer.id, comment_ids=[comment.id for comment in comments]
    )

    views: list[CommentOut] = []
    for comment in comments:
        author = authors.get(comment.author_id)
        if author is None:  # pragma: no cover
            continue
        views.append(
            CommentOut(
                id=comment.id,
                post_id=comment.post_id,
                parent_id=comment.parent_id,
                author=author_view(author, assets, storage),
                author_area_label=(
                    labels.get(author.home_cell_res9) if author.home_cell_res9 else None
                ),
                is_visiting=comment.is_visiting,
                body=comment.body,
                reply_count=comment.reply_count,
                reaction_count=comment.reaction_count,
                my_reaction=mine.get(comment.id),
                created_at=comment.created_at,
            )
        )
    return views


async def story_views(
    session: AsyncSession,
    redis: Redis,
    storage: StorageProvider,
    *,
    stories: list[Story],
    viewer: User,
    viewer_cell_res8: int | None,
    settings: Settings,
) -> list[StoryOut]:
    if not stories:
        return []

    authors = await load_users(session, (story.author_id for story in stories))
    assets = await load_assets(
        session,
        [
            *(story.media_id for story in stories),
            *(author.avatar_media_id for author in authors.values()),
        ],
    )
    labels = await _labels(session, redis, (story.cell_res9 for story in stories), settings)

    views: list[StoryOut] = []
    for story in stories:
        author = authors.get(story.author_id)
        if author is None:  # pragma: no cover
            continue
        own = story.author_id == viewer.id
        bucket = (
            None
            if own or viewer_cell_res8 is None
            else cells.bucket_between_cells(viewer_cell_res8, story.cell_res8)
        )
        views.append(
            StoryOut(
                id=story.id,
                author=author_view(author, assets, storage),
                kind=story.kind.value,
                image=(
                    image_variants(assets.get(story.media_id), storage)
                    if story.kind == StoryKind.image and story.media_id
                    else None
                ),
                text=story.text,
                area_label=labels[story.cell_res9],
                distance_bucket=bucket,
                expires_at=story.expires_at,
                created_at=story.created_at,
                # Only the author learns how many people looked.
                view_count=story.view_count if own else None,
            )
        )
    return views


async def author_views_for(
    session: AsyncSession, storage: StorageProvider, users: list[User]
) -> dict[uuid.UUID, AuthorOut]:
    assets = await load_assets(session, (user.avatar_media_id for user in users))
    return {user.id: author_view(user, assets, storage) for user in users}
