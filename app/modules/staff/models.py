"""Staff accounts: the people who moderate, and who administer the moderators.

**Staff are not users.** They live in their own table, sign in a different way, and
carry a different kind of token. A user token is refused by every staff endpoint
and a staff token by every user endpoint, so there is no code path by which an
ordinary account, however it is compromised, reaches the moderation API — and no
moderator account doubles as a neighbour's profile.

**Two factors, always.** An email and a password (scrypt), plus a code from an
authenticator app (TOTP, RFC 6238). Users prove themselves by SMS because that is
what a phone number is for; staff can ban people, and SMS is the factor a SIM
swap defeats.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin, db_enum


class StaffRole(StrEnum):
    moderator = "moderator"
    """Works the queue: removes and restores content, warns, suspends, restricts."""
    admin = "admin"
    """Everything a moderator does, plus bans, the ban lists, the audit log, and
    managing staff accounts."""


_role = db_enum(StaffRole, name="staff_role", length=16)


class StaffAccount(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "staff_accounts"

    email: Mapped[str] = mapped_column(String(320), unique=True)
    """Stored lower-cased; compared lower-cased."""
    display_name: Mapped[str] = mapped_column(String(100))
    role: Mapped[StaffRole] = mapped_column(_role)

    password_hash: Mapped[str] = mapped_column(String(255))
    """`scrypt$n$r$p$salt$hash`, all parameters in the string so they can be raised
    later without breaking existing hashes."""
    totp_secret: Mapped[str] = mapped_column(String(64))
    """Base32. A credential, held only here and shown once, when the account is
    created."""
    totp_last_step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """The last 30-second step a code was accepted for. A code is good once: a
    shoulder-surfed code cannot be replayed within its window."""

    failed_logins: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("staff_accounts.id", ondelete="SET NULL"), nullable=True
    )


class StaffSession(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One sign-in. Short-lived, with no refresh token: a moderator signs in again,
    with a fresh code, for each shift."""

    __tablename__ = "staff_sessions"

    staff_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("staff_accounts.id", ondelete="CASCADE"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (Index("ix_staff_sessions_expires_at", "expires_at"),)
