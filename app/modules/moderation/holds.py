"""Holding new content for review.

Content from a restricted account is created hidden-pending-review: its author
sees it, nobody else does, and it sits in the moderation queue until a moderator
restores or removes it. A leaf — audit and models only — because every content
module calls it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.moderation.audit import record_audit_event
from app.modules.moderation.models import AuditAction, AuditActorType, ContentStatus
from app.modules.users.models import User
from app.modules.users.standing import is_restricted


async def hold_if_restricted(
    session: AsyncSession, *, author: User, content: Any, target_type: str
) -> bool:
    """Hold `content` (already flushed) when its author is restricted."""
    if not is_restricted(author) or content.status != ContentStatus.visible:
        return False
    content.status = ContentStatus.hidden_pending_review
    content.hidden_at = datetime.now(UTC)
    await session.flush()
    await record_audit_event(
        session,
        action=AuditAction.content_flagged,
        actor_type=AuditActorType.system,
        target_type=target_type,
        target_id=content.id,
        context={"check": "restricted_account"},
    )
    return True
