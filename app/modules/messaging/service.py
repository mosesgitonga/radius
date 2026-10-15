"""Conversations and messages.

**Message requests.** A direct conversation starts as a request. Its first message
reaches the recipient's requests, not their inbox, and the sender may send nothing
more until the recipient replies — which accepts it — or accepts explicitly. A
declined request looks, to its sender, exactly like one that has not been answered:
the same state, the same refusal to send again. Telling someone they were declined
invites them to try another way, and the recipient said no to exactly that.

**Blocking.** No direct conversation, request, or message between a blocked pair,
in either direction, and every refusal is a 404 — the same answer as for a person
who does not exist. In a group chat they share, each is filtered out of the other's
history and delivery; leaving the group is not required to stop seeing someone.

**Persist, then publish.** Every realtime event and push is registered with
`after_commit`, so nothing is announced that a rollback could take back, and
recipients who fetch on the doorbell always find the message there.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import after_commit, execute_dml
from app.core.errors import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
)
from app.core.pagination import PageParams, paginate
from app.core.providers.push import PushProvider
from app.core.providers.storage import StorageProvider
from app.core.queue import get_queue
from app.core.ratelimit import get_rate_limiter
from app.core.realtime import publish_after_commit
from app.modules.auth.dependencies import is_new_account
from app.modules.media import service as media_service
from app.modules.media.models import MediaAsset, MediaPurpose
from app.modules.messaging.models import (
    Conversation,
    ConversationKind,
    ConversationMember,
    ConversationState,
    MemberRole,
    MemberStatus,
    Message,
    MessageKind,
    direct_key_for,
)
from app.modules.moderation import service as moderation_service
from app.modules.moderation.models import (
    Report,
    ReportReason,
    ReportStatus,
    ReportTargetType,
)
from app.modules.notifications import service as notifications_service
from app.modules.notifications.models import NotificationType
from app.modules.users import service as users_service
from app.modules.users.models import User, UserStatus
from app.modules.users.standing import is_restricted

logger = logging.getLogger(__name__)

MESSAGE_CREATED = "message.created"
MESSAGE_DELETED = "message.deleted"
CONVERSATION_UPDATED = "conversation.updated"
CONVERSATION_READ = "conversation.read"

MAX_MESSAGE_PAGE = 100
MAX_SYNC_CONVERSATIONS = 200

AWAITING_REPLY = "You can send one message until they reply"

TWO_PARTY: frozenset[ConversationKind] = frozenset(
    {ConversationKind.direct, ConversationKind.listing, ConversationKind.service}
)
ABOUT_SOMETHING: frozenset[ConversationKind] = frozenset(
    {ConversationKind.listing, ConversationKind.service}
)
"""Conversations about a listing or a service post. A first message in one of
these answers something the other person published, so it lands in their inbox
rather than their requests, under its own daily allowance."""
"""Conversations between exactly two people. The message-request rule and the
blocking rule — a block hides the whole conversation — apply to both."""


class SystemEvent:
    """What a system message records. The body is JSON naming the event and the
    people involved by ID, never by name: names change, and a purged account's name
    must not survive in somebody else's chat history."""

    created = "created"
    members_added = "members_added"
    member_joined = "member_joined"
    member_removed = "member_removed"
    member_left = "member_left"
    renamed = "renamed"
    role_changed = "role_changed"


# ---------------------------------------------------------------------------
# Loading and permission checks
# ---------------------------------------------------------------------------
def _conversation_not_found() -> NotFoundError:
    return NotFoundError("Conversation not found")


async def _contactable(session: AsyncSession, *, viewer: User, other_id: uuid.UUID) -> User:
    """Someone the viewer may start talking to, or a 404 that does not say why."""
    other = await users_service.get_user(session, other_id)
    if (
        other is None
        or other.deleted_at is not None
        or other.status in (UserStatus.banned, UserStatus.deletion_pending)
        or await users_service.is_blocked_between(session, viewer.id, other_id)
    ):
        raise NotFoundError("User not found")
    return other


async def _load(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    lock: bool = False,
    statuses: Sequence[MemberStatus] = (MemberStatus.active,),
) -> tuple[Conversation, ConversationMember]:
    """A conversation and the caller's membership of it, or 404.

    `lock` takes the conversation's row lock and holds it to commit. Everything
    that allocates a sequence number or changes membership takes it first, so
    those changes are serialised per conversation and ordered as they commit.
    """
    statement = select(Conversation).where(Conversation.id == conversation_id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    conversation = (await session.execute(statement)).scalar_one_or_none()
    if conversation is None:
        raise _conversation_not_found()

    member = (
        await session.execute(
            select(ConversationMember)
            .where(
                ConversationMember.conversation_id == conversation_id,
                ConversationMember.user_id == user_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if member is None or member.status not in statuses:
        raise _conversation_not_found()

    if conversation.kind in TWO_PARTY:
        other_id = await _other_direct_member(session, conversation, user_id)
        if other_id is not None and await users_service.is_blocked_between(
            session, user_id, other_id
        ):
            raise _conversation_not_found()
    return conversation, member


async def _other_direct_member(
    session: AsyncSession, conversation: Conversation, user_id: uuid.UUID
) -> uuid.UUID | None:
    return await session.scalar(
        select(ConversationMember.user_id).where(
            ConversationMember.conversation_id == conversation.id,
            ConversationMember.user_id != user_id,
        )
    )


async def _active_member_ids(session: AsyncSession, conversation_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        (
            await session.execute(
                select(ConversationMember.user_id).where(
                    ConversationMember.conversation_id == conversation_id,
                    ConversationMember.status == MemberStatus.active,
                )
            )
        )
        .scalars()
        .all()
    )


async def audience(
    session: AsyncSession, conversation_id: uuid.UUID, *, actor_id: uuid.UUID | None
) -> list[uuid.UUID]:
    """Who hears about something in a conversation: its active members, less
    anyone either blocked by or blocking the person who did it."""
    members = await _active_member_ids(session, conversation_id)
    if actor_id is None:
        return members
    hidden = await users_service.blocked_user_ids(session, actor_id)
    return [member for member in members if member not in hidden]


def _require_admin(member: ConversationMember) -> None:
    if member.role not in (MemberRole.owner, MemberRole.admin):
        raise ForbiddenError("Only the chat's admins can do that")


async def has_accepted_conversation(
    session: AsyncSession, first: uuid.UUID, second: uuid.UUID
) -> bool:
    """Whether two people have a direct conversation that was accepted.

    The test for "do they know each other" used by group invitations, and in
    Phase 6 by reviews.
    """
    state = await session.scalar(
        select(Conversation.state).where(Conversation.direct_key == direct_key_for(first, second))
    )
    return state == ConversationState.active


# ---------------------------------------------------------------------------
# Sequence numbers and system messages
# ---------------------------------------------------------------------------
def _allocate_seq(conversation: Conversation) -> int:
    """The next sequence number. The caller holds the conversation's row lock
    (see `_load(lock=True)`), which is what makes this safe."""
    conversation.last_seq = conversation.last_seq + 1
    conversation.last_message_at = datetime.now(UTC)
    return conversation.last_seq


async def _append_system_message(
    session: AsyncSession, conversation: Conversation, event: str, **fields: Any
) -> Message:
    message = Message(
        conversation_id=conversation.id,
        sender_id=None,
        seq=_allocate_seq(conversation),
        kind=MessageKind.system,
        body=json.dumps({"event": event, **fields}, default=str, separators=(",", ":")),
    )
    session.add(message)
    await session.flush()
    await _announce_message(session, conversation, message, actor_id=None)
    return message


def decode_system_body(body: str | None) -> dict[str, Any] | None:
    if not body:
        return None
    try:
        decoded = json.loads(body)
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


async def _announce_message(
    session: AsyncSession,
    conversation: Conversation,
    message: Message,
    *,
    actor_id: uuid.UUID | None,
) -> None:
    """Tell the conversation's members a message exists, after commit.

    The event carries the conversation and sequence number, not the message: a
    client fetches messages after the last seq it holds, which also fills any gap
    left by a doorbell it missed. One code path for live delivery and for sync.

    Recipients are resolved inside the transaction, under the conversation lock
    the caller holds, so they are exactly the members at the moment of sending.
    """
    await session.flush()
    publish_after_commit(
        session,
        await audience(session, conversation.id, actor_id=actor_id),
        MESSAGE_CREATED,
        {
            "conversation_id": str(conversation.id),
            "seq": message.seq,
            "message_id": str(message.id),
        },
    )


def _announce_conversation(
    session: AsyncSession,
    user_ids: Iterable[uuid.UUID],
    conversation: Conversation,
) -> None:
    publish_after_commit(
        session, user_ids, CONVERSATION_UPDATED, {"conversation_id": str(conversation.id)}
    )


# ---------------------------------------------------------------------------
# Direct conversations and requests
# ---------------------------------------------------------------------------
async def open_direct(session: AsyncSession, *, user: User, other_id: uuid.UUID) -> Conversation:
    """The one direct conversation between two people, created if needed.

    Creating it sends nothing: an empty conversation is invisible to the other
    person until a message is sent. Two people opening a conversation with each
    other at the same moment get the same row, because `direct_key` is unique.
    """
    if other_id == user.id:
        raise BadRequestError("You cannot message yourself")
    await _contactable(session, viewer=user, other_id=other_id)

    key = direct_key_for(user.id, other_id)
    existing = await _find_direct(session, key)
    if existing is not None:
        return existing

    conversation = Conversation(
        kind=ConversationKind.direct,
        state=ConversationState.request_pending,
        direct_key=key,
        created_by_id=user.id,
        request_sender_id=user.id,
    )
    try:
        async with session.begin_nested():
            session.add(conversation)
            await session.flush()
            session.add_all(
                [
                    ConversationMember(conversation_id=conversation.id, user_id=member_id)
                    for member_id in (user.id, other_id)
                ]
            )
            await session.flush()
    except IntegrityError:
        # Both tapped "message" at once; the other insert won. Use theirs.
        found = await _find_direct(session, key)
        if found is None:  # pragma: no cover — the unique index guarantees a row
            raise
        return found
    return conversation


async def open_listing_chat(
    session: AsyncSession,
    *,
    buyer: User,
    seller_id: uuid.UUID,
    listing_id: uuid.UUID,
) -> Conversation:
    """The buyer's one conversation with a seller about one listing.

    The caller (the marketplace) has already checked the listing may be seen.
    Like a direct conversation it starts as a request: the buyer can send one
    message until the seller replies, and the reply is what makes it accepted.
    """
    if seller_id == buyer.id:
        raise BadRequestError("You cannot ask about your own listing")
    await _contactable(session, viewer=buyer, other_id=seller_id)
    existing = await _find_listing_chat(session, listing_id=listing_id, buyer_id=buyer.id)
    if existing is not None:
        return existing

    conversation = Conversation(
        kind=ConversationKind.listing,
        state=ConversationState.request_pending,
        listing_id=listing_id,
        created_by_id=buyer.id,
        request_sender_id=buyer.id,
    )
    try:
        async with session.begin_nested():
            session.add(conversation)
            await session.flush()
            session.add_all(
                [
                    ConversationMember(conversation_id=conversation.id, user_id=member_id)
                    for member_id in (buyer.id, seller_id)
                ]
            )
            await session.flush()
    except IntegrityError:
        found = await _find_listing_chat(session, listing_id=listing_id, buyer_id=buyer.id)
        if found is None:  # pragma: no cover — the unique index guarantees a row
            raise
        return found
    return conversation


async def open_service_chat(
    session: AsyncSession,
    *,
    contacter: User,
    author_id: uuid.UUID,
    service_post_id: uuid.UUID,
) -> Conversation:
    """The contacter's one conversation with a post's author about that post.

    The services module has already checked the post may be seen and is open. The
    request rule applies, as for listings; the author's reply accepts it, which is
    what a review later requires.
    """
    if author_id == contacter.id:
        raise BadRequestError("You cannot contact yourself about your own post")
    await _contactable(session, viewer=contacter, other_id=author_id)
    existing = await _find_service_chat(
        session, service_post_id=service_post_id, contacter_id=contacter.id
    )
    if existing is not None:
        return existing
    conversation = Conversation(
        kind=ConversationKind.service,
        state=ConversationState.request_pending,
        service_post_id=service_post_id,
        created_by_id=contacter.id,
        request_sender_id=contacter.id,
    )
    try:
        async with session.begin_nested():
            session.add(conversation)
            await session.flush()
            session.add_all(
                [
                    ConversationMember(conversation_id=conversation.id, user_id=member_id)
                    for member_id in (contacter.id, author_id)
                ]
            )
            await session.flush()
    except IntegrityError:
        found = await _find_service_chat(
            session, service_post_id=service_post_id, contacter_id=contacter.id
        )
        if found is None:  # pragma: no cover — the unique index guarantees a row
            raise
        return found
    return conversation


async def _find_service_chat(
    session: AsyncSession, *, service_post_id: uuid.UUID, contacter_id: uuid.UUID
) -> Conversation | None:
    return (
        await session.execute(
            select(Conversation).where(
                Conversation.kind == ConversationKind.service,
                Conversation.service_post_id == service_post_id,
                Conversation.created_by_id == contacter_id,
            )
        )
    ).scalar_one_or_none()


async def accepted_conversation_about(
    session: AsyncSession,
    *,
    first: uuid.UUID,
    second: uuid.UUID,
    listing_id: uuid.UUID | None = None,
    service_post_id: uuid.UUID | None = None,
) -> Conversation | None:
    """An accepted conversation between two people about one listing or service
    post — the condition for either to review the other in that context."""
    statement = select(Conversation).where(
        Conversation.state == ConversationState.active,
        Conversation.id.in_(
            select(ConversationMember.conversation_id).where(ConversationMember.user_id == first)
        ),
        Conversation.id.in_(
            select(ConversationMember.conversation_id).where(ConversationMember.user_id == second)
        ),
    )
    if listing_id is not None:
        statement = statement.where(
            Conversation.kind == ConversationKind.listing, Conversation.listing_id == listing_id
        )
    elif service_post_id is not None:
        statement = statement.where(
            Conversation.kind == ConversationKind.service,
            Conversation.service_post_id == service_post_id,
        )
    else:  # pragma: no cover — callers pass one
        return None
    return (await session.execute(statement.limit(1))).scalar_one_or_none()


async def _find_listing_chat(
    session: AsyncSession, *, listing_id: uuid.UUID, buyer_id: uuid.UUID
) -> Conversation | None:
    return (
        await session.execute(
            select(Conversation).where(
                Conversation.kind == ConversationKind.listing,
                Conversation.listing_id == listing_id,
                Conversation.created_by_id == buyer_id,
            )
        )
    ).scalar_one_or_none()


async def listing_buyers(
    session: AsyncSession, *, listing_id: uuid.UUID, seller_id: uuid.UUID
) -> list[tuple[uuid.UUID, uuid.UUID, ConversationState]]:
    """(buyer, conversation, state) for everyone who asked about a listing and sent
    something. For the seller, to choose who it was sold to."""
    rows = await session.execute(
        select(Conversation.created_by_id, Conversation.id, Conversation.state)
        .join(ConversationMember, ConversationMember.conversation_id == Conversation.id)
        .where(
            Conversation.kind == ConversationKind.listing,
            Conversation.listing_id == listing_id,
            Conversation.last_seq > 0,
            ConversationMember.user_id == seller_id,
        )
        .order_by(Conversation.last_message_at.desc())
    )
    return [(buyer, conversation_id, state) for buyer, conversation_id, state in rows if buyer]


async def _find_direct(session: AsyncSession, key: str) -> Conversation | None:
    return (
        await session.execute(select(Conversation).where(Conversation.direct_key == key))
    ).scalar_one_or_none()


async def _check_request_allowance(
    user: User, settings: Settings, *, kind: ConversationKind
) -> None:
    """The daily cap on new message requests, tighter for new accounts.

    Checked here rather than as a route dependency because only the service knows
    whether a given send is a request. Asking about a listing has its own, larger
    allowance: reaching sellers is what the marketplace is for.

    A restricted account starts no new conversations at all. It can still reply in
    the ones it has.
    """
    if is_restricted(user):
        raise ForbiddenError(
            "Your account is restricted and cannot start new conversations for now",
            details={"reason": "restricted"},
        )
    new = is_new_account(user, settings)
    if kind == ConversationKind.service:
        bucket = "service_inquiry"
        limit = (
            settings.new_account_service_inquiries_per_day
            if new
            else settings.service_inquiries_per_day
        )
    elif kind == ConversationKind.listing:
        bucket = "listing_inquiry"
        limit = (
            settings.new_account_listing_inquiries_per_day
            if new
            else settings.listing_inquiries_per_day
        )
    else:
        bucket = "message_request"
        limit = (
            settings.new_account_message_requests_per_day
            if new
            else settings.message_requests_per_day
        )
    result = await get_rate_limiter().hit(
        bucket, f"u:{user.id}", limit=limit, window_seconds=86_400
    )
    if not result.allowed:
        logger.info("rate_limited", extra={"bucket": bucket, "scope": "user"})
        raise RateLimitedError(retry_after_seconds=result.retry_after_seconds)


async def respond_to_request(
    session: AsyncSession, *, user: User, conversation_id: uuid.UUID, accept: bool
) -> Conversation:
    """Accept or decline a request addressed to the caller.

    Only the recipient can do either. A declined request can still be accepted
    later by its recipient — changing one's mind is allowed in that direction — but
    never reopened by its sender.
    """
    conversation, _member = await _load(
        session, conversation_id=conversation_id, user_id=user.id, lock=True
    )
    if (
        conversation.kind not in TWO_PARTY
        or conversation.state == ConversationState.active
        or conversation.request_sender_id == user.id
        or conversation.last_seq == 0
    ):
        raise NotFoundError("There is no pending request in that conversation")

    if accept:
        conversation.state = ConversationState.active
        others = [
            member
            for member in await _active_member_ids(session, conversation.id)
            if member != user.id
        ]
        # Both sides learn of an acceptance.
        _announce_conversation(session, [user.id, *others], conversation)
    else:
        conversation.state = ConversationState.request_declined
        # Only the recipient's own devices learn of a decline. See the module
        # docstring for why the sender is not told.
        _announce_conversation(session, [user.id], conversation)
    await session.flush()
    return conversation


async def block_and_report(
    session: AsyncSession,
    *,
    user: User,
    conversation_id: uuid.UUID,
    reason: ReportReason,
    details: str | None,
    settings: Settings,
) -> None:
    """Decline a request, report what was sent, and block the sender.

    In that order: once the block exists, the sender's message is invisible to the
    caller, and reporting something you cannot see is refused by design.
    """
    conversation, _member = await _load(
        session, conversation_id=conversation_id, user_id=user.id, lock=True
    )
    sender_id = conversation.request_sender_id
    if (
        conversation.kind not in TWO_PARTY
        or conversation.state == ConversationState.active
        or sender_id is None
        or sender_id == user.id
        or conversation.last_seq == 0
    ):
        raise NotFoundError("There is no pending request in that conversation")

    first_message_id = await session.scalar(
        select(Message.id)
        .where(Message.conversation_id == conversation.id, Message.sender_id == sender_id)
        .order_by(Message.seq.asc())
        .limit(1)
    )
    conversation.state = ConversationState.request_declined
    await session.flush()

    if first_message_id is not None:
        await moderation_service.submit_report(
            session,
            reporter=user,
            target_type=ReportTargetType.message,
            target_id=first_message_id,
            reason=reason,
            details=details,
            settings=settings,
        )
    await users_service.block_user(session, blocker=user, blocked_id=sender_id)
    _announce_conversation(session, [user.id], conversation)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
async def send_message(
    session: AsyncSession,
    *,
    sender: User,
    conversation_id: uuid.UUID,
    body: str | None,
    media_id: uuid.UUID | None,
    reply_to_id: uuid.UUID | None,
    settings: Settings,
) -> Message:
    """Persist a message, then announce it and schedule pushes after commit."""
    text = body.strip() if body is not None else None
    if not text:
        text = None
    if (text is None) == (media_id is None):
        raise BadRequestError("A message has either text or an image")
    if text is not None and len(text) > settings.message_max_length:
        raise BadRequestError(
            "That message is too long", details={"max_length": settings.message_max_length}
        )

    conversation, member = await _load(
        session, conversation_id=conversation_id, user_id=sender.id, lock=True
    )

    is_request = False
    recipient_id: uuid.UUID | None = None
    if conversation.kind in TWO_PARTY:
        recipient_id = await _other_direct_member(session, conversation, sender.id)
        if recipient_id is None:  # pragma: no cover — two-party conversations have two members
            raise _conversation_not_found()
        await _contactable(session, viewer=sender, other_id=recipient_id)

        if conversation.state != ConversationState.active:
            if conversation.last_seq == 0:
                # Whoever sends first is the one asking — not whoever happened to
                # open the empty conversation.
                conversation.request_sender_id = sender.id
                conversation.state = ConversationState.request_pending
                is_request = True
            elif conversation.request_sender_id == sender.id:
                # Pending and declined look identical from this side, on purpose.
                raise ConflictError(AWAITING_REPLY, details={"reason": "awaiting_reply"})
            else:
                # The recipient replying is the acceptance.
                conversation.state = ConversationState.active
                _announce_conversation(session, [sender.id, recipient_id], conversation)

    if is_request:
        await _check_request_allowance(sender, settings, kind=conversation.kind)

    asset: MediaAsset | None = None
    if media_id is not None:
        (asset,) = await media_service.claim_assets_for(
            session, media_ids=[media_id], owner_id=sender.id, purpose=MediaPurpose.message
        )

    if reply_to_id is not None:
        await _visible_message(session, viewer=sender, member=member, message_id=reply_to_id)
        reply_conversation = await session.scalar(
            select(Message.conversation_id).where(Message.id == reply_to_id)
        )
        if reply_conversation != conversation.id:
            raise NotFoundError("Message not found")

    message = Message(
        conversation_id=conversation.id,
        sender_id=sender.id,
        seq=_allocate_seq(conversation),
        kind=MessageKind.image if asset is not None else MessageKind.text,
        body=text,
        media_id=asset.id if asset is not None else None,
        reply_to_id=reply_to_id,
    )
    session.add(message)
    # Your own message is, by definition, read.
    member.last_read_seq = message.seq
    await session.flush()

    await _announce_message(session, conversation, message, actor_id=sender.id)

    if is_request and recipient_id is not None:
        data = {"conversation_id": str(conversation.id)}
        if conversation.listing_id is not None:
            data["listing_id"] = str(conversation.listing_id)
        if conversation.service_post_id is not None:
            data["service_post_id"] = str(conversation.service_post_id)
        await notifications_service.notify(
            session,
            recipient_id=recipient_id,
            kind={
                ConversationKind.listing: NotificationType.listing_inquiry,
                ConversationKind.service: NotificationType.service_inquiry,
            }.get(conversation.kind, NotificationType.message_request),
            actor_id=sender.id,
            target_type="conversation",
            target_id=conversation.id,
            collapse_key=f"message_request:{conversation.id}",
            data=data,
            settings=settings,
        )
    elif conversation.state == ConversationState.active:
        message_id = str(message.id)

        async def _enqueue_push() -> None:
            await get_queue().enqueue("deliver_message_push", message_id)

        after_commit(session, _enqueue_push)

    logger.info(
        "message_sent",
        extra={"conversation_kind": str(conversation.kind), "kind": str(message.kind)},
    )
    return message


async def _visible_message(
    session: AsyncSession,
    *,
    viewer: User,
    member: ConversationMember,
    message_id: uuid.UUID,
) -> Message:
    message = await session.get(Message, message_id)
    if (
        message is None
        or message.conversation_id != member.conversation_id
        or message.seq <= member.visible_from_seq
    ):
        raise NotFoundError("Message not found")
    if message.sender_id is not None and await users_service.is_blocked_between(
        session, viewer.id, message.sender_id
    ):
        raise NotFoundError("Message not found")
    return message


async def delete_message(
    session: AsyncSession,
    storage: StorageProvider,
    *,
    user: User,
    message_id: uuid.UUID,
) -> Message:
    """Delete one's own message for everyone: it becomes "message deleted".

    The content is cleared, not just hidden — the sender asked for it to be gone.
    The exception is a message under a pending report: a sender must not be able
    to destroy the evidence against them by deleting it, so its content is kept for
    the moderator, and still shown to nobody.
    """
    message = await session.get(Message, message_id)
    if message is None or message.sender_id != user.id or message.kind == MessageKind.system:
        raise NotFoundError("Message not found")
    conversation, _member = await _load(
        session,
        conversation_id=message.conversation_id,
        user_id=user.id,
        lock=True,
        statuses=(MemberStatus.active, MemberStatus.left, MemberStatus.removed),
    )
    if message.deleted_at is not None:
        return message

    message.deleted_at = datetime.now(UTC)
    if not await _under_review(session, message.id):
        await _erase_content(session, storage, message)
    # Bumped so `GET /v1/sync` lists the conversation and clients refetch.
    conversation.updated_at = datetime.now(UTC)
    await session.flush()

    publish_after_commit(
        session,
        await audience(session, conversation.id, actor_id=user.id),
        MESSAGE_DELETED,
        {
            "conversation_id": str(conversation.id),
            "seq": message.seq,
            "message_id": str(message.id),
        },
    )
    return message


async def remove_message_for_moderation(session: AsyncSession, *, message_id: uuid.UUID) -> bool:
    """A moderator takes a reported message down. It reads as "message deleted"
    to the conversation; its content is kept, as evidence, until retention."""
    message = await session.get(Message, message_id)
    if message is None or message.deleted_at is not None:
        return False
    conversation = await session.get(Conversation, message.conversation_id, with_for_update=True)
    if conversation is None:  # pragma: no cover — the message cascades with it
        return False
    message.deleted_at = datetime.now(UTC)
    conversation.updated_at = datetime.now(UTC)
    await session.flush()
    publish_after_commit(
        session,
        await audience(session, conversation.id, actor_id=None),
        MESSAGE_DELETED,
        {
            "conversation_id": str(conversation.id),
            "seq": message.seq,
            "message_id": str(message.id),
        },
    )
    return True


async def _under_review(session: AsyncSession, message_id: uuid.UUID) -> bool:
    return bool(
        await session.scalar(
            select(
                exists().where(
                    Report.target_type == ReportTargetType.message,
                    Report.target_id == message_id,
                    Report.status == ReportStatus.pending,
                )
            )
        )
    )


async def _erase_content(session: AsyncSession, storage: StorageProvider, message: Message) -> None:
    message.body = None
    if message.media_id is None:
        return
    asset = await session.get(MediaAsset, message.media_id)
    if asset is None:
        return
    keys = media_service.strip_asset(
        asset, code="deleted", reason="The message this was sent in was deleted"
    )

    async def _delete_objects() -> None:
        for key in keys:
            await storage.delete_object(storage_key=key)

    # After commit: deleting the objects first and then rolling back would leave
    # a live message pointing at an image that no longer exists.
    after_commit(session, _delete_objects)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MessagePage:
    messages: list[Message]
    has_more: bool


async def list_messages(
    session: AsyncSession,
    *,
    viewer: User,
    conversation_id: uuid.UUID,
    after_seq: int | None,
    before_seq: int | None,
    limit: int,
) -> MessagePage:
    """Messages in sequence order.

    `after_seq` is sync: everything after the last message the client holds,
    oldest first. `before_seq` is scrolling back. Neither gives the latest page.
    """
    if after_seq is not None and before_seq is not None:
        raise BadRequestError("Use after_seq or before_seq, not both")
    limit = max(1, min(limit, MAX_MESSAGE_PAGE))

    _conversation, member = await _load(session, conversation_id=conversation_id, user_id=viewer.id)
    statement = select(Message).where(
        Message.conversation_id == conversation_id,
        Message.seq > member.visible_from_seq,
    )
    hidden = await users_service.blocked_user_ids(session, viewer.id)
    if hidden:
        statement = statement.where(
            or_(Message.sender_id.is_(None), Message.sender_id.not_in(hidden))
        )

    if after_seq is not None:
        statement = statement.where(Message.seq > after_seq).order_by(Message.seq.asc())
        rows = list((await session.execute(statement.limit(limit + 1))).scalars().all())
        return MessagePage(messages=rows[:limit], has_more=len(rows) > limit)

    if before_seq is not None:
        statement = statement.where(Message.seq < before_seq)
    rows = list(
        (await session.execute(statement.order_by(Message.seq.desc()).limit(limit + 1)))
        .scalars()
        .all()
    )
    page = rows[:limit]
    page.reverse()
    return MessagePage(messages=page, has_more=len(rows) > limit)


async def messages_changed_since(
    session: AsyncSession,
    *,
    viewer: User,
    conversation_id: uuid.UUID,
    since: datetime,
    settings: Settings,
) -> list[Message]:
    """Messages edited after a moment — today, only deletions.

    Sync by sequence finds new messages but cannot notice that an old one was
    deleted; this does, for a reconnecting client.
    """
    _conversation, member = await _load(session, conversation_id=conversation_id, user_id=viewer.id)
    cutoff = since - timedelta(seconds=settings.sync_overlap_seconds)
    rows = (
        await session.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.seq > member.visible_from_seq,
                Message.deleted_at.is_not(None),
                Message.updated_at > cutoff,
            )
            .order_by(Message.seq.asc())
            .limit(MAX_MESSAGE_PAGE * 5)
        )
    ).scalars()
    return list(rows)


async def get_conversation(
    session: AsyncSession, *, viewer: User, conversation_id: uuid.UUID
) -> tuple[Conversation, ConversationMember]:
    return await _load(
        session,
        conversation_id=conversation_id,
        user_id=viewer.id,
        statuses=(MemberStatus.active, MemberStatus.invited),
    )


InboxFolder = str
"""`chats`, `requests`, or `invitations`."""


async def list_conversations(
    session: AsyncSession, *, viewer: User, folder: InboxFolder, params: PageParams
) -> tuple[list[Conversation], str | None]:
    """The inbox, the requests folder, or pending group invitations.

    * **chats** — accepted conversations and groups, plus requests the caller sent
      (shown to them as waiting).
    * **requests** — requests addressed to the caller that have a message.
    * **invitations** — group chats the caller was added to by a stranger.

    Direct conversations with someone blocked appear in none of them.
    """
    mine = select(ConversationMember.conversation_id).where(ConversationMember.user_id == viewer.id)
    statement = select(Conversation)

    if folder == "invitations":
        statement = statement.where(
            Conversation.id.in_(mine.where(ConversationMember.status == MemberStatus.invited))
        )
    else:
        statement = statement.where(
            Conversation.id.in_(mine.where(ConversationMember.status == MemberStatus.active))
        )
        if folder == "requests":
            statement = statement.where(
                Conversation.kind == ConversationKind.direct,
                Conversation.state == ConversationState.request_pending,
                Conversation.request_sender_id != viewer.id,
                Conversation.last_seq > 0,
            )
        elif folder == "chats":
            # Listing inquiries go straight to the seller's inbox: they answer the
            # seller's own public listing, so they are not unsolicited.
            statement = statement.where(
                or_(
                    Conversation.state == ConversationState.active,
                    Conversation.request_sender_id == viewer.id,
                    Conversation.kind.in_(ABOUT_SOMETHING)
                    & (Conversation.last_seq > 0)
                    & (Conversation.state == ConversationState.request_pending),
                )
            )
        else:
            raise BadRequestError("Unknown folder", details={"folder": folder})

    hidden = await users_service.blocked_user_ids(session, viewer.id)
    if hidden:
        blocked_member = select(ConversationMember.conversation_id).where(
            ConversationMember.user_id.in_(hidden)
        )
        statement = statement.where(
            or_(
                Conversation.kind.not_in(TWO_PARTY),
                Conversation.id.not_in(blocked_member),
            )
        )

    return await paginate(
        session,
        statement,
        sort_column=Conversation.updated_at,
        id_column=Conversation.id,
        params=params,
    )


@dataclass(frozen=True, slots=True)
class SyncResult:
    conversations: list[Conversation]
    next_since: datetime
    has_more: bool


async def sync(
    session: AsyncSession, *, viewer: User, since: datetime | None, settings: Settings
) -> SyncResult:
    """Conversations that changed since a moment, oldest change first.

    A conversation is listed when it, or any membership of it, changed — so a new
    message, a rename, someone joining, and the other person reading all show up.
    The client then fetches messages after the last seq it holds for each. The
    lookback overlap re-lists a few conversations harmlessly, and catches changes
    whose commit landed after a later timestamp had already been read.
    """
    started = datetime.now(UTC)
    statement = select(Conversation).where(
        Conversation.id.in_(
            select(ConversationMember.conversation_id).where(
                ConversationMember.user_id == viewer.id,
                ConversationMember.status.in_((MemberStatus.active, MemberStatus.invited)),
            )
        )
    )
    if since is not None:
        cutoff = since - timedelta(seconds=settings.sync_overlap_seconds)
        member_changed = select(ConversationMember.conversation_id).where(
            ConversationMember.updated_at > cutoff
        )
        statement = statement.where(
            or_(Conversation.updated_at > cutoff, Conversation.id.in_(member_changed))
        )
    rows = list(
        (
            await session.execute(
                statement.order_by(Conversation.updated_at.asc(), Conversation.id.asc()).limit(
                    MAX_SYNC_CONVERSATIONS + 1
                )
            )
        )
        .scalars()
        .all()
    )
    has_more = len(rows) > MAX_SYNC_CONVERSATIONS
    page = rows[:MAX_SYNC_CONVERSATIONS]
    # When truncated, resume from the last one listed; the overlap re-lists it.
    next_since = page[-1].updated_at if has_more else started

    hidden = await users_service.blocked_user_ids(session, viewer.id)
    if hidden and page:
        blocked_direct = set(
            (
                await session.execute(
                    select(ConversationMember.conversation_id).where(
                        ConversationMember.conversation_id.in_([c.id for c in page]),
                        ConversationMember.user_id.in_(hidden),
                    )
                )
            ).scalars()
        )
        page = [
            conversation
            for conversation in page
            if conversation.kind not in TWO_PARTY or conversation.id not in blocked_direct
        ]
    return SyncResult(conversations=page, next_since=next_since, has_more=has_more)


async def mark_read(
    session: AsyncSession, *, user: User, conversation_id: uuid.UUID, seq: int
) -> ConversationMember:
    """Move the caller's read mark forward. It never moves back.

    Read receipts are published to the other members only once a conversation
    is accepted: the sender of a request does not learn it was seen.
    """
    conversation, member = await _load(session, conversation_id=conversation_id, user_id=user.id)
    target = min(max(seq, 0), conversation.last_seq)
    if target <= member.last_read_seq:
        return member
    member.last_read_seq = target
    await session.flush()

    recipients = (
        await audience(session, conversation.id, actor_id=user.id)
        if conversation.state == ConversationState.active
        else [user.id]
    )
    publish_after_commit(
        session,
        recipients,
        CONVERSATION_READ,
        {"conversation_id": str(conversation.id), "user_id": str(user.id), "seq": target},
    )
    return member


async def set_muted(
    session: AsyncSession, *, user: User, conversation_id: uuid.UUID, muted: bool
) -> ConversationMember:
    _conversation, member = await _load(session, conversation_id=conversation_id, user_id=user.id)
    member.muted = muted
    await session.flush()
    return member


# ---------------------------------------------------------------------------
# Group chats
# ---------------------------------------------------------------------------
def _clean_title(title: str, settings: Settings) -> str:
    cleaned = " ".join(title.split())
    if not cleaned:
        raise BadRequestError("A group chat needs a name")
    if len(cleaned) > settings.group_chat_title_max_length:
        raise BadRequestError(
            "That name is too long",
            details={"max_length": settings.group_chat_title_max_length},
        )
    return cleaned


async def _member_count(session: AsyncSession, conversation_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count(ConversationMember.id)).where(
                ConversationMember.conversation_id == conversation_id,
                ConversationMember.status.in_((MemberStatus.active, MemberStatus.invited)),
            )
        )
        or 0
    )


async def create_group(
    session: AsyncSession,
    *,
    creator: User,
    title: str,
    member_ids: Sequence[uuid.UUID],
    settings: Settings,
) -> Conversation:
    """A group chat, with the creator as owner.

    Each person added joins outright if they have an accepted conversation with
    the creator, and is otherwise invited — see `MemberStatus.invited`.
    """
    cleaned = _clean_title(title, settings)
    wanted = list(dict.fromkeys(member_id for member_id in member_ids if member_id != creator.id))
    if not wanted:
        raise BadRequestError("Add at least one other person")
    if len(wanted) + 1 > settings.group_chat_member_limit:
        raise BadRequestError(
            "Too many members", details={"member_limit": settings.group_chat_member_limit}
        )
    for member_id in wanted:
        await _contactable(session, viewer=creator, other_id=member_id)

    conversation = Conversation(
        kind=ConversationKind.group,
        state=ConversationState.active,
        title=cleaned,
        created_by_id=creator.id,
    )
    session.add(conversation)
    await session.flush()
    session.add(
        ConversationMember(
            conversation_id=conversation.id, user_id=creator.id, role=MemberRole.owner
        )
    )
    invited = await _add_people(session, conversation, actor=creator, user_ids=wanted)
    await session.flush()

    await _append_system_message(
        session, conversation, SystemEvent.created, actor_id=creator.id, title=cleaned
    )
    joined = [member_id for member_id in wanted if member_id not in invited]
    if joined:
        await _append_system_message(
            session,
            conversation,
            SystemEvent.members_added,
            actor_id=creator.id,
            user_ids=[str(member_id) for member_id in joined],
        )
    await _invite_notifications(
        session, conversation, actor=creator, invited=invited, settings=settings
    )
    _announce_conversation(session, [creator.id, *joined], conversation)
    return conversation


async def _add_people(
    session: AsyncSession,
    conversation: Conversation,
    *,
    actor: User,
    user_ids: Sequence[uuid.UUID],
) -> set[uuid.UUID]:
    """Add or re-add people. Returns those who were invited rather than joined.

    Somebody joining later sees messages only from the point they joined.
    """
    existing = {
        member.user_id: member
        for member in (
            await session.execute(
                select(ConversationMember).where(
                    ConversationMember.conversation_id == conversation.id,
                    ConversationMember.user_id.in_(user_ids),
                )
            )
        ).scalars()
    }
    invited: set[uuid.UUID] = set()
    for user_id in user_ids:
        status = (
            MemberStatus.active
            if await has_accepted_conversation(session, actor.id, user_id)
            else MemberStatus.invited
        )
        member = existing.get(user_id)
        if member is not None and member.status in (MemberStatus.active, MemberStatus.invited):
            continue
        if member is None:
            member = ConversationMember(conversation_id=conversation.id, user_id=user_id)
            session.add(member)
        member.status = status
        member.role = MemberRole.member
        member.invited_by_id = actor.id
        member.visible_from_seq = conversation.last_seq
        if status == MemberStatus.invited:
            invited.add(user_id)
    return invited


async def _invite_notifications(
    session: AsyncSession,
    conversation: Conversation,
    *,
    actor: User,
    invited: Iterable[uuid.UUID],
    settings: Settings,
) -> None:
    for user_id in invited:
        await notifications_service.notify(
            session,
            recipient_id=user_id,
            kind=NotificationType.chat_invite,
            actor_id=actor.id,
            target_type="conversation",
            target_id=conversation.id,
            collapse_key=f"chat_invite:{conversation.id}",
            data={"conversation_id": str(conversation.id)},
            settings=settings,
        )


async def _load_group(
    session: AsyncSession,
    *,
    user: User,
    conversation_id: uuid.UUID,
    statuses: Sequence[MemberStatus] = (MemberStatus.active,),
) -> tuple[Conversation, ConversationMember]:
    conversation, member = await _load(
        session, conversation_id=conversation_id, user_id=user.id, lock=True, statuses=statuses
    )
    if conversation.kind != ConversationKind.group:
        raise BadRequestError("That is not a group chat")
    if conversation.group_id is not None:
        # A group's linked chat follows the group: who is in it, and what it is
        # called, are decided there.
        raise BadRequestError(
            "This chat belongs to a group; manage it from the group",
            details={"group_id": str(conversation.group_id)},
        )
    return conversation, member


# ---------------------------------------------------------------------------
# A group's linked chat
# ---------------------------------------------------------------------------
async def create_linked_chat(
    session: AsyncSession,
    *,
    group_id: uuid.UUID,
    title: str,
    owner_id: uuid.UUID,
    member_ids: Sequence[uuid.UUID],
) -> Conversation:
    """A group chat whose membership mirrors a group's.

    No invitations and no member limit: everyone in it is already in the group,
    which is the consent the invitation rule exists to get. Everyone sees the
    whole history, since the chat starts empty.
    """
    conversation = Conversation(
        kind=ConversationKind.group,
        state=ConversationState.active,
        title=title[:200],
        created_by_id=owner_id,
        group_id=group_id,
    )
    session.add(conversation)
    await session.flush()
    session.add_all(
        ConversationMember(
            conversation_id=conversation.id,
            user_id=member_id,
            role=MemberRole.owner if member_id == owner_id else MemberRole.member,
        )
        for member_id in dict.fromkeys(member_ids)
    )
    await session.flush()
    await _append_system_message(session, conversation, SystemEvent.created, actor_id=owner_id)
    return conversation


async def set_linked_membership(
    session: AsyncSession, *, conversation_id: uuid.UUID, user_id: uuid.UUID, active: bool
) -> None:
    """Add someone to a group's chat as they join the group, or take them out as
    they leave, are removed or are banned. Joining starts their history there."""
    conversation = (
        await session.execute(
            select(Conversation)
            .where(Conversation.id == conversation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if conversation is None:
        return
    member = await _membership_of(session, conversation.id, user_id)
    if active:
        if member is not None and member.status == MemberStatus.active:
            return
        if member is None:
            member = ConversationMember(conversation_id=conversation.id, user_id=user_id)
            session.add(member)
        member.status = MemberStatus.active
        member.role = MemberRole.member
        member.visible_from_seq = conversation.last_seq
        await session.flush()
        await _append_system_message(
            session, conversation, SystemEvent.member_joined, user_id=str(user_id)
        )
    else:
        if member is None or member.status != MemberStatus.active:
            return
        member.status = MemberStatus.removed
        await session.flush()
        await _append_system_message(
            session, conversation, SystemEvent.member_left, user_id=str(user_id)
        )
    _announce_conversation(session, [user_id], conversation)


async def rename_linked_chat(
    session: AsyncSession, *, conversation_id: uuid.UUID, title: str
) -> None:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is not None and conversation.title != title:
        conversation.title = title[:200]
        await session.flush()


async def add_members(
    session: AsyncSession,
    *,
    actor: User,
    conversation_id: uuid.UUID,
    member_ids: Sequence[uuid.UUID],
    settings: Settings,
) -> Conversation:
    conversation, member = await _load_group(session, user=actor, conversation_id=conversation_id)
    _require_admin(member)
    wanted = list(dict.fromkeys(member_id for member_id in member_ids if member_id != actor.id))
    if not wanted:
        raise BadRequestError("Add at least one person")
    for member_id in wanted:
        await _contactable(session, viewer=actor, other_id=member_id)

    already = set(
        (
            await session.execute(
                select(ConversationMember.user_id).where(
                    ConversationMember.conversation_id == conversation.id,
                    ConversationMember.user_id.in_(wanted),
                    ConversationMember.status.in_((MemberStatus.active, MemberStatus.invited)),
                )
            )
        ).scalars()
    )
    new = [member_id for member_id in wanted if member_id not in already]
    if not new:
        return conversation
    if await _member_count(session, conversation.id) + len(new) > settings.group_chat_member_limit:
        raise BadRequestError(
            "That would take the chat over its member limit",
            details={"member_limit": settings.group_chat_member_limit},
        )

    invited = await _add_people(session, conversation, actor=actor, user_ids=new)
    await session.flush()
    joined = [member_id for member_id in new if member_id not in invited]
    if joined:
        await _append_system_message(
            session,
            conversation,
            SystemEvent.members_added,
            actor_id=actor.id,
            user_ids=[str(member_id) for member_id in joined],
        )
    await _invite_notifications(
        session, conversation, actor=actor, invited=invited, settings=settings
    )
    _announce_conversation(session, joined, conversation)
    return conversation


async def remove_member(
    session: AsyncSession, *, actor: User, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """Admins remove members; only the owner removes admins; nobody removes the owner."""
    if user_id == actor.id:
        raise BadRequestError("Leave the chat instead")
    conversation, member = await _load_group(session, user=actor, conversation_id=conversation_id)
    _require_admin(member)
    target = await _membership_of(session, conversation.id, user_id)
    if target is None or target.status not in (MemberStatus.active, MemberStatus.invited):
        raise NotFoundError("That person is not in this chat")
    if target.role == MemberRole.owner:
        raise ForbiddenError("The owner cannot be removed")
    if target.role == MemberRole.admin and member.role != MemberRole.owner:
        raise ForbiddenError("Only the owner can remove an admin")

    was_active = target.status == MemberStatus.active
    target.status = MemberStatus.removed
    target.role = MemberRole.member
    await session.flush()
    if was_active:
        await _append_system_message(
            session,
            conversation,
            SystemEvent.member_removed,
            actor_id=actor.id,
            user_id=str(user_id),
        )
    _announce_conversation(session, [user_id], conversation)


async def _membership_of(
    session: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> ConversationMember | None:
    return (
        await session.execute(
            select(ConversationMember).where(
                ConversationMember.conversation_id == conversation_id,
                ConversationMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


async def set_role(
    session: AsyncSession,
    *,
    actor: User,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    role: MemberRole,
) -> None:
    """The owner promotes and demotes admins, or hands ownership over.

    Handing over ownership makes the previous owner an admin, so a chat always has
    exactly one owner.
    """
    conversation, member = await _load_group(session, user=actor, conversation_id=conversation_id)
    if member.role != MemberRole.owner:
        raise ForbiddenError("Only the owner can change roles")
    if user_id == actor.id:
        raise BadRequestError("Hand ownership to someone else instead")
    target = await _membership_of(session, conversation.id, user_id)
    if target is None or target.status != MemberStatus.active:
        raise NotFoundError("That person is not in this chat")
    if target.role == role:
        return

    target.role = role
    if role == MemberRole.owner:
        member.role = MemberRole.admin
    await session.flush()
    await _append_system_message(
        session,
        conversation,
        SystemEvent.role_changed,
        actor_id=actor.id,
        user_id=str(user_id),
        role=role.value,
    )


async def rename_group(
    session: AsyncSession,
    *,
    actor: User,
    conversation_id: uuid.UUID,
    title: str,
    settings: Settings,
) -> Conversation:
    conversation, member = await _load_group(session, user=actor, conversation_id=conversation_id)
    _require_admin(member)
    cleaned = _clean_title(title, settings)
    if cleaned == conversation.title:
        return conversation
    conversation.title = cleaned
    await session.flush()
    await _append_system_message(
        session, conversation, SystemEvent.renamed, actor_id=actor.id, title=cleaned
    )
    return conversation


async def leave_group(session: AsyncSession, *, user: User, conversation_id: uuid.UUID) -> None:
    """Leave a group chat. An owner leaving hands the chat on rather than orphaning it."""
    conversation, member = await _load_group(session, user=user, conversation_id=conversation_id)
    was_owner = member.role == MemberRole.owner
    member.status = MemberStatus.left
    member.role = MemberRole.member
    await session.flush()
    if was_owner:
        await _promote_successor(session, conversation.id)
    await _append_system_message(
        session, conversation, SystemEvent.member_left, user_id=str(user.id)
    )
    _announce_conversation(session, [user.id], conversation)


async def _promote_successor(session: AsyncSession, conversation_id: uuid.UUID) -> None:
    """The longest-standing admin, or failing that the longest-standing member."""
    successor = (
        await session.execute(
            select(ConversationMember)
            .where(
                ConversationMember.conversation_id == conversation_id,
                ConversationMember.status == MemberStatus.active,
            )
            .order_by(
                (ConversationMember.role == MemberRole.admin).desc(),
                ConversationMember.created_at.asc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if successor is not None:
        successor.role = MemberRole.owner
        await session.flush()


async def respond_to_invite(
    session: AsyncSession, *, user: User, conversation_id: uuid.UUID, accept: bool
) -> Conversation:
    """Join a group chat one was invited to, or decline it.

    Joining starts the member's history at that moment: they were not there for
    what came before.
    """
    conversation, member = await _load_group(
        session, user=user, conversation_id=conversation_id, statuses=(MemberStatus.invited,)
    )
    if not accept:
        member.status = MemberStatus.left
        await session.flush()
        _announce_conversation(session, [user.id], conversation)
        return conversation

    member.status = MemberStatus.active
    member.visible_from_seq = conversation.last_seq
    await session.flush()
    await _append_system_message(
        session, conversation, SystemEvent.member_joined, user_id=str(user.id)
    )
    _announce_conversation(session, [user.id], conversation)
    return conversation


async def list_members(
    session: AsyncSession, *, viewer: User, conversation_id: uuid.UUID
) -> list[ConversationMember]:
    """Current members, less anyone blocked either way."""
    await _load(
        session,
        conversation_id=conversation_id,
        user_id=viewer.id,
        statuses=(MemberStatus.active, MemberStatus.invited),
    )
    hidden = await users_service.blocked_user_ids(session, viewer.id)
    statement = (
        select(ConversationMember)
        .where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.status.in_((MemberStatus.active, MemberStatus.invited)),
        )
        .order_by(ConversationMember.created_at.asc())
    )
    if hidden:
        statement = statement.where(ConversationMember.user_id.not_in(hidden))
    return list((await session.execute(statement)).scalars().all())


# ---------------------------------------------------------------------------
# Push (worker)
# ---------------------------------------------------------------------------
async def deliver_message_push(
    session: AsyncSession,
    redis: Redis,
    provider: PushProvider,
    *,
    message_id: uuid.UUID,
    settings: Settings,
) -> dict[str, int]:
    """Push a message to every member who is offline, has not muted the chat, and
    is not blocked with the sender. Requests are notified through `notify` instead."""
    message = await session.get(Message, message_id)
    if message is None or message.deleted_at is not None or message.sender_id is None:
        return {"sent": 0, "skipped": 0}
    conversation = await session.get(Conversation, message.conversation_id)
    sender = await session.get(User, message.sender_id)
    if conversation is None or sender is None or conversation.state != ConversationState.active:
        return {"sent": 0, "skipped": 0}

    hidden = await users_service.blocked_user_ids(session, sender.id)
    recipients = (
        await session.execute(
            select(ConversationMember.user_id).where(
                ConversationMember.conversation_id == conversation.id,
                ConversationMember.status == MemberStatus.active,
                ConversationMember.muted.is_(False),
                ConversationMember.user_id != sender.id,
            )
        )
    ).scalars()

    is_group = conversation.kind == ConversationKind.group
    title = (conversation.title or "Group chat") if is_group else sender.display_name
    body = f"New message from {sender.display_name}" if is_group else "New message"
    if message.kind == MessageKind.image:
        preview = f"{sender.display_name} sent a photo" if is_group else "Sent a photo"
    else:
        preview = f"{sender.display_name}: {message.body}" if is_group else (message.body or "")

    sent = skipped = 0
    for recipient_id in recipients:
        if recipient_id in hidden:
            skipped += 1
            continue
        outcome = await notifications_service.push_to_user(
            session,
            redis,
            provider,
            user_id=recipient_id,
            kind=NotificationType.message,
            title=title,
            body=body,
            preview=preview,
            collapse_key=f"conversation:{conversation.id}",
            data={"conversation_id": str(conversation.id), "seq": str(message.seq)},
            settings=settings,
        )
        if outcome.sent:
            sent += 1
        else:
            skipped += 1
    return {"sent": sent, "skipped": skipped}


# ---------------------------------------------------------------------------
# Data rights
# ---------------------------------------------------------------------------
async def purge_user_messages(
    session: AsyncSession, storage: StorageProvider | None, *, user_id: uuid.UUID
) -> dict[str, int]:
    """Erase what a purged account said, and take them out of every chat.

    Their messages stay as "message deleted" placeholders so other people's
    conversations keep their shape and numbering, with the content cleared —
    except under a pending report, where it is kept for the moderator, as with a
    deletion. They leave every chat; a group they owned passes to someone else.
    """
    now = datetime.now(UTC)
    messages = list(
        (
            await session.execute(
                select(Message).where(
                    Message.sender_id == user_id,
                    Message.kind != MessageKind.system,
                )
            )
        )
        .scalars()
        .all()
    )
    reported = (
        set(
            (
                await session.execute(
                    select(Report.target_id).where(
                        Report.target_type == ReportTargetType.message,
                        Report.status == ReportStatus.pending,
                        Report.target_id.in_([message.id for message in messages]),
                    )
                )
            ).scalars()
        )
        if messages
        else set()
    )
    for message in messages:
        if message.deleted_at is None:
            message.deleted_at = now
        if message.id in reported:
            continue
        message.body = None
        if storage is not None:
            await _erase_content(session, storage, message)

    owned = list(
        (
            await session.execute(
                select(ConversationMember.conversation_id).where(
                    ConversationMember.user_id == user_id,
                    ConversationMember.role == MemberRole.owner,
                    ConversationMember.status == MemberStatus.active,
                )
            )
        )
        .scalars()
        .all()
    )
    left = await execute_dml(
        session,
        update(ConversationMember)
        .where(
            ConversationMember.user_id == user_id,
            ConversationMember.status.in_((MemberStatus.active, MemberStatus.invited)),
        )
        .values(status=MemberStatus.left, role=MemberRole.member, updated_at=now),
    )
    for conversation_id in owned:
        await _promote_successor(session, conversation_id)
    await session.flush()
    return {"messages": len(messages), "memberships_left": left}


async def export_user_messages(session: AsyncSession, *, user_id: uuid.UUID) -> dict[str, Any]:
    """The conversations a person is in and every message they sent.

    Messages *received* are not included: they are the other person's words, and
    handing them to whoever requests an export would make the access right a way
    to extract someone else's data. The export says so.
    """
    memberships = (
        await session.execute(
            select(ConversationMember, Conversation)
            .join(Conversation, Conversation.id == ConversationMember.conversation_id)
            .where(ConversationMember.user_id == user_id)
            .order_by(ConversationMember.created_at.asc())
        )
    ).all()
    sent = (
        await session.execute(
            select(Message).where(Message.sender_id == user_id).order_by(Message.created_at.asc())
        )
    ).scalars()

    def _when(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    return {
        "conversations": [
            {
                "id": str(conversation.id),
                "kind": str(conversation.kind),
                "title": conversation.title,
                "role": str(member.role),
                "status": str(member.status),
                "muted": member.muted,
                "joined_at": _when(member.created_at),
            }
            for member, conversation in memberships
        ],
        "messages_sent": [
            {
                "id": str(message.id),
                "conversation_id": str(message.conversation_id),
                "seq": message.seq,
                "kind": str(message.kind),
                "body": message.body,
                "image_media_id": str(message.media_id) if message.media_id else None,
                "sent_at": _when(message.created_at),
                "deleted_at": _when(message.deleted_at),
            }
            for message in sent
        ],
        "notes": (
            "Messages other people sent you are not included: they are their "
            "words, not yours. You can read them in the app."
        ),
    }
