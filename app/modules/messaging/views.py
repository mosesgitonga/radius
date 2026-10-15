"""Turning conversations and messages into responses, in batches.

An inbox page of twenty conversations needs memberships, the other person in each
direct chat, avatars, member counts and last messages. Looked up per row that is a
hundred-odd queries; batched it is six, whatever the page size.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, inspect, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.providers.storage import StorageProvider
from app.modules.feed.views import author_view, load_assets, load_users
from app.modules.media.views import image_variants
from app.modules.messaging.models import (
    Conversation,
    ConversationMember,
    ConversationState,
    MemberStatus,
    Message,
    MessageKind,
)
from app.modules.messaging.schemas import (
    ConversationOut,
    ConversationViewState,
    MemberOut,
    MessageOut,
    ReplyRefOut,
)
from app.modules.messaging.service import TWO_PARTY, decode_system_body
from app.modules.services.models import ServiceKind, ServicePost
from app.modules.users.models import User
from app.modules.users.service import blocked_user_ids


async def message_views(
    session: AsyncSession,
    storage: StorageProvider,
    *,
    messages: Sequence[Message],
    viewer: User,
) -> list[MessageOut]:
    if not messages:
        return []
    users = await load_users(
        session, (message.sender_id for message in messages if message.sender_id)
    )
    assets = await load_assets(
        session,
        [message.media_id for message in messages]
        + [user.avatar_media_id for user in users.values()],
    )
    reply_ids = {message.reply_to_id for message in messages if message.reply_to_id}
    reply_seqs: dict[uuid.UUID, int] = {}
    if reply_ids:
        reply_seqs = {
            row.id: row.seq
            for row in (
                await session.execute(
                    select(Message.id, Message.seq).where(Message.id.in_(reply_ids))
                )
            )
        }

    views: list[MessageOut] = []
    for message in messages:
        deleted = message.deleted_at is not None
        sender = users.get(message.sender_id) if message.sender_id else None
        reply_seq = reply_seqs.get(message.reply_to_id) if message.reply_to_id else None
        views.append(
            MessageOut(
                id=message.id,
                conversation_id=message.conversation_id,
                seq=message.seq,
                kind=message.kind,
                sender=author_view(sender, assets, storage) if sender else None,
                # A message under review keeps its content in the database for the
                # moderator; it is still never shown once deleted.
                body=None if deleted or message.kind == MessageKind.system else message.body,
                image=(
                    None
                    if deleted or message.media_id is None
                    else image_variants(assets.get(message.media_id), storage)
                ),
                reply_to=(
                    ReplyRefOut(id=message.reply_to_id, seq=reply_seq)
                    if message.reply_to_id and reply_seq is not None
                    else None
                ),
                system=(
                    decode_system_body(message.body) if message.kind == MessageKind.system else None
                ),
                deleted=deleted,
                created_at=message.created_at,
            )
        )
    return views


JOB_SAFETY_NOTICE = (
    "Never pay to get a job. Genuine employers do not ask for registration, "
    "training or application fees. Report anyone who does. / Usilipe pesa ili "
    "upate kazi. Waajiri halali hawaombi ada ya usajili, mafunzo au maombi."
)
"""Bilingual, because the scams it warns against arrive in both languages."""


async def _hiring_posts(
    session: AsyncSession, conversations: Sequence[Conversation]
) -> set[uuid.UUID]:
    ids = {c.service_post_id for c in conversations if c.service_post_id is not None}
    if not ids:
        return set()
    return set(
        (
            await session.execute(
                select(ServicePost.id).where(
                    ServicePost.id.in_(ids), ServicePost.kind == ServiceKind.hiring
                )
            )
        ).scalars()
    )


def _view_state(conversation: Conversation, viewer: User) -> ConversationViewState:
    if conversation.state == ConversationState.active:
        return "active"
    if conversation.request_sender_id == viewer.id:
        # Pending or declined: the sender cannot tell the difference.
        return "request_sent"
    if conversation.state == ConversationState.request_pending:
        return "request_received"
    return "declined"


async def conversation_views(
    session: AsyncSession,
    storage: StorageProvider,
    *,
    conversations: Sequence[Conversation],
    viewer: User,
) -> list[ConversationOut]:
    if not conversations:
        return []
    for conversation in conversations:
        # A conversation changed in this request has `updated_at` expired (it is
        # set by the database on update); reload it here, asynchronously, rather
        # than let attribute access attempt a synchronous load.
        if inspect(conversation).expired_attributes:
            await session.refresh(conversation)
    ids = [conversation.id for conversation in conversations]

    mine = {
        member.conversation_id: member
        for member in (
            await session.execute(
                select(ConversationMember).where(
                    ConversationMember.conversation_id.in_(ids),
                    ConversationMember.user_id == viewer.id,
                )
            )
        ).scalars()
    }
    # `.all()` matters: a Result has `.keys()`, so `dict()` would treat it as a
    # mapping rather than as pairs.
    counts: dict[uuid.UUID, int] = dict(
        (
            await session.execute(
                select(ConversationMember.conversation_id, func.count(ConversationMember.id))
                .where(
                    ConversationMember.conversation_id.in_(ids),
                    ConversationMember.status == MemberStatus.active,
                )
                .group_by(ConversationMember.conversation_id)
            )
        ).all()
    )

    direct_ids = [c.id for c in conversations if c.kind in TWO_PARTY]
    others: dict[uuid.UUID, ConversationMember] = {}
    if direct_ids:
        others = {
            member.conversation_id: member
            for member in (
                await session.execute(
                    select(ConversationMember).where(
                        ConversationMember.conversation_id.in_(direct_ids),
                        ConversationMember.user_id != viewer.id,
                    )
                )
            ).scalars()
        }

    with_messages = [(c.id, c.last_seq) for c in conversations if c.last_seq > 0]
    last_messages: dict[uuid.UUID, Message] = {}
    if with_messages:
        last_messages = {
            message.conversation_id: message
            for message in (
                await session.execute(
                    select(Message).where(
                        tuple_(Message.conversation_id, Message.seq).in_(with_messages)
                    )
                )
            ).scalars()
        }

    hiring = await _hiring_posts(session, conversations)

    hidden = await blocked_user_ids(session, viewer.id)
    visible_last = [
        message
        for conversation_id, message in last_messages.items()
        if (message.sender_id is None or message.sender_id not in hidden)
        and conversation_id in mine
        and message.seq > mine[conversation_id].visible_from_seq
    ]
    rendered = {
        view.conversation_id: view
        for view in await message_views(session, storage, messages=visible_last, viewer=viewer)
    }

    users = await load_users(session, (member.user_id for member in others.values()))
    assets = await load_assets(session, [user.avatar_media_id for user in users.values()])

    views: list[ConversationOut] = []
    for conversation in conversations:
        member = mine.get(conversation.id)
        if member is None:  # pragma: no cover — callers only pass the viewer's own
            continue
        other = others.get(conversation.id)
        other_user = users.get(other.user_id) if other else None
        seen_from = max(member.last_read_seq, member.visible_from_seq)
        views.append(
            ConversationOut(
                id=conversation.id,
                kind=conversation.kind,
                title=conversation.title,
                listing_id=conversation.listing_id,
                service_post_id=conversation.service_post_id,
                safety_notice=(
                    JOB_SAFETY_NOTICE if conversation.service_post_id in hiring else None
                ),
                state=_view_state(conversation, viewer),
                my_role=member.role,
                my_status=member.status,
                muted=member.muted,
                other_user=author_view(other_user, assets, storage) if other_user else None,
                other_last_read_seq=(
                    other.last_read_seq
                    if other is not None and conversation.state == ConversationState.active
                    else None
                ),
                member_count=int(counts.get(conversation.id, 0)),
                last_seq=conversation.last_seq,
                last_read_seq=member.last_read_seq,
                unread_count=(
                    max(conversation.last_seq - seen_from, 0)
                    if member.status == MemberStatus.active
                    else 0
                ),
                last_message=rendered.get(conversation.id),
                last_message_at=conversation.last_message_at,
                created_at=conversation.created_at,
                updated_at=conversation.updated_at,
            )
        )
    return views


async def member_views(
    session: AsyncSession,
    storage: StorageProvider,
    *,
    members: Sequence[ConversationMember],
) -> list[MemberOut]:
    users = await load_users(session, (member.user_id for member in members))
    assets = await load_assets(session, [user.avatar_media_id for user in users.values()])
    return [
        MemberOut(
            user=author_view(users[member.user_id], assets, storage),
            role=member.role,
            status=member.status,
            last_read_seq=member.last_read_seq,
        )
        for member in members
        if member.user_id in users
    ]
