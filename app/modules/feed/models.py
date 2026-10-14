"""Feed models: posts, images, stories, comments, reactions.

Posts carry their own location rather than reading the author's. Two reasons, and
the second is the important one:

1. A post should stay where it was made. If it followed the author's current home,
   a user who moved would drag two years of posts into a neighbourhood that never
   saw them.
2. It makes the feed query a plain spatial predicate on the posts table —
   `ST_DWithin(posts.centroid, viewer_centroid, radius)` — answerable from one
   GiST index. Joining to users to find each author's location would make the
   hottest query in the product a join against a table that is being updated
   constantly.

The location comes from the author's **home** area at posting time, even while
they are exploring elsewhere. See decision 1.1.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from geoalchemy2 import Geography
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.models import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin, db_enum
from app.modules.moderation.models import ContentStatus

_content_status = db_enum(ContentStatus, name="content_status", length=32)


class StoryKind(StrEnum):
    image = "image"
    text = "text"


_story_kind = db_enum(StoryKind, name="story_kind", length=20)


class LocatedContentMixin:
    """The location columns every piece of radius-scoped content needs.

    Shared because posts, stories, and — from Phase 5 — listings, service posts
    and events all answer the same question the same way. One mixin means one
    index strategy and one definition of what "where" means.
    """

    cell_res9: Mapped[int] = mapped_column(BigInteger, index=True)
    cell_res8: Mapped[int] = mapped_column(BigInteger, index=True)
    cell_res7: Mapped[int] = mapped_column(BigInteger, index=True)
    cell_res6: Mapped[int] = mapped_column(BigInteger, index=True)

    centroid: Mapped[object] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=True)
    )
    """The author's home cell centre, never a position. Every proximity query in
    the product runs against this column."""

    area_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("areas.id", ondelete="SET NULL"), nullable=True
    )
    """Resolved at creation so a feed response can show "Kamakwa" without a
    spatial lookup per row."""


class Post(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, LocatedContentMixin):
    """A post in the neighbourhood feed."""

    __tablename__ = "posts"

    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    group_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    """Phase 4. Nullable and not yet a foreign key, as the brief asks: a group post
    reuses this model with a group scope, and adding the constraint later is a
    migration rather than a table rewrite.

    Until then it is always null, and the feed query requires null — so a group
    post will not leak into the public feed the moment Phase 4 starts writing it."""

    body: Mapped[str] = mapped_column(String(4000))
    """Capped well above the configured limit. The configuration is the product
    rule and can be tuned; the column is a backstop so a config mistake cannot
    accept something unbounded."""

    category: Mapped[str] = mapped_column(String(50), index=True)
    """A plain string, not a database enum: the brief requires the category list
    to be configurable, and an enum would mean a migration to add one."""

    status: Mapped[ContentStatus] = mapped_column(
        _content_status,
        default=ContentStatus.visible,
        server_default=ContentStatus.visible.value,
    )
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    reach_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Roughly how many active neighbours could see this, computed once at
    creation. Cached because it is shown to the author and never aggregated."""

    comment_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reaction_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    """Counters denormalised onto the row. A feed page of twenty posts would
    otherwise need two aggregate subqueries per row, and these are read far more
    often than they change."""

    images: Mapped[list[PostImage]] = relationship(
        back_populates="post",
        cascade="all, delete-orphan",
        order_by="PostImage.position",
        lazy="selectin",
    )
    """`selectin` so a feed page loads every post's images in one extra query
    rather than one per post."""

    __table_args__ = (
        CheckConstraint("comment_count >= 0", name="post_comment_count_non_negative"),
        CheckConstraint("reaction_count >= 0", name="post_reaction_count_non_negative"),
        CheckConstraint("length(body) > 0", name="post_body_not_empty"),
        # The feed: visible, undeleted, newest first. The (created_at, id) tail is
        # what the keyset paginator needs to page without duplicates.
        Index("ix_posts_status_created_at_id", "status", "created_at", "id"),
        Index("ix_posts_author_id_created_at", "author_id", "created_at"),
        Index("ix_posts_category_created_at", "category", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<Post {self.id} by {self.author_id}>"


class PostImage(Base, UUIDPrimaryKeyMixin):
    """One image on a post, in a fixed order."""

    __tablename__ = "post_images"

    post_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), index=True
    )
    media_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("media_assets.id", ondelete="RESTRICT"))
    """`RESTRICT`: an image must not vanish from under a post. Deleting the post
    is what releases it, and the media sweeper then cleans up."""

    position: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    post: Mapped[Post] = relationship(back_populates="images")

    __table_args__ = (
        UniqueConstraint("post_id", "position", name="uq_post_images_post_id_position"),
        # The same image twice on one post is a client bug, not a feature.
        UniqueConstraint("post_id", "media_id", name="uq_post_images_post_id_media_id"),
        CheckConstraint("position >= 0", name="post_image_position_non_negative"),
    )


class Story(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, LocatedContentMixin):
    """A story: an image or a line of text, gone in a day."""

    __tablename__ = "stories"

    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[StoryKind] = mapped_column(_story_kind)

    media_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="RESTRICT"), nullable=True
    )
    text: Mapped[str | None] = mapped_column(String(500), nullable=True)

    status: Mapped[ContentStatus] = mapped_column(
        _content_status,
        default=ContentStatus.visible,
        server_default=ContentStatus.visible.value,
    )
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    """Set at creation rather than computed from `created_at` on read, so changing
    the configured lifetime does not retroactively resurrect or kill stories that
    already exist."""

    view_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    __table_args__ = (
        # An image story needs an image; a text story needs text. Enforced here
        # rather than only in the schema, because this is the kind of invariant a
        # future admin tool or backfill will otherwise break.
        CheckConstraint(
            "(kind = 'image' AND media_id IS NOT NULL AND text IS NULL)"
            " OR (kind = 'text' AND text IS NOT NULL AND media_id IS NULL)",
            name="story_kind_matches_content",
        ),
        CheckConstraint("view_count >= 0", name="story_view_count_non_negative"),
        # The expiry sweeper, and the "is this still live?" filter on read.
        Index("ix_stories_expires_at_status", "expires_at", "status"),
        Index("ix_stories_author_id_created_at", "author_id", "created_at"),
    )

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at <= now

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<Story {self.id} {self.kind}>"


class StoryView(Base, UUIDPrimaryKeyMixin):
    """That somebody saw a story. Shown only to its author."""

    __tablename__ = "story_views"

    story_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stories.id", ondelete="CASCADE"), index=True
    )
    viewer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # One row per viewer: the author wants to know who looked, not how often.
        # Counting repeat views would also make it obvious who keeps coming back,
        # which is more than the feature promises.
        UniqueConstraint("story_id", "viewer_id", name="uq_story_views_story_id_viewer_id"),
    )


class Comment(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """A comment, or a single-level reply to one."""

    __tablename__ = "comments"

    post_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), index=True
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("comments.id", ondelete="CASCADE"), nullable=True, index=True
    )
    """One level only, as the brief specifies. The service refuses to reply to a
    reply: unbounded nesting is unreadable on a phone and turns a comment list
    into a recursive query."""

    body: Mapped[str] = mapped_column(String(2000))

    is_visiting: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    """Whether the author was exploring another area when they wrote this.

    Decision 1.1, rule 3: a visitor's comment shows their home area with a
    "visiting" marker, so locals can tell a neighbour from somebody passing
    through. Recorded at write time, because explore state is ephemeral and would
    be gone by the time anyone read the comment."""

    status: Mapped[ContentStatus] = mapped_column(
        _content_status,
        default=ContentStatus.visible,
        server_default=ContentStatus.visible.value,
    )
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reply_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reaction_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    __table_args__ = (
        CheckConstraint("length(body) > 0", name="comment_body_not_empty"),
        CheckConstraint("reply_count >= 0", name="comment_reply_count_non_negative"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="comment_not_self_parent"),
        # Top-level comments on a post, oldest first, paginated.
        Index("ix_comments_post_id_parent_id_created_at", "post_id", "parent_id", "created_at"),
    )

    @property
    def is_reply(self) -> bool:
        return self.parent_id is not None


class Reaction(Base, UUIDPrimaryKeyMixin):
    """One person's reaction to one post or comment."""

    __tablename__ = "reactions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    post_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    comment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("comments.id", ondelete="CASCADE"), nullable=True, index=True
    )
    """Two nullable foreign keys with a check that exactly one is set, rather than
    a type-and-id pair. There are only two reactable things and there will only
    ever be a few, so real referential integrity is worth more here than the
    openness a polymorphic key would buy — a reaction must not outlive its post.
    Reports take the opposite approach, for the opposite reason."""

    kind: Mapped[str] = mapped_column(String(32))
    """From a configurable list, so adding one needs no migration."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "(post_id IS NOT NULL AND comment_id IS NULL)"
            " OR (post_id IS NULL AND comment_id IS NOT NULL)",
            name="reaction_targets_exactly_one",
        ),
        # One reaction per person per thing. Changing it replaces the old one,
        # which is what every client expects and what keeps the counter honest.
        UniqueConstraint("user_id", "post_id", name="uq_reactions_user_id_post_id"),
        UniqueConstraint("user_id", "comment_id", name="uq_reactions_user_id_comment_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        target = self.post_id or self.comment_id
        return f"<Reaction {self.kind} by {self.user_id} on {target}>"
