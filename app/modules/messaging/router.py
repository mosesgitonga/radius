"""Messaging endpoints: conversations, requests, group chats, messages, sync.

The realtime socket is in `socket.py`; it only rings the doorbell. Everything a
client shows is fetched from here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import SessionDep
from app.core.errors import ErrorEnvelope
from app.core.idempotency import IDEMPOTENCY_HEADER, execute_idempotently
from app.core.pagination import PageParams, page_params
from app.core.providers.storage import StorageProvider, get_storage_provider
from app.modules.auth.dependencies import ActiveUser, CurrentUser, tiered_user_rate_limit
from app.modules.messaging import service as messaging_service
from app.modules.messaging import views
from app.modules.messaging.models import Conversation
from app.modules.messaging.schemas import (
    BlockAndReportIn,
    ConversationOut,
    ConversationPageOut,
    DirectOpenIn,
    GroupCreateIn,
    GroupRenameIn,
    MemberOut,
    MembersAddIn,
    MessageCreateIn,
    MessageOut,
    MessagePageOut,
    MuteIn,
    ReadIn,
    RequestResponseIn,
    RoleIn,
    SyncOut,
)
from app.modules.users.models import User

router = APIRouter(tags=["messaging"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
StorageDep = Annotated[StorageProvider, Depends(get_storage_provider)]
PageDep = Annotated[PageParams, Depends(page_params)]

_message_limit = tiered_user_rate_limit(
    bucket="send_message",
    window_seconds=60,
    new_account_limit=lambda s: s.new_account_messages_per_minute,
    established_limit=lambda s: s.messages_per_minute,
)

NOT_FOUND_DOC = (
    "\n\nEvery refusal to show a conversation is a 404 — not a member, never "
    "existed, or the other person is blocked look the same."
)


async def _one(
    session: AsyncSession, storage: StorageProvider, conversation: Conversation, viewer: User
) -> ConversationOut:
    (rendered,) = await views.conversation_views(
        session, storage, conversations=[conversation], viewer=viewer
    )
    return rendered


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------
@router.post(
    "/conversations/direct",
    response_model=ConversationOut,
    summary="Open a direct conversation",
    description=(
        "Returns the one direct conversation with this person, creating it if "
        "needed. Creating it sends nothing.\n\n"
        "If the two of you have no accepted conversation, the first message sent "
        "becomes a **message request**: it lands in the recipient's requests, and "
        "the sender can send nothing more until they reply or accept. A blocked "
        "person, either way round, is a 404."
    ),
)
async def open_direct(
    payload: DirectOpenIn,
    user: ActiveUser,
    session: SessionDep,
    storage: StorageDep,
) -> ConversationOut:
    conversation = await messaging_service.open_direct(session, user=user, other_id=payload.user_id)
    return await _one(session, storage, conversation, user)


@router.post(
    "/conversations/groups",
    response_model=ConversationOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a group chat",
    description=(
        "The creator becomes the owner. People who have an accepted conversation "
        "with the creator join at once; anyone else is **invited**, and sees "
        "nothing of the chat until they accept. The member limit is configurable."
    ),
)
async def create_group(
    payload: GroupCreateIn,
    user: ActiveUser,
    session: SessionDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> ConversationOut:
    conversation = await messaging_service.create_group(
        session,
        creator=user,
        title=payload.title,
        member_ids=payload.member_ids,
        settings=settings,
    )
    return await _one(session, storage, conversation, user)


@router.get(
    "/conversations",
    response_model=ConversationPageOut,
    summary="List conversations",
    description=(
        "`chats` is the inbox: accepted conversations, group chats, and requests "
        "the caller sent. `requests` holds requests addressed to the caller. "
        "`invitations` holds group chats the caller has been invited to. Most "
        "recently active first."
    ),
)
async def list_conversations(
    user: CurrentUser,
    session: SessionDep,
    storage: StorageDep,
    page: PageDep,
    folder: Annotated[Literal["chats", "requests", "invitations"], Query()] = "chats",
) -> ConversationPageOut:
    conversations, next_cursor = await messaging_service.list_conversations(
        session, viewer=user, folder=folder, params=page
    )
    return ConversationPageOut(
        items=await views.conversation_views(
            session, storage, conversations=conversations, viewer=user
        ),
        next_cursor=next_cursor,
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationOut,
    summary="Get a conversation",
    description="Members and invitees only." + NOT_FOUND_DOC,
)
async def get_conversation(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    storage: StorageDep,
) -> ConversationOut:
    conversation, _member = await messaging_service.get_conversation(
        session, viewer=user, conversation_id=conversation_id
    )
    return await _one(session, storage, conversation, user)


@router.patch(
    "/conversations/{conversation_id}",
    response_model=ConversationOut,
    summary="Rename a group chat",
    description="Owner and admins only.",
)
async def rename_group(
    conversation_id: uuid.UUID,
    payload: GroupRenameIn,
    user: ActiveUser,
    session: SessionDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> ConversationOut:
    conversation = await messaging_service.rename_group(
        session, actor=user, conversation_id=conversation_id, title=payload.title, settings=settings
    )
    return await _one(session, storage, conversation, user)


@router.post(
    "/conversations/{conversation_id}/request",
    response_model=ConversationOut,
    summary="Accept or decline a message request",
    description=(
        "Recipient only. Replying also accepts. The sender is never told about a "
        "decline: to them, the request simply stays unanswered."
    ),
)
async def respond_to_request(
    conversation_id: uuid.UUID,
    payload: RequestResponseIn,
    user: CurrentUser,
    session: SessionDep,
    storage: StorageDep,
) -> ConversationOut:
    conversation = await messaging_service.respond_to_request(
        session, user=user, conversation_id=conversation_id, accept=payload.action == "accept"
    )
    return await _one(session, storage, conversation, user)


@router.post(
    "/conversations/{conversation_id}/block-and-report",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Decline a request, report it, and block the sender",
    description=(
        "For the request nobody should have sent. Files a report against its first "
        "message, so a moderator sees what was said, then blocks the sender — in "
        "that order, because a blocked person's messages cannot be reported."
    ),
)
async def block_and_report(
    conversation_id: uuid.UUID,
    payload: BlockAndReportIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> None:
    await messaging_service.block_and_report(
        session,
        user=user,
        conversation_id=conversation_id,
        reason=payload.reason,
        details=payload.details,
        settings=settings,
    )


@router.post(
    "/conversations/{conversation_id}/invitation",
    response_model=ConversationOut,
    summary="Accept or decline a group chat invitation",
    description=(
        "On accepting, the caller sees messages from that point on, not before. "
        "Declining returns the chat with `my_status: left`."
    ),
)
async def respond_to_invite(
    conversation_id: uuid.UUID,
    payload: RequestResponseIn,
    user: ActiveUser,
    session: SessionDep,
    storage: StorageDep,
) -> ConversationOut:
    conversation = await messaging_service.respond_to_invite(
        session, user=user, conversation_id=conversation_id, accept=payload.action == "accept"
    )
    return await _one(session, storage, conversation, user)


@router.post(
    "/conversations/{conversation_id}/leave",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Leave a group chat",
    description="An owner who leaves hands the chat to its longest-standing admin, or member.",
)
async def leave_group(conversation_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    await messaging_service.leave_group(session, user=user, conversation_id=conversation_id)


@router.put(
    "/conversations/{conversation_id}/mute",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mute or unmute a conversation",
    description="Muted conversations still deliver in realtime, but never push.",
)
async def set_muted(
    conversation_id: uuid.UUID, payload: MuteIn, user: CurrentUser, session: SessionDep
) -> None:
    await messaging_service.set_muted(
        session, user=user, conversation_id=conversation_id, muted=payload.muted
    )


@router.post(
    "/conversations/{conversation_id}/read",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark a conversation read up to a sequence number",
    description=(
        "Read state is one number per member. It only moves forward. The other "
        "members see it once the conversation is accepted."
    ),
)
async def mark_read(
    conversation_id: uuid.UUID, payload: ReadIn, user: CurrentUser, session: SessionDep
) -> None:
    await messaging_service.mark_read(
        session, user=user, conversation_id=conversation_id, seq=payload.seq
    )


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------
@router.get(
    "/conversations/{conversation_id}/members",
    response_model=list[MemberOut],
    summary="List the members of a conversation",
    description="Anyone blocked either way is left out.",
)
async def list_members(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    storage: StorageDep,
) -> list[MemberOut]:
    members = await messaging_service.list_members(
        session, viewer=user, conversation_id=conversation_id
    )
    return await views.member_views(session, storage, members=members)


@router.post(
    "/conversations/{conversation_id}/members",
    response_model=ConversationOut,
    summary="Add people to a group chat",
    description="Owner and admins only. The same join-or-invite rule as creating a chat.",
)
async def add_members(
    conversation_id: uuid.UUID,
    payload: MembersAddIn,
    user: ActiveUser,
    session: SessionDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> ConversationOut:
    conversation = await messaging_service.add_members(
        session,
        actor=user,
        conversation_id=conversation_id,
        member_ids=payload.user_ids,
        settings=settings,
    )
    return await _one(session, storage, conversation, user)


@router.delete(
    "/conversations/{conversation_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove someone from a group chat",
    description="Admins remove members; only the owner removes admins.",
)
async def remove_member(
    conversation_id: uuid.UUID, user_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> None:
    await messaging_service.remove_member(
        session, actor=user, conversation_id=conversation_id, user_id=user_id
    )


@router.put(
    "/conversations/{conversation_id}/members/{user_id}/role",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change a member's role",
    description="Owner only. Giving someone `owner` hands over the chat; you become an admin.",
)
async def set_role(
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: RoleIn,
    user: CurrentUser,
    session: SessionDep,
) -> None:
    await messaging_service.set_role(
        session, actor=user, conversation_id=conversation_id, user_id=user_id, role=payload.role
    )


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=MessagePageOut,
    summary="Read messages",
    description=(
        "In sequence order, oldest first. With no parameters: the latest page. "
        "`after_seq` fetches everything after the last message a client holds — "
        "this is how a reconnecting client catches up. `before_seq` scrolls back." + NOT_FOUND_DOC
    ),
)
async def list_messages(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    storage: StorageDep,
    after_seq: Annotated[int | None, Query(ge=0)] = None,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=messaging_service.MAX_MESSAGE_PAGE)] = 50,
) -> MessagePageOut:
    page = await messaging_service.list_messages(
        session,
        viewer=user,
        conversation_id=conversation_id,
        after_seq=after_seq,
        before_seq=before_seq,
        limit=limit,
    )
    return MessagePageOut(
        items=await views.message_views(session, storage, messages=page.messages, viewer=user),
        has_more=page.has_more,
    )


@router.get(
    "/conversations/{conversation_id}/messages/changes",
    response_model=list[MessageOut],
    summary="Messages deleted since a moment",
    description=(
        "Sync by sequence finds new messages but not deletions of old ones. Call "
        "this with the previous sync time for conversations that `GET /v1/sync` lists."
    ),
)
async def messages_changed_since(
    conversation_id: uuid.UUID,
    since: datetime,
    user: CurrentUser,
    session: SessionDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> list[MessageOut]:
    messages = await messaging_service.messages_changed_since(
        session, viewer=user, conversation_id=conversation_id, since=since, settings=settings
    )
    return await views.message_views(session, storage, messages=messages, viewer=user)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
    summary="Send a message",
    description=(
        "Text, or one ready `message` upload. Persisted before anyone is told: the "
        "socket event arrives after this returns. Offline members get a push.\n\n"
        "409 with `details.reason = awaiting_reply` when the caller has already "
        f"sent a request that has not been accepted.\n\nSend an `{IDEMPOTENCY_HEADER}` "
        "header to make retries safe."
    ),
    responses={409: {"model": ErrorEnvelope, "description": "Request awaiting a reply"}},
    dependencies=[Depends(_message_limit)],
)
async def send_message(
    conversation_id: uuid.UUID,
    payload: MessageCreateIn,
    request: Request,
    user: ActiveUser,
    session: SessionDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> JSONResponse:
    async def action() -> MessageOut:
        message = await messaging_service.send_message(
            session,
            sender=user,
            conversation_id=conversation_id,
            body=payload.body,
            media_id=payload.media_id,
            reply_to_id=payload.reply_to_id,
            settings=settings,
        )
        (rendered,) = await views.message_views(session, storage, messages=[message], viewer=user)
        return rendered

    return await execute_idempotently(
        session,
        request=request,
        user_id=user.id,
        payload={"conversation_id": str(conversation_id), **payload.model_dump(mode="json")},
        status_code=status.HTTP_201_CREATED,
        action=action,
        settings=settings,
    )


@router.delete(
    "/messages/{message_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a message for everyone",
    description='Sender only. It becomes "message deleted" and its text or image is erased.',
)
async def delete_message(
    message_id: uuid.UUID, user: CurrentUser, session: SessionDep, storage: StorageDep
) -> None:
    await messaging_service.delete_message(session, storage, user=user, message_id=message_id)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
@router.get(
    "/sync",
    response_model=SyncOut,
    summary="Conversations changed since the last sync",
    description=(
        "For a client coming back online. Lists conversations that changed after "
        "`since` (omit it on first launch), each with its `last_seq`; then fetch "
        "`messages?after_seq=` for any whose `last_seq` is ahead of what the client "
        "holds. Pass `next_since` back next time. Re-listing is harmless: sync by "
        "sequence number is idempotent."
    ),
)
async def sync(
    user: CurrentUser,
    session: SessionDep,
    storage: StorageDep,
    settings: SettingsDep,
    since: datetime | None = None,
) -> SyncOut:
    result = await messaging_service.sync(session, viewer=user, since=since, settings=settings)
    return SyncOut(
        conversations=await views.conversation_views(
            session, storage, conversations=result.conversations, viewer=user
        ),
        next_since=result.next_since,
        has_more=result.has_more,
    )
