"""Turning service posts and reviews into responses, in batches."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.providers.storage import StorageProvider
from app.modules.feed.views import author_view, load_assets, load_users
from app.modules.location import cells
from app.modules.location.service import area_label_for_cell
from app.modules.messaging.views import JOB_SAFETY_NOTICE
from app.modules.moderation.models import ContentStatus
from app.modules.services import service as services_service
from app.modules.services.models import Review, ServiceKind, ServicePost
from app.modules.services.schemas import ProviderOut, ReviewOut, ServicePostOut
from app.modules.users.models import User
from app.modules.users.schemas import AreaLabelOut


async def post_views(
    session: AsyncSession,
    redis: Redis,
    storage: StorageProvider,
    *,
    posts: Sequence[ServicePost],
    viewer: User,
    settings: Settings,
    buckets: dict[uuid.UUID, cells.DistanceBucket] | None = None,
) -> list[ServicePostOut]:
    if not posts:
        return []
    authors = await load_users(session, (post.author_id for post in posts))
    assets = await load_assets(session, [user.avatar_media_id for user in authors.values()])
    chats = await services_service.my_conversations_about(
        session, viewer=viewer, post_ids=[post.id for post in posts]
    )
    labels: dict[int, AreaLabelOut] = {}
    for cell in {post.cell_res9 for post in posts}:
        label = await area_label_for_cell(session, redis, cell_res9=cell, settings=settings)
        labels[cell] = AreaLabelOut(label=label.label, precision=label.precision)

    now = datetime.now(UTC)
    views: list[ServicePostOut] = []
    for post in posts:
        author = authors.get(post.author_id)
        if author is None:  # pragma: no cover
            continue
        mine = post.author_id == viewer.id
        bucket = None
        if not mine:
            if buckets is not None and post.id in buckets:
                bucket = buckets[post.id]
            elif viewer.home_cell_res8 is not None:
                bucket = cells.bucket_between_cells(viewer.home_cell_res8, post.cell_res8)
        card = author_view(author, assets, storage)
        views.append(
            ServicePostOut(
                id=post.id,
                author=ProviderOut(
                    **card.model_dump(),
                    rating_average=float(author.rating_average) if author.rating_average else None,
                    rating_count=author.rating_count,
                ),
                kind=post.kind,
                category=post.category,
                title=post.title,
                description=post.description,
                rate_info=post.rate_info,
                availability_info=post.availability_info,
                travel_km=post.travel_km,
                budget_info=post.budget_info,
                job_type=post.job_type,
                pay_min_kes=post.pay_min_kes,
                pay_max_kes=post.pay_max_kes,
                pay_period=post.pay_period,
                area_label=labels[post.cell_res9],
                distance_bucket=bucket,
                is_open=post.is_open(now),
                expires_at=post.expires_at,
                closed_at=post.closed_at,
                is_mine=mine,
                my_conversation_id=chats.get(post.id),
                safety_notice=JOB_SAFETY_NOTICE if post.kind == ServiceKind.hiring else None,
                status=post.status,
                held_for=(
                    list(post.flagged_phrases)
                    if mine and post.status == ContentStatus.hidden_pending_review
                    else None
                ),
                created_at=post.created_at,
            )
        )
    return views


async def review_views(
    session: AsyncSession, storage: StorageProvider, *, reviews: Sequence[Review]
) -> list[ReviewOut]:
    reviewers = await load_users(session, (review.reviewer_id for review in reviews))
    assets = await load_assets(session, [user.avatar_media_id for user in reviewers.values()])
    return [
        ReviewOut(
            id=review.id,
            reviewer=author_view(reviewers[review.reviewer_id], assets, storage),
            subject_id=review.subject_id,
            context_type=review.context_type,
            context_id=review.context_id,
            rating=review.rating,
            body=review.body,
            reply_body=review.reply_body,
            replied_at=review.replied_at,
            status=review.status,
            created_at=review.created_at,
            updated_at=review.updated_at,
        )
        for review in reviews
        if review.reviewer_id in reviewers
    ]
