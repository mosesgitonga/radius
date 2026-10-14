"""Device sessions, which are where refresh tokens live.

One row per signed-in device. The refresh token itself is never stored — only a
keyed hash of it — so a database leak does not hand over working credentials.

The interesting part is `family_id` and `replaced_by_id`, which together implement
reuse detection. Rotating refresh tokens are safer than long-lived ones, but
rotation creates a problem: if a client's network drops after the server rotates
and before the client stores the new token, the client retries with a token the
server has already consumed. That looks exactly like an attacker replaying a
stolen token, and it must be treated as one — but then an honest client on bad
mobile data would be logged out at random.

The resolution is to keep the whole chain. Each rotation records the session it
replaced, and every session descended from one sign-in shares a `family_id`. When
a consumed token is presented again, the entire family is revoked: whoever holds
the current token loses it too, so a thief cannot keep using what they stole, and
the honest user signs in again once rather than being quietly impersonated.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin, db_enum


class SessionRevocationReason(StrEnum):
    logout = "logout"
    """The user signed out of this device."""
    logout_all = "logout_all"
    """The user signed out everywhere."""
    rotated = "rotated"
    """Normal rotation: superseded by the session in `replaced_by_id`."""
    reuse_detected = "reuse_detected"
    """An already-consumed token was presented again, so the family was revoked."""
    expired = "expired"
    max_sessions = "max_sessions"
    """Evicted because the account reached its session limit."""
    account_deleted = "account_deleted"
    admin_action = "admin_action"
    """Revoked by a moderator, from Phase 8."""


_revocation_reason = db_enum(SessionRevocationReason, name="session_revocation_reason", length=32)


class DeviceSession(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A refresh token's record."""

    __tablename__ = "device_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    refresh_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    """HMAC-SHA256 of the token, keyed with the application secret. Deterministic
    so the session can be found from the token alone, which a salted KDF could not
    do. See `app/core/security.py` for why that is the right trade here."""

    family_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    """Shared by every session descended from one sign-in. Revoking a family is how
    a replayed token is contained."""

    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("device_sessions.id", ondelete="SET NULL"), nullable=True
    )
    """The session this one rotated into, so the chain can be walked when
    diagnosing a reuse event."""

    device_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """Client-supplied and therefore untrusted — shown back to the user on the
    sessions screen so they can recognise their own devices, never used for
    authorisation."""

    device_platform: Mapped[str | None] = mapped_column(String(20), nullable=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[SessionRevocationReason | None] = mapped_column(
        _revocation_reason, nullable=True
    )

    __table_args__ = (
        # Listing a user's live sessions, and the max-sessions eviction check.
        Index("ix_device_sessions_user_id_revoked_at", "user_id", "revoked_at"),
        # The retention job sweeps expired rows.
        Index("ix_device_sessions_expires_at", "expires_at"),
    )

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at <= now

    def is_usable(self, now: datetime) -> bool:
        return not self.is_revoked and not self.is_expired(now)

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<DeviceSession {self.id} user={self.user_id} revoked={self.is_revoked}>"
