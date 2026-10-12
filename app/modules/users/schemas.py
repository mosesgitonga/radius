"""Request and response models for users.

Every response model here is checked by `tests/test_privacy_schemas.py`. The
pattern to follow when adding fields: a place is an `AreaLabelOut` and a distance
is a `DistanceBucket`. There is no field for a coordinate, a cell, or a number of
metres, and adding one fails the build.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.location.cells import DistanceBucket
from app.modules.users.models import ConsentType, UserStatus


class AreaLabelOut(BaseModel):
    """Where somebody is, as other people see it.

    A name and a coarseness, nothing else. The label has already been snapped up
    to a cell shared by at least K registered users, so it describes a
    neighbourhood rather than a person.
    """

    label: str = Field(examples=["Kamakwa", "near Nyeri town", "nearby"])
    precision: Literal["neighbourhood", "area", "town", "generic"] = Field(
        description=(
            "How coarse the label is. Coarser means fewer neighbours shared the "
            "finer cell, so it was widened to keep the label anonymous."
        )
    )


class MeOut(BaseModel):
    """The caller's own profile.

    Includes the caller's own phone number — it is their own data, and the app
    shows it on the account screen — but still no coordinates, because the server
    does not have any to give.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    display_name: str
    phone_number: str
    status: UserStatus
    member_since: datetime

    area_label: AreaLabelOut | None = Field(
        default=None, description="Null until a home location has been set."
    )
    has_home_location: bool
    home_set_at: datetime | None = None
    next_home_change_allowed_at: datetime | None = Field(
        default=None,
        description=(
            "When the home area may next be changed. Null when it can be changed "
            "now, or has never been set."
        ),
    )

    is_verified_neighbour: bool
    rating_average: float | None = None
    rating_count: int

    deletion_requested_at: datetime | None = None
    avatar_media_id: uuid.UUID | None = Field(
        default=None, description="Populated from Phase 2, when media exists."
    )


class PublicProfileOut(BaseModel):
    """Another neighbour, as the caller may see them."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    display_name: str
    area_label: AreaLabelOut | None = None
    member_since: datetime

    distance_bucket: DistanceBucket | None = Field(
        default=None,
        description=(
            "How far away they are, as a band rather than a number. Bands exist "
            "specifically so that repeated measurement from different positions "
            "cannot narrow down where somebody lives."
        ),
    )

    is_verified_neighbour: bool = Field(
        description="A neighbour whose home area has been consistent over time."
    )
    rating_average: float | None = None
    rating_count: int


class ProfileUpdateIn(BaseModel):
    display_name: str | None = Field(default=None, min_length=2, max_length=50)


class ConsentOut(BaseModel):
    consent_type: ConsentType
    granted: bool
    document_version: str
    recorded_at: datetime


class ConsentUpdateIn(BaseModel):
    """Only optional consents can be changed here.

    Withdrawing a required consent means deleting the account, and the service
    says so rather than offering a toggle that would quietly break the product.
    """

    consent_type: Literal[ConsentType.marketing]
    granted: bool


class BlockCreateIn(BaseModel):
    user_id: uuid.UUID


class BlockOut(BaseModel):
    user_id: uuid.UUID
    display_name: str
    blocked_at: datetime


class DeletionStatusOut(BaseModel):
    requested_at: datetime
    purge_after: datetime = Field(
        description=(
            "When the account is irreversibly purged. Until then the request can be cancelled."
        )
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "requested_at": "2026-10-12T08:00:00Z",
                "purge_after": "2026-11-11T08:00:00Z",
            }
        }
    )
