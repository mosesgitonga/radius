from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.modules.feed.schemas import AuthorOut
from app.modules.follows.models import FollowEntityType


class FollowIn(BaseModel):
    entity_type: FollowEntityType = Field(
        description=(
            "What is followed. Only `service_provider` for now: a person can never be "
            "followed, and any other type is refused."
        )
    )
    entity_id: uuid.UUID = Field(description="For `service_provider`, the offering's ID.")


class FollowedServiceOut(BaseModel):
    """The service followed. No place: a follow reaches across any distance."""

    id: uuid.UUID
    title: str
    category: str
    provider: AuthorOut


class FollowOut(BaseModel):
    entity_type: FollowEntityType
    entity_id: uuid.UUID
    followed_at: datetime
    follower_count: int
    is_following: bool = True
    service: FollowedServiceOut | None = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "entity_type": "service_provider",
                "entity_id": "0b9a3f2e-5f8c-4d1e-9a8b-2c6f7e1d3a40",
                "followed_at": "2026-11-01T09:30:00Z",
                "follower_count": 41,
                "is_following": True,
                "service": {
                    "id": "0b9a3f2e-5f8c-4d1e-9a8b-2c6f7e1d3a40",
                    "title": "Plumber, 10 years' experience",
                    "category": "plumbing",
                    "provider": {
                        "id": "7d1c2b3a-4e5f-6a7b-8c9d-0e1f2a3b4c5d",
                        "display_name": "Kamau",
                        "avatar": None,
                        "is_verified_neighbour": True,
                    },
                },
            }
        }
    }


class FollowPageOut(BaseModel):
    items: list[FollowOut]
    next_cursor: str | None = None
