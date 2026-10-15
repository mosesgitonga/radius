"""Reviews: who may write one, and keeping ratings honest.

A review needs grounds. The reviewer and the person reviewed must have had an
*accepted* conversation — one the other side replied to — about exactly the
listing or service post the review is for. That stops the two classic abuses: a
stranger leaving a one-star review out of spite, and a seller's friends leaving
five-star reviews for things they never bought. One review per pair per context;
the reviewed person may reply once, and edit their reply.

Every change to the set of visible reviews recomputes the subject's cached rating
from scratch (`ratings.recompute`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import execute_dml
from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.core.pagination import PageParams, paginate
from app.modules.marketplace.models import Listing
from app.modules.messaging import service as messaging_service
from app.modules.moderation.models import ContentStatus
from app.modules.notifications import service as notifications_service
from app.modules.notifications.models import NotificationType
from app.modules.services import access, ratings
from app.modules.services.models import Review, ReviewContext, ServicePost
from app.modules.users.models import User, UserStatus
from app.modules.users.service import blocked_user_ids, get_user, is_blocked_between


def _clean_body(body: str | None, settings: Settings) -> str:
    cleaned = (body or "").strip()
    if len(cleaned) > settings.review_body_max_length:
        raise BadRequestError(
            "That review is too long", details={"max_length": settings.review_body_max_length}
        )
    return cleaned


async def _context_exists(
    session: AsyncSession, context_type: ReviewContext, context_id: uuid.UUID
) -> bool:
    """The thing reviewed must exist. It may since have sold, closed or even been
    deleted by its owner — a review of a completed sale is the point."""
    model: Any = Listing if context_type == ReviewContext.listing else ServicePost
    return await session.scalar(select(model.id).where(model.id == context_id)) is not None


async def create_review(
    session: AsyncSession,
    *,
    reviewer: User,
    subject_id: uuid.UUID,
    context_type: ReviewContext,
    context_id: uuid.UUID,
    rating: int,
    body: str | None,
    settings: Settings,
) -> Review:
    if subject_id == reviewer.id:
        raise BadRequestError("You cannot review yourself")
    if not 1 <= rating <= 5:
        raise BadRequestError("A rating is from 1 to 5 stars")
    subject = await get_user(session, subject_id)
    if (
        subject is None
        or subject.deleted_at is not None
        or subject.status == UserStatus.banned
        or await is_blocked_between(session, reviewer.id, subject_id)
    ):
        raise NotFoundError("User not found")
    if not await _context_exists(session, context_type, context_id):
        raise NotFoundError("Nothing to review there")

    conversation = await messaging_service.accepted_conversation_about(
        session,
        first=reviewer.id,
        second=subject_id,
        listing_id=context_id if context_type == ReviewContext.listing else None,
        service_post_id=context_id if context_type == ReviewContext.service_post else None,
    )
    if conversation is None:
        raise ForbiddenError(
            "You can review someone only after a conversation with them about this",
            details={"reason": "no_qualifying_conversation"},
        )

    review = Review(
        reviewer_id=reviewer.id,
        subject_id=subject_id,
        context_type=context_type,
        context_id=context_id,
        conversation_id=conversation.id,
        rating=rating,
        body=_clean_body(body, settings),
    )
    try:
        async with session.begin_nested():
            session.add(review)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(
            select(Review.id).where(
                Review.reviewer_id == reviewer.id,
                Review.subject_id == subject_id,
                Review.context_type == context_type,
                Review.context_id == context_id,
                Review.deleted_at.is_(None),
            )
        )
        raise ConflictError(
            "You have already reviewed them for this; edit that review instead",
            details={"reason": "already_reviewed", "review_id": str(existing)},
        ) from None

    await ratings.recompute(session, subject_id)
    await notifications_service.notify(
        session,
        recipient_id=subject_id,
        kind=NotificationType.review_received,
        actor_id=reviewer.id,
        target_type="review",
        target_id=review.id,
        collapse_key=f"review_received:{subject_id}",
        data={"review_id": str(review.id)},
        settings=settings,
    )
    await session.refresh(review)
    return review


async def update_review(
    session: AsyncSession,
    *,
    reviewer: User,
    review_id: uuid.UUID,
    rating: int | None,
    body: str | None,
    settings: Settings,
) -> Review:
    review = await access.visible_review(
        session, review_id=review_id, viewer_id=reviewer.id, lock=True
    )
    if review.reviewer_id != reviewer.id:
        raise ForbiddenError("Only its author can change a review")
    if rating is not None:
        if not 1 <= rating <= 5:
            raise BadRequestError("A rating is from 1 to 5 stars")
        review.rating = rating
    if body is not None:
        review.body = _clean_body(body, settings)
    await session.flush()
    await ratings.recompute(session, review.subject_id)
    await session.refresh(review)
    return review


async def delete_review(session: AsyncSession, *, reviewer: User, review_id: uuid.UUID) -> None:
    review = await access.visible_review(
        session, review_id=review_id, viewer_id=reviewer.id, lock=True
    )
    if review.reviewer_id != reviewer.id:
        raise ForbiddenError("Only its author can delete a review")
    review.deleted_at = datetime.now(UTC)
    await session.flush()
    await ratings.recompute(session, review.subject_id)


async def reply(
    session: AsyncSession,
    *,
    subject: User,
    review_id: uuid.UUID,
    body: str | None,
    settings: Settings,
) -> Review:
    """Reply to a review of you, or remove your reply with None."""
    review = await access.visible_review(
        session, review_id=review_id, viewer_id=subject.id, lock=True
    )
    if review.subject_id != subject.id:
        raise ForbiddenError("Only the person reviewed can reply")
    cleaned = _clean_body(body, settings) if body is not None else ""
    first_reply = review.reply_body is None and bool(cleaned)
    review.reply_body = cleaned or None
    review.replied_at = datetime.now(UTC) if cleaned else None
    await session.flush()
    if first_reply:
        await notifications_service.notify(
            session,
            recipient_id=review.reviewer_id,
            kind=NotificationType.review_reply,
            actor_id=subject.id,
            target_type="review",
            target_id=review.id,
            collapse_key=f"review_reply:{review.id}",
            data={"review_id": str(review.id)},
            settings=settings,
        )
    await session.refresh(review)
    return review


async def reviews_of(
    session: AsyncSession, *, viewer: User, subject_id: uuid.UUID, params: PageParams
) -> tuple[User, list[Review], str | None]:
    """A person's visible reviews, newest first, with their cached rating.

    Reviews by anyone the viewer has blocked, or who blocked them, are left out,
    and so is the whole list if the viewer and the subject are blocked either way.
    """
    subject = await get_user(session, subject_id)
    if (
        subject is None
        or subject.deleted_at is not None
        or (subject_id != viewer.id and await is_blocked_between(session, viewer.id, subject_id))
    ):
        raise NotFoundError("User not found")
    statement = select(Review).where(
        Review.subject_id == subject_id,
        Review.deleted_at.is_(None),
        or_(
            Review.status == ContentStatus.visible,
            (Review.status == ContentStatus.hidden_pending_review)
            & (Review.reviewer_id == viewer.id),
        ),
    )
    hidden = await blocked_user_ids(session, viewer.id)
    if hidden:
        statement = statement.where(Review.reviewer_id.not_in(hidden))
    rows, next_cursor = await paginate(
        session, statement, sort_column=Review.created_at, id_column=Review.id, params=params
    )
    return subject, rows, next_cursor


async def get_review(session: AsyncSession, *, viewer: User, review_id: uuid.UUID) -> Review:
    return await access.visible_review(session, review_id=review_id, viewer_id=viewer.id)


# ---------------------------------------------------------------------------
# Data rights
# ---------------------------------------------------------------------------
async def purge_user_reviews(session: AsyncSession, *, user_id: uuid.UUID) -> dict[str, int]:
    """Soft-delete reviews a purged account wrote, and those about it, then refresh
    the ratings of everyone they had reviewed."""
    subjects = set(
        (
            await session.execute(
                select(Review.subject_id).where(
                    Review.reviewer_id == user_id, Review.deleted_at.is_(None)
                )
            )
        ).scalars()
    )
    now = datetime.now(UTC)
    written = await execute_dml(
        session,
        update(Review)
        .where(Review.reviewer_id == user_id, Review.deleted_at.is_(None))
        .values(deleted_at=now),
    )
    received = await execute_dml(
        session,
        update(Review)
        .where(Review.subject_id == user_id, Review.deleted_at.is_(None))
        .values(deleted_at=now),
    )
    for subject_id in subjects | {user_id}:
        await ratings.recompute(session, subject_id)
    return {"written": written, "received": received}


async def export_user_reviews(session: AsyncSession, *, user_id: uuid.UUID) -> dict[str, Any]:
    """Reviews a person wrote and reviews about them. Unlike private messages,
    reviews are public statements about the person, so both are theirs to see."""

    def _row(review: Review) -> dict[str, Any]:
        return {
            "id": str(review.id),
            "reviewer_id": str(review.reviewer_id),
            "subject_id": str(review.subject_id),
            "context_type": str(review.context_type),
            "context_id": str(review.context_id),
            "rating": review.rating,
            "body": review.body,
            "reply": review.reply_body,
            "status": str(review.status),
            "created_at": review.created_at.isoformat(),
            "deleted_at": review.deleted_at.isoformat() if review.deleted_at else None,
        }

    written = (
        await session.execute(
            select(Review).where(Review.reviewer_id == user_id).order_by(Review.created_at)
        )
    ).scalars()
    received = (
        await session.execute(
            select(Review).where(Review.subject_id == user_id).order_by(Review.created_at)
        )
    ).scalars()
    return {
        "written": [_row(review) for review in written],
        "received": [_row(review) for review in received],
    }
