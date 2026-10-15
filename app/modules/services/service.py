"""Service posts: offering, seeking and hiring.

The job-scam rules are here. Posting a job needs a trust threshold. A hiring post
that asks the job seeker for money — in English or Swahili — is *held*: visible
to its author only, its matched phrases recorded and audited, waiting for a
person. It is held rather than refused, because the same words appear in honest
posts ("we pay your registration fee"), and holding errs safe without turning
honest employers away. Editing the phrases out does not release a held post;
only a moderator does.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from redis.asyncio import Redis
from sqlalchemy import Select, func, literal, or_, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import RadiusFeature, Settings
from app.core.db import execute_dml
from app.core.errors import BadRequestError, ConflictError, ForbiddenError
from app.core.pagination import PageParams, decode_cursor, encode_cursor, paginate
from app.core.search import TEXT_SEARCH_CONFIG, matching_phrases, prefix_tsquery
from app.modules.location import bands, cells
from app.modules.location.radius import RadiusEngine
from app.modules.location.service import Anchor, AnchorMode, home_location_columns, resolve_anchor
from app.modules.messaging import service as messaging_service
from app.modules.messaging.models import Conversation, ConversationKind
from app.modules.moderation.audit import record_audit_event
from app.modules.moderation.models import AuditAction, AuditActorType, ContentStatus
from app.modules.services import access
from app.modules.services.models import JobType, PayPeriod, ServiceKind, ServicePost
from app.modules.users.models import User, UserStatus
from app.modules.users.service import blocked_user_ids

logger = logging.getLogger(__name__)

ServiceSort = Literal["relevance", "newest", "nearest"]
MAX_SEARCH_RESULTS = 50

# Fields only one kind may carry. Sent for another kind, they are a client bug
# worth telling about, not something to drop silently.
KIND_FIELDS: dict[ServiceKind, frozenset[str]] = {
    ServiceKind.offering: frozenset({"rate_info", "availability_info", "travel_km"}),
    ServiceKind.seeking: frozenset({"budget_info", "availability_info"}),
    ServiceKind.hiring: frozenset(
        {"job_type", "pay_min_kes", "pay_max_kes", "pay_period", "availability_info"}
    ),
}
ALL_KIND_FIELDS = frozenset().union(*KIND_FIELDS.values())


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
def can_hire(user: User, settings: Settings, now: datetime | None = None) -> bool:
    """The trust threshold for posting a job: a verified phone and an account old
    enough — or the verified-neighbour badge, which implies both and more."""
    if user.status != UserStatus.active:
        return False
    if user.is_verified_neighbour:
        return True
    if user.phone_verified_at is None:
        return False
    age = (now or datetime.now(UTC)) - user.created_at
    return age >= timedelta(days=settings.hiring_min_account_age_days)


def fee_demands(post: ServicePost, settings: Settings) -> list[str]:
    """Phrases in a hiring post that ask the job seeker for money."""
    if post.kind != ServiceKind.hiring:
        return []
    text = " ".join(part for part in (post.title, post.description, post.availability_info) if part)
    return matching_phrases(text, settings.job_scam_phrase_list)


def _clean(value: Any, *, field: str, max_length: int, required: bool = False) -> str | None:
    cleaned = (str(value) if value is not None else "").strip()
    if required and not cleaned:
        raise BadRequestError(f"A post needs a {field}")
    if len(cleaned) > max_length:
        raise BadRequestError(
            f"That {field} is too long", details={"field": field, "max_length": max_length}
        )
    return cleaned or None


def _apply_fields(post: ServicePost, fields: dict[str, Any], settings: Settings) -> None:
    """Set the fields a kind allows, refusing ones it does not."""
    stray = {name for name, value in fields.items() if value is not None} & (
        ALL_KIND_FIELDS - KIND_FIELDS[post.kind]
    )
    if stray:
        raise BadRequestError(
            f"Not used for a {post.kind.value} post", details={"fields": sorted(stray)}
        )
    detail = settings.service_detail_max_length
    if "title" in fields:
        post.title = (
            _clean(
                fields["title"],
                field="title",
                max_length=settings.service_title_max_length,
                required=True,
            )
            or ""
        )
    if "description" in fields:
        post.description = (
            _clean(
                fields["description"],
                field="description",
                max_length=settings.service_description_max_length,
            )
            or ""
        )
    if "category" in fields:
        if fields["category"] not in settings.service_category_list:
            raise BadRequestError(
                "That category does not exist",
                details={"categories": list(settings.service_category_list)},
            )
        post.category = fields["category"]
    for name in ("rate_info", "availability_info", "budget_info"):
        if name in fields:
            setattr(post, name, _clean(fields[name], field=name, max_length=detail))
    if "travel_km" in fields:
        travel = fields["travel_km"]
        if travel is not None and not 1 <= int(travel) <= settings.service_max_travel_km:
            raise BadRequestError(
                "That travel distance is out of range",
                details={"max_km": settings.service_max_travel_km},
            )
        post.travel_km = travel
    if "job_type" in fields:
        post.job_type = JobType(fields["job_type"]) if fields["job_type"] else None
    if "pay_period" in fields:
        post.pay_period = PayPeriod(fields["pay_period"]) if fields["pay_period"] else None
    for name in ("pay_min_kes", "pay_max_kes"):
        if name in fields:
            value = fields[name]
            if value is not None and value < 0:
                raise BadRequestError("Pay cannot be negative")
            setattr(post, name, value)
    if (
        post.pay_min_kes is not None
        and post.pay_max_kes is not None
        and post.pay_min_kes > post.pay_max_kes
    ):
        raise BadRequestError("The minimum pay is above the maximum")


async def _hold_if_suspicious(session: AsyncSession, post: ServicePost, settings: Settings) -> None:
    phrases = fee_demands(post, settings)
    if not phrases:
        return
    newly = post.status == ContentStatus.visible
    post.flagged_phrases = sorted(set(post.flagged_phrases) | set(phrases))
    if newly:
        post.status = ContentStatus.hidden_pending_review
        post.hidden_at = datetime.now(UTC)
    await session.flush()
    if newly:
        await record_audit_event(
            session,
            action=AuditAction.content_flagged,
            actor_type=AuditActorType.system,
            target_type="service_post",
            target_id=post.id,
            context={"check": "job_fee_demand", "phrases": phrases},
        )
        logger.info("service_post_held", extra={"phrases": len(phrases)})


# ---------------------------------------------------------------------------
# Creating and changing
# ---------------------------------------------------------------------------
async def create_post(
    session: AsyncSession,
    *,
    author: User,
    kind: ServiceKind,
    fields: dict[str, Any],
    settings: Settings,
) -> ServicePost:
    location = home_location_columns(author)
    if location is None:
        raise ConflictError(
            "Set your home area before posting",
            details={"action": "PUT /v1/me/location"},
        )
    if kind == ServiceKind.hiring and not can_hire(author, settings):
        raise ForbiddenError(
            "New accounts cannot post jobs yet",
            details={
                "reason": "hiring_trust_threshold",
                "min_account_age_days": settings.hiring_min_account_age_days,
            },
        )
    post = ServicePost(
        author_id=author.id,
        kind=kind,
        title="",
        description="",
        coarse_centroid=bands.coarse_wkt(int(location["cell_res8"])),  # type: ignore[call-overload]
        expires_at=(
            None
            if kind == ServiceKind.offering
            else datetime.now(UTC) + timedelta(days=settings.service_post_ttl_days)
        ),
        flagged_phrases=[],
        **location,
    )
    _apply_fields(post, fields, settings)
    try:
        async with session.begin_nested():
            session.add(post)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(
            select(ServicePost.id).where(
                ServicePost.author_id == author.id,
                ServicePost.kind == ServiceKind.offering,
                ServicePost.category == post.category,
                ServicePost.deleted_at.is_(None),
            )
        )
        raise ConflictError(
            "You already offer this service; edit that post instead",
            details={"reason": "duplicate_offering", "post_id": str(existing)},
        ) from None
    await _hold_if_suspicious(session, post, settings)
    await session.refresh(post)
    return post


async def update_post(
    session: AsyncSession,
    *,
    author: User,
    post_id: uuid.UUID,
    changes: dict[str, Any],
    settings: Settings,
) -> ServicePost:
    """Edit a post. Checked again for fee demands; a held post stays held."""
    post = await access.own_post(session, post_id=post_id, author_id=author.id)
    _apply_fields(post, changes, settings)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        raise ConflictError(
            "You already offer a service in that category",
            details={"reason": "duplicate_offering"},
        ) from None
    await _hold_if_suspicious(session, post, settings)
    await session.refresh(post)
    return post


async def delete_post(session: AsyncSession, *, author: User, post_id: uuid.UUID) -> None:
    post = await access.own_post(session, post_id=post_id, author_id=author.id)
    post.deleted_at = datetime.now(UTC)
    await session.flush()


async def set_open(
    session: AsyncSession,
    *,
    author: User,
    post_id: uuid.UUID,
    open_: bool,
    settings: Settings,
) -> ServicePost:
    """Close a post — the job is filled, the need met — or reopen it. Reopening a
    seeking or hiring post gives it a fresh term."""
    post = await access.own_post(session, post_id=post_id, author_id=author.id)
    now = datetime.now(UTC)
    if open_:
        post.closed_at = None
        if post.kind != ServiceKind.offering:
            post.expires_at = now + timedelta(days=settings.service_post_ttl_days)
    else:
        post.closed_at = post.closed_at or now
    await session.flush()
    await session.refresh(post)
    return post


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
async def get_post(session: AsyncSession, *, viewer: User, post_id: uuid.UUID) -> ServicePost:
    return await access.visible_post(session, post_id=post_id, viewer_id=viewer.id)


async def my_posts(
    session: AsyncSession, *, author: User, params: PageParams
) -> tuple[list[ServicePost], str | None]:
    statement = select(ServicePost).where(
        ServicePost.author_id == author.id,
        ServicePost.deleted_at.is_(None),
        ServicePost.status != ContentStatus.removed,
    )
    return await paginate(
        session,
        statement,
        sort_column=ServicePost.created_at,
        id_column=ServicePost.id,
        params=params,
    )


@dataclass(frozen=True, slots=True)
class BrowsePage:
    posts: list[ServicePost]
    buckets: dict[uuid.UUID, cells.DistanceBucket]
    next_cursor: str | None
    radius_km: float
    anchor: Anchor
    sort: ServiceSort


async def browse(
    session: AsyncSession,
    redis: Redis,
    *,
    viewer: User,
    kind: ServiceKind | None,
    category: str | None,
    query: str | None,
    sort: ServiceSort | None,
    distance_km: float | None,
    mode: AnchorMode,
    params: PageParams,
    settings: Settings,
) -> BrowsePage:
    """Open, visible posts within the services radius — three times the feed's —
    or the viewer's filter, up to its cap. Nobody blocked either way."""
    anchor = await resolve_anchor(redis, user=viewer, mode=mode)
    if anchor is None:
        raise ConflictError(
            "Set your home area to find services nearby",
            details={"action": "PUT /v1/me/location"},
        )
    engine = RadiusEngine(session, redis, settings)
    radius = await engine.radius_for(RadiusFeature.services, anchor.cell_res8)
    radius_km = engine.effective_radius_km(radius, distance_km)

    now = datetime.now(UTC)
    band = bands.band_expression(ServicePost.coarse_centroid, anchor).label("band")
    statement: Select[Any] = select(ServicePost, band).where(
        ServicePost.deleted_at.is_(None),
        ServicePost.status == ContentStatus.visible,
        ServicePost.closed_at.is_(None),
        or_(ServicePost.expires_at.is_(None), ServicePost.expires_at > now),
        func.ST_DWithin(
            ServicePost.centroid,
            func.ST_GeogFromText(anchor.centroid_wkt),
            int(radius_km * 1000),
        ),
    )
    hidden = await blocked_user_ids(session, viewer.id)
    if hidden:
        statement = statement.where(ServicePost.author_id.not_in(hidden))
    if kind is not None:
        statement = statement.where(ServicePost.kind == kind)
    if category is not None:
        if category not in settings.service_category_list:
            raise BadRequestError(
                "That category does not exist",
                details={"categories": list(settings.service_category_list)},
            )
        statement = statement.where(ServicePost.category == category)

    ts_query = prefix_tsquery(query) if query else None
    search = func.to_tsquery(TEXT_SEARCH_CONFIG, literal(ts_query)) if ts_query else None
    if search is not None:
        statement = statement.where(ServicePost.search_vector.op("@@")(search))
    chosen: ServiceSort = sort or ("relevance" if search is not None else "newest")
    if chosen == "relevance" and search is None:
        chosen = "newest"

    limit = params.limit
    next_cursor: str | None = None
    if chosen == "relevance":
        assert search is not None  # noqa: S101 — narrowed above
        rows = (
            await session.execute(
                statement.order_by(
                    func.ts_rank(ServicePost.search_vector, search).desc(),
                    ServicePost.created_at.desc(),
                    ServicePost.id.desc(),
                ).limit(min(limit, MAX_SEARCH_RESULTS))
            )
        ).all()
    elif chosen == "nearest":
        if params.cursor:
            statement = statement.where(
                bands.after_cursor(band, ServicePost.created_at, ServicePost.id, params.cursor)
            )
        rows = (
            await session.execute(
                statement.order_by(
                    *bands.ordering(band, ServicePost.created_at, ServicePost.id)
                ).limit(limit + 1)
            )
        ).all()
        if len(rows) > limit:
            rows = rows[:limit]
            last, last_band = rows[-1]
            next_cursor = bands.encode_cursor(int(last_band), last.created_at, last.id)
    else:
        if params.cursor:
            value, row_id = decode_cursor(params.cursor)
            statement = statement.where(
                tuple_(ServicePost.created_at, ServicePost.id)
                < tuple_(literal(value), literal(row_id))
            )
        rows = (
            await session.execute(
                statement.order_by(ServicePost.created_at.desc(), ServicePost.id.desc()).limit(
                    limit + 1
                )
            )
        ).all()
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1][0]
            next_cursor = encode_cursor(last.created_at, last.id)

    return BrowsePage(
        posts=[row[0] for row in rows],
        buckets={row[0].id: bands.bucket_for(int(row[1])) for row in rows},
        next_cursor=next_cursor,
        radius_km=radius_km,
        anchor=anchor,
        sort=chosen,
    )


async def open_chat(session: AsyncSession, *, contacter: User, post_id: uuid.UUID) -> Conversation:
    """Contact a post's author. Only while it is open; an existing conversation
    carries on after it closes."""
    post = await access.visible_post(session, post_id=post_id, viewer_id=contacter.id)
    if not post.is_open(datetime.now(UTC)):
        existing = await session.scalar(
            select(Conversation).where(
                Conversation.kind == ConversationKind.service,
                Conversation.service_post_id == post.id,
                Conversation.created_by_id == contacter.id,
            )
        )
        if existing is None:
            raise ConflictError("This post is closed")
        return existing
    return await messaging_service.open_service_chat(
        session, contacter=contacter, author_id=post.author_id, service_post_id=post.id
    )


async def my_conversations_about(
    session: AsyncSession, *, viewer: User, post_ids: list[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    if not post_ids:
        return {}
    rows = await session.execute(
        select(Conversation.service_post_id, Conversation.id).where(
            Conversation.kind == ConversationKind.service,
            Conversation.service_post_id.in_(post_ids),
            Conversation.created_by_id == viewer.id,
        )
    )
    return {post_id: conversation_id for post_id, conversation_id in rows if post_id}


# ---------------------------------------------------------------------------
# Data rights
# ---------------------------------------------------------------------------
async def purge_user_posts(session: AsyncSession, *, user_id: uuid.UUID) -> int:
    return await execute_dml(
        session,
        update(ServicePost)
        .where(ServicePost.author_id == user_id, ServicePost.deleted_at.is_(None))
        .values(deleted_at=datetime.now(UTC)),
    )


async def export_user_posts(session: AsyncSession, *, user_id: uuid.UUID) -> list[dict[str, Any]]:
    posts = (
        await session.execute(
            select(ServicePost)
            .where(ServicePost.author_id == user_id)
            .order_by(ServicePost.created_at)
        )
    ).scalars()

    def _when(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    return [
        {
            "id": str(post.id),
            "kind": str(post.kind),
            "category": post.category,
            "title": post.title,
            "description": post.description,
            "rate_info": post.rate_info,
            "availability_info": post.availability_info,
            "budget_info": post.budget_info,
            "job_type": str(post.job_type) if post.job_type else None,
            "pay_min_kes": post.pay_min_kes,
            "pay_max_kes": post.pay_max_kes,
            "status": str(post.status),
            "created_at": _when(post.created_at),
            "closed_at": _when(post.closed_at),
            "deleted_at": _when(post.deleted_at),
        }
        for post in posts
    ]
