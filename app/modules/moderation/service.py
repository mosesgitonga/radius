"""Audit logging.

One entry point, `record_audit_event`, used by every module. It does one thing
beyond inserting a row: it refuses to store sensitive values.

That guard is not defensive programming for its own sake. The audit log is kept
for two years, is read by humans during incidents, and is the natural place for a
well-meaning developer to drop "the location the user set" or "the phone number
that requested the code" while debugging. Those are exactly the values the rest of
the system works to avoid storing, so the check lives here where it cannot be
forgotten rather than in a convention nobody reads.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_request_id
from app.core.logging import is_sensitive_key
from app.modules.moderation.models import AuditAction, AuditActorType, AuditEvent

logger = logging.getLogger(__name__)

REDACTED_BY_POLICY = "[rejected:sensitive-key]"


def sanitise_context(context: dict[str, Any] | None) -> dict[str, Any]:
    """Strip values the audit log must not retain.

    Keys are matched by the same rule the log redaction filter uses, so "sensitive"
    means one thing across the system. The key is kept and the value replaced,
    because the fact that someone tried to record a coordinate is itself worth
    seeing in a review.
    """
    if not context:
        return {}
    return {
        key: REDACTED_BY_POLICY if is_sensitive_key(key) else value
        for key, value in context.items()
    }


async def record_audit_event(
    session: AsyncSession,
    *,
    action: AuditAction,
    actor_type: AuditActorType = AuditActorType.user,
    actor_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    context: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append one audit row.

    Added to the session but not committed: the audit entry belongs to the same
    transaction as the action it records, so an action that rolls back leaves no
    claim that it happened.
    """
    event = AuditEvent(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        context=sanitise_context(context),
        request_id=get_request_id(),
    )
    session.add(event)
    logger.info(
        "audit_event",
        extra={"audit_action": str(action), "actor_type": str(actor_type)},
    )
    return event
