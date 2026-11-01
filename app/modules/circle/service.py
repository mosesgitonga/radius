"""Circle requests, links, and who sees whom.

The rules (decisions C.1-C.3):

- **Mutual.** A request becomes a link only when the other person accepts. Two
  people asking each other is both accepting.
- **Silent.** A decline is never shown to the sender: their request still looks
  pending until it would have expired, then disappears exactly as an unanswered
  one does. Removing someone is silent too. Neither is notified.
- **A cooldown** after a decline or an expiry, so a "no" is not answered by
  asking again tomorrow.
- **Every refusal looks the same:** policy, cooldown or a full circle on the
  other side all give `circle_request_not_allowed`, so a refusal says nothing
  about the other person's choices. A block is a 404, as everywhere.
- **The contact rule** (M.7): a request reaches someone, so a newcomer may send
  one only to someone they already have an accepted conversation with.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from redis.asyncio import Redis
from sqlalchemy import Select, delete, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import RadiusFeature, Settings
from app.core.db import execute_dml
from app.core.errors import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
)
from app.core.pagination import PageParams, decode_cursor, encode_cursor, paginate
from app.core.ratelimit import get_rate_limiter
from app.modules.circle import links
from app.modules.circle.models import CircleLink, CircleRequest, CircleRequestStatus
from app.modules.feed import service as feed_service
from app.modules.location import bands
from app.modules.location.radius import RadiusEngine
from app.modules.location.service import resolve_anchor
from app.modules.messaging import service as messaging_service
from app.modules.notifications import service as notifications_service
from app.modules.notifications.models import NotificationType
from app.modules.users import contact
from app.modules.users import service as users_service
from app.modules.users.models import CircleRequestPolicy, User, UserStatus
from app.modules.users.standing import is_restricted

logger = logging.getLogger(__name__)

NOT_ALLOWED = "circle_request_not_allowed"

Direction = Literal["incoming", "outgoing"]


class Relationship(StrEnum):
    """How the viewer stands with someone, as a profile shows it. Never `declined`
    and never `blocked`: a block is a 404 before this is asked."""

    none = "none"
    request_sent = "request_sent"
    request_received = "request_received"
    in_circle = "in_circle"


@dataclass(frozen=True, slots=True)
class SendOutcome:
    relationship: Relationship
    request: CircleRequest | None


def _not_allowed() -> ForbiddenError:
    return ForbiddenError(
        "You can't add this person to your circle right now", details={"reason": NOT_ALLOWED}
    )


def _request_not_found() -> NotFoundError:
    return NotFoundError("Request not found")


def _expiry(settings: Settings, now: datetime) -> datetime:
    """Requests made before this have expired, whether or not the worker has yet
    marked them so."""
    return now - timedelta(days=settings.circle_request_expiry_days)


def _sender_sees_pending(user_id: uuid.UUID, settings: Settings, now: datetime) -> Any:
    """Requests `user_id` sent that still look pending to them: pending, or declined
    without their knowing, and not yet expired or withdrawn."""
    return (
        (CircleRequest.requester_id == user_id)
        & CircleRequest.status.in_((CircleRequestStatus.pending, CircleRequestStatus.declined))
        & CircleRequest.cancelled_at.is_(None)
        & (CircleRequest.created_at > _expiry(settings, now))
    )


def _awaiting(user_id: uuid.UUID, settings: Settings, now: datetime) -> Any:
    """Requests waiting for `user_id` to answer."""
    return (
        (CircleRequest.recipient_id == user_id)
        & (CircleRequest.status == CircleRequestStatus.pending)
        & (CircleRequest.created_at > _expiry(settings, now))
    )


async def _person(session: AsyncSession, *, viewer: User, user_id: uuid.UUID) -> User:
    """Someone the viewer may deal with, or a 404 that does not say why."""
    other = await users_service.get_user(session, user_id)
    if (
        other is None
        or other.deleted_at is not None
        or other.status in (UserStatus.banned, UserStatus.deletion_pending)
        or await users_service.is_blocked_between(session, viewer.id, user_id)
    ):
        raise NotFoundError("User not found")
    return other


# ---------------------------------------------------------------------------
# Where two people stand
# ---------------------------------------------------------------------------
async def relationship(
    session: AsyncSession,
    *,
    viewer_id: uuid.UUID,
    other_id: uuid.UUID,
    settings: Settings,
    now: datetime | None = None,
) -> Relationship:
    if viewer_id == other_id:
        return Relationship.none
    if await links.in_circle(session, viewer_id, other_id):
        return Relationship.in_circle
    moment = now or datetime.now(UTC)
    sent = await session.scalar(
        select(CircleRequest.id)
        .where(
            _sender_sees_pending(viewer_id, settings, moment),
            CircleRequest.recipient_id == other_id,
        )
        .limit(1)
    )
    if sent is not None:
        return Relationship.request_sent
    received = await session.scalar(
        select(CircleRequest.id)
        .where(_awaiting(viewer_id, settings, moment), CircleRequest.requester_id == other_id)
        .limit(1)
    )
    return Relationship.request_received if received is not None else Relationship.none


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------
async def _in_cooldown(
    session: AsyncSession,
    *,
    requester_id: uuid.UUID,
    recipient_id: uuid.UUID,
    settings: Settings,
    now: datetime,
) -> bool:
    """Whether the last "no", or silence, from this person is recent enough to
    hold the requester back. Directional: the one who declined may still ask."""
    cooldown = timedelta(days=settings.circle_request_cooldown_days)
    expiry = timedelta(days=settings.circle_request_expiry_days)
    same_pair = (CircleRequest.requester_id == requester_id) & (
        CircleRequest.recipient_id == recipient_id
    )
    declined = (CircleRequest.status == CircleRequestStatus.declined) & (
        CircleRequest.responded_at > now - cooldown
    )
    # An expiry is measured from when the request expired, so whether the worker
    # has marked it yet makes no difference.
    expired = (
        CircleRequest.status.in_((CircleRequestStatus.expired, CircleRequestStatus.pending))
        & (CircleRequest.created_at <= now - expiry)
        & (CircleRequest.created_at > now - expiry - cooldown)
    )
    found = await session.scalar(
        select(CircleRequest.id).where(same_pair, or_(declined, expired)).limit(1)
    )
    return found is not None


async def _allowed_by_policy(
    session: AsyncSession, redis: Redis, *, requester: User, recipient: User, settings: Settings
) -> bool:
    policy = recipient.circle_request_policy
    if policy == CircleRequestPolicy.everyone:
        return True
    if policy == CircleRequestPolicy.nearby_only:
        return await feed_service.author_within_feed(
            session, redis, viewer=recipient, author=requester, settings=settings
        )
    if policy == CircleRequestPolicy.contacts_only:
        return await messaging_service.has_accepted_conversation(
            session, requester.id, recipient.id
        )
    return False


async def _expire_stale_between(
    session: AsyncSession, first: uuid.UUID, second: uuid.UUID, settings: Settings, now: datetime
) -> None:
    """Mark this pair's out-of-date pending requests expired, so the one-pending-
    per-pair index does not trip over a request the worker has not reached."""
    await execute_dml(
        session,
        update(CircleRequest)
        .where(
            or_(
                (CircleRequest.requester_id == first) & (CircleRequest.recipient_id == second),
                (CircleRequest.requester_id == second) & (CircleRequest.recipient_id == first),
            ),
            CircleRequest.status == CircleRequestStatus.pending,
            CircleRequest.created_at <= _expiry(settings, now),
        )
        .values(status=CircleRequestStatus.expired)
        .execution_options(synchronize_session=False),
    )


async def _check_allowance(user: User, settings: Settings, now: datetime) -> None:
    young = now - user.created_at < timedelta(days=settings.circle_new_account_days)
    limit = (
        settings.new_account_circle_requests_per_day
        if young or is_restricted(user)
        else settings.circle_requests_per_day
    )
    result = await get_rate_limiter().hit(
        "circle_request", f"u:{user.id}", limit=limit, window_seconds=86_400
    )
    if not result.allowed:
        logger.info("rate_limited", extra={"bucket": "circle_request", "scope": "user"})
        raise RateLimitedError(retry_after_seconds=result.retry_after_seconds)


async def send_request(
    session: AsyncSession,
    redis: Redis,
    *,
    requester: User,
    recipient_id: uuid.UUID,
    settings: Settings,
    now: datetime | None = None,
) -> SendOutcome:
    """Ask someone to join your circle. Idempotent: asking again while a request
    looks pending returns it, and asking someone already in your circle is not
    an error. If they had already asked you, this accepts their request."""
    moment = now or datetime.now(UTC)
    if recipient_id == requester.id:
        raise BadRequestError("You cannot add yourself to your circle")
    recipient = await _person(session, viewer=requester, user_id=recipient_id)

    if await links.in_circle(session, requester.id, recipient.id):
        return SendOutcome(relationship=Relationship.in_circle, request=None)

    await _expire_stale_between(session, requester.id, recipient.id, settings, moment)

    theirs = await session.scalar(
        select(CircleRequest)
        .where(
            _awaiting(requester.id, settings, moment), CircleRequest.requester_id == recipient.id
        )
        .with_for_update()
    )
    if theirs is not None:
        await _accept(session, request=theirs, acceptor=requester, settings=settings, now=moment)
        return SendOutcome(relationship=Relationship.in_circle, request=theirs)

    mine = await session.scalar(
        select(CircleRequest).where(
            _sender_sees_pending(requester.id, settings, moment),
            CircleRequest.recipient_id == recipient.id,
        )
    )
    if mine is not None:
        return SendOutcome(relationship=Relationship.request_sent, request=mine)

    if is_restricted(requester):
        raise ForbiddenError(
            "Your account is restricted and cannot send circle requests for now",
            details={"reason": "restricted"},
        )
    knows_them = await messaging_service.has_accepted_conversation(
        session, requester.id, recipient.id
    )
    if not knows_them:
        contact.require_can_contact_strangers(requester, settings=settings, now=moment)

    if await links.circle_size(session, requester.id) >= settings.circle_max_members:
        raise ConflictError(
            f"Your circle is full ({settings.circle_max_members} people)",
            details={"reason": "circle_full", "max_members": settings.circle_max_members},
        )
    pending = int(
        await session.scalar(
            select(func.count())
            .select_from(CircleRequest)
            .where(_sender_sees_pending(requester.id, settings, moment))
        )
        or 0
    )
    if pending >= settings.circle_max_pending_outgoing:
        raise ConflictError(
            "You have too many requests waiting for an answer",
            details={
                "reason": "too_many_pending",
                "max_pending": settings.circle_max_pending_outgoing,
            },
        )

    if (
        await _in_cooldown(
            session,
            requester_id=requester.id,
            recipient_id=recipient.id,
            settings=settings,
            now=moment,
        )
        or not await _allowed_by_policy(
            session, redis, requester=requester, recipient=recipient, settings=settings
        )
        or await links.circle_size(session, recipient.id) >= settings.circle_max_members
    ):
        raise _not_allowed()

    await _check_allowance(requester, settings, moment)

    request = CircleRequest(requester_id=requester.id, recipient_id=recipient.id)
    try:
        async with session.begin_nested():
            session.add(request)
            await session.flush()
    except IntegrityError:
        # They asked us at the same instant. Theirs won the index, so accept it.
        theirs = await session.scalar(
            select(CircleRequest)
            .where(
                _awaiting(requester.id, settings, moment),
                CircleRequest.requester_id == recipient.id,
            )
            .with_for_update()
        )
        if theirs is None:  # pragma: no cover — the unique index guarantees a row
            raise
        await _accept(session, request=theirs, acceptor=requester, settings=settings, now=moment)
        return SendOutcome(relationship=Relationship.in_circle, request=theirs)

    await notifications_service.notify(
        session,
        recipient_id=recipient.id,
        kind=NotificationType.circle,
        actor_id=requester.id,
        target_type="circle_request",
        target_id=request.id,
        collapse_key=f"circle_request:{request.id}",
        data={"event": "request", "request_id": str(request.id), "user_id": str(requester.id)},
        push_text=f"{requester.display_name} wants to add you to their circle",
        settings=settings,
    )
    logger.info("circle_request_sent")
    return SendOutcome(relationship=Relationship.request_sent, request=request)


async def _accept(
    session: AsyncSession,
    *,
    request: CircleRequest,
    acceptor: User,
    settings: Settings,
    now: datetime,
) -> None:
    requester_id = request.requester_id
    if await links.circle_size(session, acceptor.id) >= settings.circle_max_members:
        raise ConflictError(
            f"Your circle is full ({settings.circle_max_members} people)",
            details={"reason": "circle_full", "max_members": settings.circle_max_members},
        )
    if await links.circle_size(session, requester_id) >= settings.circle_max_members:
        raise _not_allowed()

    low, high = CircleLink.pair(acceptor.id, requester_id)
    await session.execute(
        insert(CircleLink)
        .values(user_low_id=low, user_high_id=high)
        .on_conflict_do_nothing(index_elements=["user_low_id", "user_high_id"])
    )
    request.status = CircleRequestStatus.accepted
    request.responded_at = now
    # A request one of them had sent and the other quietly declined would go on
    # looking pending to its sender. They are in each other's circle now.
    await execute_dml(
        session,
        update(CircleRequest)
        .where(
            or_(
                (CircleRequest.requester_id == acceptor.id)
                & (CircleRequest.recipient_id == requester_id),
                (CircleRequest.requester_id == requester_id)
                & (CircleRequest.recipient_id == acceptor.id),
            ),
            CircleRequest.status == CircleRequestStatus.declined,
            CircleRequest.cancelled_at.is_(None),
        )
        .values(cancelled_at=now)
        .execution_options(synchronize_session=False),
    )
    await session.flush()

    await notifications_service.notify(
        session,
        recipient_id=requester_id,
        kind=NotificationType.circle,
        actor_id=acceptor.id,
        target_type="user",
        target_id=acceptor.id,
        collapse_key=f"circle_accepted:{request.id}",
        data={"event": "accepted", "request_id": str(request.id), "user_id": str(acceptor.id)},
        push_text=f"{acceptor.display_name} accepted your circle request",
        settings=settings,
    )
    logger.info("circle_request_accepted")


async def _awaiting_me(
    session: AsyncSession, *, user: User, request_id: uuid.UUID, settings: Settings, now: datetime
) -> CircleRequest:
    request = await session.scalar(
        select(CircleRequest)
        .where(CircleRequest.id == request_id, _awaiting(user.id, settings, now))
        .with_for_update()
    )
    if request is None or await users_service.is_blocked_between(
        session, user.id, request.requester_id
    ):
        raise _request_not_found()
    return request


async def accept_request(
    session: AsyncSession,
    *,
    user: User,
    request_id: uuid.UUID,
    settings: Settings,
    now: datetime | None = None,
) -> CircleRequest:
    moment = now or datetime.now(UTC)
    request = await _awaiting_me(
        session, user=user, request_id=request_id, settings=settings, now=moment
    )
    await _accept(session, request=request, acceptor=user, settings=settings, now=moment)
    return request


async def decline_request(
    session: AsyncSession,
    *,
    user: User,
    request_id: uuid.UUID,
    settings: Settings,
    now: datetime | None = None,
) -> None:
    """Silent: the sender is not told, and goes on seeing the request as pending
    until it would have expired."""
    moment = now or datetime.now(UTC)
    request = await _awaiting_me(
        session, user=user, request_id=request_id, settings=settings, now=moment
    )
    request.status = CircleRequestStatus.declined
    request.responded_at = moment
    await session.flush()


async def cancel_request(
    session: AsyncSession,
    *,
    user: User,
    request_id: uuid.UUID,
    settings: Settings,
    now: datetime | None = None,
) -> None:
    """Withdraw a request I sent. One the other person already declined stays
    declined, so its cooldown still holds; I only stop seeing it."""
    moment = now or datetime.now(UTC)
    request = await session.scalar(
        select(CircleRequest)
        .where(CircleRequest.id == request_id, _sender_sees_pending(user.id, settings, moment))
        .with_for_update()
    )
    if request is None:
        raise _request_not_found()
    if request.status == CircleRequestStatus.pending:
        request.status = CircleRequestStatus.cancelled
        request.responded_at = moment
    request.cancelled_at = moment
    await session.flush()


async def list_requests(
    session: AsyncSession,
    *,
    user: User,
    direction: Direction,
    params: PageParams,
    settings: Settings,
    now: datetime | None = None,
) -> tuple[list[CircleRequest], str | None]:
    """Requests waiting for me, or that I sent and still see as pending. Newest
    first. Anyone blocked either way is left out."""
    moment = now or datetime.now(UTC)
    blocked = await users_service.blocked_user_ids(session, user.id)
    statement: Select[CircleRequest]
    if direction == "incoming":
        statement = select(CircleRequest).where(_awaiting(user.id, settings, moment))
        if blocked:
            statement = statement.where(CircleRequest.requester_id.not_in(blocked))
    else:
        statement = select(CircleRequest).where(_sender_sees_pending(user.id, settings, moment))
        if blocked:
            statement = statement.where(CircleRequest.recipient_id.not_in(blocked))
    return await paginate(
        session,
        statement,
        sort_column=CircleRequest.created_at,
        id_column=CircleRequest.id,
        params=params,
    )


# ---------------------------------------------------------------------------
# The circle
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Member:
    user_id: uuid.UUID
    since: datetime


async def list_members(
    session: AsyncSession, *, user: User, params: PageParams
) -> tuple[list[Member], str | None]:
    """My circle, most recently added first.

    Keyset-paginated by hand on (since, other person's ID): a link has no ID of
    its own, and the other person's ID is unique within one circle.
    """
    other = links.other_member(user.id)
    statement = select(CircleLink.created_at, other.label("other_id")).where(
        links.involving(user.id)
    )
    if params.cursor:
        since, other_id = decode_cursor(params.cursor)
        statement = statement.where(tuple_(CircleLink.created_at, other) < tuple_(since, other_id))
    statement = statement.order_by(CircleLink.created_at.desc(), other.desc()).limit(
        params.limit + 1
    )
    rows = (await session.execute(statement)).all()
    members = [Member(user_id=row.other_id, since=row.created_at) for row in rows]
    if len(members) <= params.limit:
        return members, None
    page = members[: params.limit]
    return page, encode_cursor(page[-1].since, page[-1].user_id)


async def homes_within_feed(
    session: AsyncSession,
    redis: Redis,
    *,
    viewer: User,
    user_ids: list[uuid.UUID],
    settings: Settings,
) -> set[uuid.UUID]:
    """Which of these people live within the viewer's feed radius of home. Only
    they are shown by their area and a distance band; everyone else by their town
    (decision C.6). From home, never an explore location."""
    if not user_ids:
        return set()
    anchor = await resolve_anchor(redis, user=viewer, mode="home")
    if anchor is None:
        return set()
    radius = await RadiusEngine(session, redis, settings).radius_for(
        RadiusFeature.feed, anchor.cell_res8
    )
    return set(
        await session.scalars(
            select(User.id).where(
                User.id.in_(user_ids),
                bands.within(User.home_centroid, anchor, radius.radius_m),
            )
        )
    )


async def remove_member(session: AsyncSession, *, user: User, other_id: uuid.UUID) -> None:
    """Silent, and idempotent. A conversation already accepted stays accepted."""
    low, high = CircleLink.pair(user.id, other_id)
    await execute_dml(
        session,
        delete(CircleLink).where(CircleLink.user_low_id == low, CircleLink.user_high_id == high),
    )


# ---------------------------------------------------------------------------
# Workers, retention and data rights
# ---------------------------------------------------------------------------
async def expire_requests(
    session: AsyncSession, *, settings: Settings, now: datetime | None = None
) -> int:
    """Mark requests nobody answered as expired. Reads already treat them so; this
    keeps the table honest and starts their retention clock. Nobody is told."""
    moment = now or datetime.now(UTC)
    expired = await execute_dml(
        session,
        update(CircleRequest)
        .where(
            CircleRequest.status == CircleRequestStatus.pending,
            CircleRequest.created_at <= _expiry(settings, moment),
        )
        .values(status=CircleRequestStatus.expired)
        .execution_options(synchronize_session=False),
    )
    if expired:
        logger.info("circle_requests_expired", extra={"count": expired})
    return expired


async def purge_closed_requests(
    session: AsyncSession, *, settings: Settings, now: datetime | None = None
) -> int:
    """Delete request records `CIRCLE_REQUEST_RETENTION_DAYS` after they closed.

    Declined and expired ones by the brief's rule; accepted and cancelled ones too,
    since the link (or nothing) is all that needs remembering. Closing time is when
    it was answered or withdrawn, or for an expiry, when it expired. Every
    retention period is longer than the cooldown, which reads these rows.
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=settings.circle_request_retention_days)
    expiry = timedelta(days=settings.circle_request_expiry_days)
    closed_at = func.coalesce(CircleRequest.responded_at, CircleRequest.created_at + expiry)
    removed = await execute_dml(
        session,
        delete(CircleRequest).where(
            CircleRequest.status != CircleRequestStatus.pending,
            closed_at <= cutoff,
        ),
    )
    if removed:
        logger.info("circle_requests_purged", extra={"count": removed})
    return removed


async def purge_user_circle(session: AsyncSession, *, user_id: uuid.UUID) -> dict[str, int]:
    """Every link and request involving the person, both ways."""
    link_count = await execute_dml(session, delete(CircleLink).where(links.involving(user_id)))
    request_count = await execute_dml(
        session,
        delete(CircleRequest).where(
            or_(CircleRequest.requester_id == user_id, CircleRequest.recipient_id == user_id)
        ),
    )
    return {"circle_links": link_count, "circle_requests": request_count}


async def export_user_circle(
    session: AsyncSession, *, user: User, settings: Settings, now: datetime | None = None
) -> dict[str, Any]:
    """The circle, and the requests as the person sees them.

    A request they sent shows the status they were shown: pending until it
    would have expired, then expired. Telling them here that it was declined
    would undo the silent decline (decision C.2); the decline is the other
    person's data, and is in *their* export.
    """
    moment = now or datetime.now(UTC)
    links_rows = (
        await session.execute(
            select(links.other_member(user.id).label("other_id"), CircleLink.created_at)
            .where(links.involving(user.id))
            .order_by(CircleLink.created_at)
        )
    ).all()
    sent = (
        await session.execute(
            select(CircleRequest)
            .where(CircleRequest.requester_id == user.id)
            .order_by(CircleRequest.created_at)
        )
    ).scalars()
    received = (
        await session.execute(
            select(CircleRequest)
            .where(CircleRequest.recipient_id == user.id)
            .order_by(CircleRequest.created_at)
        )
    ).scalars()

    def as_sender_saw(request: CircleRequest) -> str:
        if request.status in (CircleRequestStatus.pending, CircleRequestStatus.declined):
            if request.cancelled_at is not None:
                return "cancelled"
            return "pending" if request.created_at > _expiry(settings, moment) else "expired"
        return str(request.status)

    return {
        "members": [
            {"user_id": str(row.other_id), "since": row.created_at.isoformat()}
            for row in links_rows
        ],
        "requests_sent": [
            {
                "to_user_id": str(request.recipient_id),
                "sent_at": request.created_at.isoformat(),
                "status": as_sender_saw(request),
            }
            for request in sent
        ],
        "requests_received": [
            {
                "from_user_id": str(request.requester_id),
                "received_at": request.created_at.isoformat(),
                "status": str(request.status),
                "answered_at": request.responded_at.isoformat() if request.responded_at else None,
            }
            for request in received
        ],
        "who_may_ask": str(user.circle_request_policy),
        "notes": (
            f"Closed requests are deleted {settings.circle_request_retention_days} days "
            "after they close."
        ),
    }
