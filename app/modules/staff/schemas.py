"""Request and response models for staff sign-in and staff management."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.modules.staff.models import StaffAccount, StaffRole


class SignInIn(BaseModel):
    email: str = Field(max_length=320, examples=["moderator@example.com"])
    password: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=6, max_length=10, description="From the authenticator app.")


class StaffOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    role: StaffRole
    disabled: bool
    locked: bool
    last_login_at: datetime | None
    created_at: datetime

    @classmethod
    def of(cls, account: StaffAccount, *, now: datetime) -> StaffOut:
        return cls(
            id=account.id,
            email=account.email,
            display_name=account.display_name,
            role=account.role,
            disabled=account.disabled_at is not None,
            locked=account.locked_until is not None and account.locked_until > now,
            last_login_at=account.last_login_at,
            created_at=account.created_at,
        )


class SignInOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 — the OAuth token type, not a secret
    expires_at: datetime
    staff: StaffOut


class StaffCreateIn(BaseModel):
    email: str = Field(max_length=320)
    display_name: str = Field(min_length=1, max_length=100)
    role: StaffRole = StaffRole.moderator


class StaffUpdateIn(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    role: StaffRole | None = None
    disabled: bool | None = None


class IssuedCredentialsOut(BaseModel):
    """Shown once. Hand the password and the authenticator secret (or the QR code
    of `totp_uri`) to the moderator over a channel you trust; neither can be read
    back later."""

    staff: StaffOut
    temporary_password: str
    totp_secret: str
    totp_uri: str


class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=6, max_length=10)
