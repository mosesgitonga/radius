"""Request and response models for the feed.

Every response here is scanned by `tests/test_privacy_schemas.py`. A post is
placed by an `AreaLabelOut` and a `DistanceBucket` — never a cell, a centroid, or
a number of metres.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.modules.feed.models import PostApproval
from app.modules.location.cells import DistanceBucket
from app.modules.media.schemas import ImageVariantOut
from app.modules.moderation.models import ContentStatus
from app.modules.users.schemas import AreaLabelOut


class AuthorOut(BaseModel):
    """Who wrote something, as much as a neighbour needs and no more."""

    id: uuid.UUID
    display_name: str
    avatar: dict[str, ImageVariantOut] | None = None
    is_verified_neighbour: bool


class PostImageOut(BaseModel):
    media_id: uuid.UUID
    variants: dict[str, ImageVariantOut]


class PostCreateIn(BaseModel):
    body: str = Field(
        min_length=1, max_length=4000, examples=["Has anyone seen a brown dog near Kamakwa?"]
    )
    category: str = Field(examples=["lost_and_found"])
    media_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="Ready `post` uploads, in display order. At most six.",
    )


class PostOut(BaseModel):
    id: uuid.UUID
    author: AuthorOut
    body: str
    category: str
    images: list[PostImageOut]
    area_label: AreaLabelOut
    distance_bucket: DistanceBucket | None = Field(
        default=None,
        description="How far from the viewer, as a band. Null on the author's own posts.",
    )
    comment_count: int
    reaction_count: int
    my_reaction: str | None = None
    status: ContentStatus = Field(
        description=(
            "Always `visible` for other people's posts. The author also sees "
            "`hidden_pending_review` on their own, because being hidden without "
            "being told is worse than being told."
        )
    )
    group_id: uuid.UUID | None = Field(default=None, description="Set for a post in a group.")
    approval_status: PostApproval | None = Field(
        default=None,
        description=(
            "Group posts only, in groups that approve posts. The author and the "
            "group's moderators see `pending` and `rejected`; everyone else sees "
            "only approved posts."
        ),
    )
    pinned: bool = False
    created_at: datetime


class PostCreatedOut(BaseModel):
    post: PostOut
    reach_estimate: int = Field(
        description=(
            "Roughly how many active neighbours can see this — 'visible to about "
            "340 neighbours'. Rounded, and an estimate by design."
        ),
        examples=[340],
    )


class FeedOut(BaseModel):
    """One page of the feed. The same shape as every list in the API."""

    items: list[PostOut]
    next_cursor: str | None = None
    radius_km: float = Field(
        description="The radius this page was drawn from, rounded for display."
    )
    sort: Literal["recent"] = Field(
        default="recent",
        description=(
            "How the page is ordered. Only `recent` exists today; the field is "
            "here so ranking can be added later without changing the response."
        ),
    )


class CommentCreateIn(BaseModel):
    body: str = Field(min_length=1, max_length=2000)
    parent_id: uuid.UUID | None = Field(
        default=None,
        description="A top-level comment to reply to. Replies to replies are refused.",
    )


class CommentOut(BaseModel):
    id: uuid.UUID
    post_id: uuid.UUID
    parent_id: uuid.UUID | None
    author: AuthorOut
    author_area_label: AreaLabelOut | None = Field(
        default=None,
        description="Where the commenter lives — their home area, never where they are.",
    )
    is_visiting: bool = Field(
        default=False,
        description=(
            "True when the commenter wrote this while exploring another area, so "
            "locals can tell a visitor from a neighbour."
        ),
    )
    body: str
    reply_count: int
    reaction_count: int
    my_reaction: str | None = None
    created_at: datetime


class CommentPageOut(BaseModel):
    items: list[CommentOut]
    next_cursor: str | None = None


class ReactionIn(BaseModel):
    kind: str = Field(examples=["like"])


class ReactionOut(BaseModel):
    kind: str | None = Field(description="The caller's reaction now, or null if removed.")
    reaction_count: int


class StoryCreateIn(BaseModel):
    kind: Literal["image", "text"]
    media_id: uuid.UUID | None = None
    text: str | None = Field(default=None, max_length=500)


class StoryOut(BaseModel):
    id: uuid.UUID
    author: AuthorOut
    kind: Literal["image", "text"]
    image: dict[str, ImageVariantOut] | None = None
    text: str | None = None
    area_label: AreaLabelOut
    distance_bucket: DistanceBucket | None = None
    expires_at: datetime
    created_at: datetime
    view_count: int | None = Field(default=None, description="Shown to the author only.")


class StoryPageOut(BaseModel):
    items: list[StoryOut]
    next_cursor: str | None = None


class StoryViewerOut(BaseModel):
    user: AuthorOut
    viewed_at: datetime


class StoryViewerPageOut(BaseModel):
    items: list[StoryViewerOut]
    next_cursor: str | None = None
