"""Circle responses.

None carries a last-seen time, an online flag, or anything finer than an area
label and a distance band, and beyond the viewer's feed radius only a town
(decision C.6). `tests/test_privacy_schemas.py` holds every circle and follow
schema to that.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.modules.circle.service import Relationship
from app.modules.feed.schemas import AuthorOut
from app.modules.location.cells import DistanceBucket
from app.modules.users.schemas import AreaLabelOut


class CircleRequestIn(BaseModel):
    user_id: uuid.UUID


class CircleRequestOut(BaseModel):
    id: uuid.UUID
    user: AuthorOut = Field(description="The other person: who asked, or who was asked.")
    direction: Literal["incoming", "outgoing"]
    status: Literal["pending"] = Field(
        default="pending",
        description=(
            "Always `pending`: only requests waiting for an answer are listed. A request "
            "you sent stays listed until it expires, whatever happened to it."
        ),
    )
    created_at: datetime
    expires_at: datetime


class CircleRequestSentOut(BaseModel):
    relationship: Relationship = Field(
        description=(
            "`request_sent`, or `in_circle` when they had already asked you, which accepts both."
        )
    )
    request: CircleRequestOut | None = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "relationship": "request_sent",
                "request": {
                    "id": "5f0c1d2e-3a4b-4c5d-8e9f-0a1b2c3d4e5f",
                    "user": {
                        "id": "7d1c2b3a-4e5f-6a7b-8c9d-0e1f2a3b4c5d",
                        "display_name": "Wanjiku",
                        "avatar": None,
                        "is_verified_neighbour": False,
                    },
                    "direction": "outgoing",
                    "status": "pending",
                    "created_at": "2026-11-01T09:30:00Z",
                    "expires_at": "2026-12-01T09:30:00Z",
                },
            }
        }
    }


class CircleRequestPageOut(BaseModel):
    items: list[CircleRequestOut]
    next_cursor: str | None = None


class CircleMemberOut(BaseModel):
    user: AuthorOut
    area_label: AreaLabelOut | None = Field(
        default=None,
        description=(
            "Their home area within your feed radius; beyond it, their town only. Null "
            "if they have no home area. A move is never announced."
        ),
    )
    distance_bucket: DistanceBucket | None = Field(
        default=None, description="Within your feed radius only. Never beyond it."
    )
    within_feed_radius: bool
    since: datetime = Field(description="When you joined each other's circle.")


class CircleMemberPageOut(BaseModel):
    items: list[CircleMemberOut]
    next_cursor: str | None = None
