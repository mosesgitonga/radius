"""Notifications: device tokens, preferences, the in-app list, and push.

Three rules shape everything here.

* **Nobody is notified about themselves, or by someone blocked.** Blocking is
  checked at the moment of notifying, so a block placed between the reaction and
  the push still holds.
* **Bursts collapse.** A repeat event with the same collapse key, while the
  earlier notification is unread and recent, updates that one row — "Kamau and 3
  others reacted to your post" — rather than adding another. Pushes per key are
  throttled on top, so a post going viral buzzes the author's phone once a minute
  at most, not once per reaction.
* **Online users get the socket, not the phone.** A push is for someone who is
  not looking. Somebody with the app open receives the realtime event and nothing
  buzzes.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import after_commit, execute_dml
from app.core.errors import NotFoundError
from app.core.pagination import PageParams, paginate
from app.core.providers.push import PushMessage, PushProvider
from app.core.queue import get_queue
from app.core.realtime import online_user_ids, publish_after_commit
from app.core.redis import CACHE_ERRORS
from app.modules.auth.models import DeviceSession
from app.modules.notifications.models import (
    MAX_REMEMBERED_ACTORS,
    DeviceToken,
    Notification,
    NotificationPreference,
    NotificationType,
)
from app.modules.users.models import User
from app.modules.users.service import blocked_user_ids, is_blocked_between

logger = logging.getLogger(__name__)

NOTIFICATION_CREATED = "notification.created"

# Types with an in-app row. Messages have none: the inbox is their in-app surface,
# and a notification per message would double every unread count.
IN_APP_TYPES: frozenset[NotificationType] = frozenset(NotificationType) - {NotificationType.message}


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------
async def register_device(
    session: AsyncSession,
    *,
    user: User,
    device_session_id: uuid.UUID | None,
    token: str,
    platform: str,
) -> DeviceToken:
    """Register, or re-register, a push token for this sign-in.

    The token is unique, and re-registering moves it: when a shared phone signs
    in as somebody else, the token follows the new account and the previous one
    stops receiving pushes on that phone. That is the behaviour a family sharing a
    handset needs.
    """
    now = datetime.now(UTC)
    statement = (
        insert(DeviceToken)
        .values(
            id=uuid.uuid4(),
            user_id=user.id,
            device_session_id=device_session_id,
            token=token,
            platform=platform,
            last_seen_at=now,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[DeviceToken.token],
            set_={
                "user_id": user.id,
                "device_session_id": device_session_id,
                "platform": platform,
                "last_seen_at": now,
                "updated_at": now,
            },
        )
        .returning(DeviceToken.id)
    )
    token_id = (await session.execute(statement)).scalar_one()
    device = await session.get(DeviceToken, token_id, populate_existing=True)
    assert device is not None  # noqa: S101 — just upserted
    return device


async def unregister_device(session: AsyncSession, *, user: User, token: str) -> None:
    """Idempotent: removing a token that is not registered is not an error."""
    await execute_dml(
        session,
        delete(DeviceToken).where(DeviceToken.user_id == user.id, DeviceToken.token == token),
    )


async def live_tokens(session: AsyncSession, user_id: uuid.UUID) -> list[str]:
    """Tokens registered by sign-ins that are still live.

    A token from a signed-out session is not pushed to, even before it is cleaned
    up — signing out has to mean the phone stops showing that person's messages.
    """
    statement = (
        select(DeviceToken.token)
        .join(DeviceSession, DeviceSession.id == DeviceToken.device_session_id)
        .where(
            DeviceToken.user_id == user_id,
            DeviceSession.revoked_at.is_(None),
            DeviceSession.expires_at > datetime.now(UTC),
        )
    )
    return list((await session.execute(statement)).scalars().all())


async def forget_tokens(session: AsyncSession, tokens: Iterable[str]) -> int:
    dead = list(tokens)
    if not dead:
        return 0
    return await execute_dml(session, delete(DeviceToken).where(DeviceToken.token.in_(dead)))


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Preference:
    type: NotificationType
    push_enabled: bool = True
    in_app_enabled: bool = True
    show_preview: bool = False


async def preferences_for(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Preference]:
    """Every type, with stored overrides applied to the defaults."""
    stored = {
        row.type: row
        for row in (
            await session.execute(
                select(NotificationPreference).where(NotificationPreference.user_id == user_id)
            )
        ).scalars()
    }
    result: dict[str, Preference] = {}
    for kind in NotificationType:
        row = stored.get(kind.value)
        result[kind.value] = (
            Preference(
                type=kind,
                push_enabled=row.push_enabled,
                in_app_enabled=row.in_app_enabled,
                show_preview=row.show_preview,
            )
            if row is not None
            else Preference(type=kind)
        )
    return result


async def set_preference(
    session: AsyncSession,
    *,
    user: User,
    kind: NotificationType,
    push_enabled: bool | None,
    in_app_enabled: bool | None,
    show_preview: bool | None,
) -> Preference:
    current = (await preferences_for(session, user.id))[kind.value]
    merged = Preference(
        type=kind,
        push_enabled=current.push_enabled if push_enabled is None else push_enabled,
        in_app_enabled=current.in_app_enabled if in_app_enabled is None else in_app_enabled,
        show_preview=current.show_preview if show_preview is None else show_preview,
    )
    now = datetime.now(UTC)
    values = {
        "push_enabled": merged.push_enabled,
        "in_app_enabled": merged.in_app_enabled,
        "show_preview": merged.show_preview,
        "updated_at": now,
    }
    await session.execute(
        insert(NotificationPreference)
        .values(id=uuid.uuid4(), user_id=user.id, type=kind.value, created_at=now, **values)
        .on_conflict_do_update(constraint="uq_notification_preferences_user_id_type", set_=values)
    )
    return merged


# ---------------------------------------------------------------------------
# Creating notifications
# ---------------------------------------------------------------------------
async def notify(
    session: AsyncSession,
    *,
    recipient_id: uuid.UUID,
    kind: NotificationType,
    actor_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
    collapse_key: str,
    settings: Settings,
    data: dict[str, str] | None = None,
) -> Notification | None:
    """Record an in-app notification and schedule its push, after commit.

    Returns None when nothing was recorded: the recipient is the actor, either has
    blocked the other, or the type is switched off in-app. A switched-off in-app
    type can still push; the two preferences are independent.
    """
    if recipient_id == actor_id:
        return None
    if await is_blocked_between(session, recipient_id, actor_id):
        return None

    preference = (await preferences_for(session, recipient_id))[kind.value]
    notification: Notification | None = None

    if kind in IN_APP_TYPES and preference.in_app_enabled:
        notification = await _collapse_or_create(
            session,
            recipient_id=recipient_id,
            kind=kind,
            actor_id=actor_id,
            target_type=target_type,
            target_id=target_id,
            collapse_key=collapse_key,
            data=data or {},
            settings=settings,
        )
        publish_after_commit(
            session,
            [recipient_id],
            NOTIFICATION_CREATED,
            {"notification_id": str(notification.id), "type": kind.value},
        )

    if preference.push_enabled:
        job_args = (
            str(recipient_id),
            kind.value,
            str(actor_id),
            collapse_key,
            dict(data or {}, target_type=target_type, target_id=str(target_id)),
        )

        async def _enqueue() -> None:
            await get_queue().enqueue("deliver_notification_push", *job_args)

        after_commit(session, _enqueue)
    return notification


async def _collapse_or_create(
    session: AsyncSession,
    *,
    recipient_id: uuid.UUID,
    kind: NotificationType,
    actor_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
    collapse_key: str,
    data: dict[str, str],
    settings: Settings,
) -> Notification:
    window_start = datetime.now(UTC) - timedelta(
        minutes=settings.notification_collapse_window_minutes
    )
    existing = (
        await session.execute(
            select(Notification)
            .where(
                Notification.user_id == recipient_id,
                Notification.collapse_key == collapse_key,
                Notification.read_at.is_(None),
                Notification.created_at >= window_start,
            )
            .order_by(Notification.created_at.desc())
            .limit(1)
            # Two reactions landing together would otherwise both read the
            # same count and one increment would be lost.
            .with_for_update()
        )
    ).scalar_one_or_none()

    actor = str(actor_id)
    if existing is not None:
        remembered = [other for other in existing.actor_ids if other != actor]
        existing.actor_ids = [actor, *remembered][:MAX_REMEMBERED_ACTORS]
        existing.actor_id = actor_id
        existing.event_count = existing.event_count + 1
        existing.updated_at = datetime.now(UTC)
        await session.flush()
        return existing

    notification = Notification(
        user_id=recipient_id,
        type=kind.value,
        actor_id=actor_id,
        actor_ids=[actor],
        event_count=1,
        target_type=target_type,
        target_id=target_id,
        collapse_key=collapse_key,
        data=data,
    )
    session.add(notification)
    await session.flush()
    return notification


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
async def list_notifications(
    session: AsyncSession, *, user: User, params: PageParams
) -> tuple[list[Notification], str | None]:
    """Newest activity first. Notifications from people now blocked are left out."""
    hidden = await blocked_user_ids(session, user.id)
    statement = select(Notification).where(Notification.user_id == user.id)
    if hidden:
        statement = statement.where(
            Notification.actor_id.is_(None) | Notification.actor_id.not_in(hidden)
        )
    return await paginate(
        session,
        statement,
        sort_column=Notification.updated_at,
        id_column=Notification.id,
        params=params,
    )


async def unread_count(session: AsyncSession, *, user: User) -> int:
    hidden = await blocked_user_ids(session, user.id)
    statement = select(func.count(Notification.id)).where(
        Notification.user_id == user.id, Notification.read_at.is_(None)
    )
    if hidden:
        statement = statement.where(
            Notification.actor_id.is_(None) | Notification.actor_id.not_in(hidden)
        )
    return int((await session.execute(statement)).scalar_one())


async def mark_read(
    session: AsyncSession, *, user: User, notification_id: uuid.UUID | None = None
) -> int:
    """Mark one notification read, or all of them when no ID is given."""
    statement = (
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
        # `updated_at` kept as it was: it orders the list by activity, and reading
        # is not activity — otherwise "mark all read" would reshuffle the list.
        .values(read_at=datetime.now(UTC), updated_at=Notification.updated_at)
    )
    if notification_id is not None:
        owned = await session.scalar(
            select(Notification.id).where(
                Notification.id == notification_id, Notification.user_id == user.id
            )
        )
        if owned is None:
            raise NotFoundError("Notification not found")
        statement = statement.where(Notification.id == notification_id)
    return await execute_dml(session, statement)


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------
def _throttle_key(user_id: uuid.UUID, collapse_key: str) -> str:
    return f"push:throttle:{user_id}:{collapse_key}"


async def _claim_push_slot(
    redis: Redis, user_id: uuid.UUID, collapse_key: str, settings: Settings
) -> bool:
    """Whether a push for this key may go out now. Fails open: a throttle that
    cannot be checked must not swallow somebody's message notification."""
    if settings.push_throttle_seconds <= 0:
        return True
    try:
        claimed = await redis.set(
            _throttle_key(user_id, collapse_key),
            "1",
            ex=settings.push_throttle_seconds,
            nx=True,
        )
    except CACHE_ERRORS:
        logger.warning("push_throttle_unavailable")
        return True
    return bool(claimed)


@dataclass(frozen=True, slots=True)
class PushOutcome:
    sent: bool
    reason: str


async def push_to_user(
    session: AsyncSession,
    redis: Redis,
    provider: PushProvider,
    *,
    user_id: uuid.UUID,
    kind: NotificationType,
    title: str,
    body: str,
    preview: str | None,
    collapse_key: str,
    data: dict[str, str],
    settings: Settings,
) -> PushOutcome:
    """Send one user one push, if every rule allows it.

    `preview` replaces `body` only when the user has opted in to previews for this
    type. The order of checks is cheapest-first, and the throttle slot is claimed
    last so a push skipped for another reason does not use it up.
    """
    preference = (await preferences_for(session, user_id))[kind.value]
    if not preference.push_enabled:
        return PushOutcome(sent=False, reason="disabled")
    if user_id in await online_user_ids(redis, [user_id]):
        return PushOutcome(sent=False, reason="online")
    tokens = await live_tokens(session, user_id)
    if not tokens:
        return PushOutcome(sent=False, reason="no_devices")
    if not await _claim_push_slot(redis, user_id, collapse_key, settings):
        return PushOutcome(sent=False, reason="throttled")

    message = PushMessage(
        title=title,
        body=preview if (preview and preference.show_preview) else body,
        data={**data, "type": kind.value},
        collapse_key=collapse_key,
    )
    result = await provider.send(tokens=tokens, message=message)
    if result.invalid_tokens:
        await forget_tokens(session, result.invalid_tokens)
    return PushOutcome(sent=result.sent > 0, reason="sent" if result.sent else "failed")


_PUSH_TEXT: dict[NotificationType, tuple[str, str]] = {
    # (singular, plural) — "{actor}" and "{others}" are filled in.
    NotificationType.message_request: (
        "{actor} wants to send you a message",
        "{actor} wants to send you a message",
    ),
    NotificationType.chat_invite: (
        "{actor} added you to a group chat",
        "{actor} added you to a group chat",
    ),
    NotificationType.comment: (
        "{actor} commented on your post",
        "{actor} and {others} commented on your post",
    ),
    NotificationType.reply: (
        "{actor} replied to your comment",
        "{actor} and {others} replied to your comment",
    ),
    NotificationType.reaction: (
        "{actor} reacted to your {target}",
        "{actor} and {others} reacted to your {target}",
    ),
    NotificationType.group_invite: (
        "{actor} invited you to a group",
        "{actor} invited you to a group",
    ),
    NotificationType.group_join_request: (
        "{actor} asked to join your group",
        "{actor} and {others} asked to join your group",
    ),
    NotificationType.group_join_approved: (
        "{actor} approved your request to join",
        "{actor} approved your request to join",
    ),
    NotificationType.group_post_approved: (
        "{actor} approved your post",
        "{actor} approved your post",
    ),
    NotificationType.listing_inquiry: (
        "{actor} asked about your listing",
        "{actor} and {others} asked about your listing",
    ),
}


def render_activity(
    kind: NotificationType, *, actor_name: str, others: int, target: str = "post"
) -> str:
    singular, plural = _PUSH_TEXT[kind]
    if others <= 0:
        return singular.format(actor=actor_name, target=target)
    noun = "other" if others == 1 else "others"
    return plural.format(actor=actor_name, others=f"{others} {noun}", target=target)


async def deliver_notification_push(
    session: AsyncSession,
    redis: Redis,
    provider: PushProvider,
    *,
    recipient_id: uuid.UUID,
    kind: NotificationType,
    actor_id: uuid.UUID,
    collapse_key: str,
    data: dict[str, str],
    settings: Settings,
) -> PushOutcome:
    """The worker half of `notify`: render and send the push for an activity.

    Re-checks the block, because one may have been placed after the event and
    before the job ran. The count of others comes from the collapsed in-app row
    when there is one, so the push and the list agree.
    """
    if await is_blocked_between(session, recipient_id, actor_id):
        return PushOutcome(sent=False, reason="blocked")
    actor = await session.get(User, actor_id)
    if actor is None or actor.deleted_at is not None:
        return PushOutcome(sent=False, reason="actor_gone")

    collapsed = (
        await session.execute(
            select(Notification.event_count, Notification.actor_ids)
            .where(
                Notification.user_id == recipient_id,
                Notification.collapse_key == collapse_key,
                Notification.read_at.is_(None),
            )
            .order_by(Notification.updated_at.desc())
            .limit(1)
        )
    ).first()
    others = max(len(collapsed.actor_ids) - 1, 0) if collapsed is not None else 0

    text = render_activity(
        kind,
        actor_name=actor.display_name,
        others=others,
        target=data.get("target_type", "post"),
    )
    return await push_to_user(
        session,
        redis,
        provider,
        user_id=recipient_id,
        kind=kind,
        title="MyRadius",
        body=text,
        preview=None,
        collapse_key=collapse_key,
        data=data,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# Retention and data rights
# ---------------------------------------------------------------------------
async def purge_old_notifications(
    session: AsyncSession, *, settings: Settings, now: datetime | None = None
) -> int:
    cutoff = (now or datetime.now(UTC)) - timedelta(days=settings.notification_retention_days)
    removed = await execute_dml(
        session, delete(Notification).where(Notification.updated_at < cutoff)
    )
    if removed:
        logger.info("notifications_purged", extra={"count": removed})
    return removed


async def delete_stale_device_tokens(
    session: AsyncSession, *, settings: Settings, now: datetime | None = None
) -> int:
    cutoff = (now or datetime.now(UTC)) - timedelta(days=settings.device_token_stale_days)
    removed = await execute_dml(
        session,
        delete(DeviceToken).where(
            (DeviceToken.last_seen_at < cutoff) | DeviceToken.device_session_id.is_(None)
        ),
    )
    if removed:
        logger.info("stale_device_tokens_deleted", extra={"count": removed})
    return removed


async def purge_user_notifications(session: AsyncSession, *, user_id: uuid.UUID) -> dict[str, int]:
    """Everything this module holds about a person, for the erasure right.

    Notifications *about* them in other people's lists stay — they belong to the
    recipient — but they name a user who is now "Deleted user".
    """
    return {
        "device_tokens": await execute_dml(
            session, delete(DeviceToken).where(DeviceToken.user_id == user_id)
        ),
        "notifications": await execute_dml(
            session, delete(Notification).where(Notification.user_id == user_id)
        ),
        "notification_preferences": await execute_dml(
            session,
            delete(NotificationPreference).where(NotificationPreference.user_id == user_id),
        ),
    }


async def export_user_notifications(session: AsyncSession, *, user_id: uuid.UUID) -> dict[str, Any]:
    """Devices, preferences and notifications, for the access right.

    Device tokens are shown by their last eight characters: enough to tell two
    phones apart, and the full token is an address for pushing to that phone,
    which an export file passed around should not carry.
    """
    devices = (
        await session.execute(select(DeviceToken).where(DeviceToken.user_id == user_id))
    ).scalars()
    notifications = (
        await session.execute(
            select(Notification)
            .where(Notification.user_id == user_id)
            .order_by(Notification.created_at.asc())
        )
    ).scalars()
    preferences = await preferences_for(session, user_id)
    return {
        "devices": [
            {
                "platform": device.platform,
                "token_ending": device.token[-8:],
                "registered_at": device.created_at.isoformat(),
                "last_seen_at": device.last_seen_at.isoformat(),
            }
            for device in devices
        ],
        "preferences": [
            {
                "type": key,
                "push_enabled": preference.push_enabled,
                "in_app_enabled": preference.in_app_enabled,
                "show_preview": preference.show_preview,
            }
            for key, preference in sorted(preferences.items())
        ],
        "notifications": [
            {
                "type": notification.type,
                "target_type": notification.target_type,
                "target_id": str(notification.target_id),
                "event_count": notification.event_count,
                "created_at": notification.created_at.isoformat(),
                "read_at": notification.read_at.isoformat() if notification.read_at else None,
            }
            for notification in notifications
        ],
    }
