"""Request and response models for authentication."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PhoneNumberIn(BaseModel):
    """A phone number in any form the brief accepts."""

    phone_number: str = Field(
        min_length=7,
        max_length=20,
        description=(
            "Kenyan mobile number. `07…`, `01…`, `254…` and `+254…` are all "
            "accepted and normalised to E.164."
        ),
        examples=["0712345678", "+254712345678"],
    )


class OtpRequestIn(PhoneNumberIn):
    pass


class OtpRequestOut(BaseModel):
    """Deliberately identical whether or not the number has an account.

    Returning anything that differed would turn this endpoint into a way to find
    out who is registered — which, for a neighbourhood product, is a way to find
    out who lives somewhere.
    """

    expires_in_seconds: int = Field(description="How long the code remains valid.")
    resend_after_seconds: int = Field(
        description="Earliest another code may be requested for this number."
    )
    phone_number_masked: str = Field(
        description="The number with its middle digits hidden, for confirmation.",
        examples=["+2547••••678"],
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "expires_in_seconds": 300,
                "resend_after_seconds": 60,
                "phone_number_masked": "+2547••••678",
            }
        }
    )


class DeviceIn(BaseModel):
    """Optional device description, shown back on the sessions screen.

    Client-supplied and therefore untrusted. It is only ever displayed to the
    account's own owner so they can recognise their devices, and is never used
    for any authorisation decision.
    """

    device_name: str | None = Field(default=None, max_length=100, examples=["Pixel 6a"])
    device_platform: Literal["android", "ios", "web", "other"] | None = None


class OtpVerifyIn(PhoneNumberIn, DeviceIn):
    code: str = Field(min_length=4, max_length=8, examples=["123456"])


class TokenPairOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 — the OAuth scheme name
    expires_in_seconds: int = Field(description="Lifetime of the access token.")


class OtpVerifyOut(BaseModel):
    """Either signed in, or cleared to sign up."""

    status: Literal["signed_in", "signup_required"]
    tokens: TokenPairOut | None = Field(
        default=None, description="Present when `status` is `signed_in`."
    )
    signup_token: str | None = Field(
        default=None,
        description=(
            "Present when `status` is `signup_required`. Single-use, short-lived, "
            "and must be passed to `POST /v1/auth/signup`."
        ),
    )


class SignupIn(DeviceIn):
    """Completing signup after the number is verified.

    Consents are separate booleans rather than one "I agree" flag, because the
    Data Protection Act requires consent to be specific and freely given. Marketing
    is optional and defaults to false; bundling it in would make it not a choice.
    """

    signup_token: str = Field(description="From `POST /v1/auth/otp/verify`.")
    display_name: str = Field(min_length=2, max_length=50, examples=["Wanjiku M."])

    is_adult: Literal[True] = Field(
        description=(
            "Must be `true`. The service is for adults only, which keeps the "
            "Act's parental-consent requirements out of scope."
        )
    )
    accept_terms: Literal[True] = Field(description="Must be `true`.")
    accept_privacy_policy: Literal[True] = Field(description="Must be `true`.")
    accept_location_use: Literal[True] = Field(
        description=(
            "Must be `true`. Everything in MyRadius is scoped by location, so "
            "there is no version of the product that works without it."
        )
    )
    accept_marketing: bool = Field(
        default=False, description="Optional, and may be withdrawn at any time."
    )


class SignupOut(BaseModel):
    tokens: TokenPairOut
    user_id: uuid.UUID


class RefreshIn(DeviceIn):
    refresh_token: str


class LogoutIn(BaseModel):
    refresh_token: str


class LogoutAllIn(BaseModel):
    keep_current_session: bool = Field(
        default=True,
        description=(
            "Keep the session making this request, so signing out other devices "
            "does not sign out the phone in your hand."
        ),
    )


class LogoutAllOut(BaseModel):
    sessions_revoked: int


class SessionOut(BaseModel):
    """One signed-in device. Carries no token and no location."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    device_name: str | None
    device_platform: str | None
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    is_current: bool = False
