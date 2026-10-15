"""Groups, their members, and invite links.

**A ban is a membership row.** Banning someone sets their row's status to
`banned`, and every way into a group — open join, join request, direct invite,
invite link — goes through the same row first. "Banned users can't rejoin" is then
true by construction, rather than by remembering to check in each of four places.
The row is kept when they would otherwise have left, so leaving cannot launder a
ban.

**Only public groups have a place.** A public group stores a cell and centroid so
it can be discovered nearby; a private group stores none, because it is never
discovered and has no use for one. The check constraint holds that in the
database.
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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin, db_enum


class GroupVisibility(StrEnum):
    public = "public"
    """Discoverable nearby, and readable by anyone signed in."""
    private = "private"
    """Invite-only, never discovered, readable by members alone."""


class GroupJoinPolicy(StrEnum):
    """How someone joins a *public* group. Private groups are invite-only."""

    open = "open"
    approval = "approval"


class GroupRole(StrEnum):
    owner = "owner"
    admin = "admin"
    moderator = "moderator"
    member = "member"


ROLE_RANK: dict[GroupRole, int] = {
    GroupRole.owner: 3,
    GroupRole.admin: 2,
    GroupRole.moderator: 1,
    GroupRole.member: 0,
}
"""Nobody acts on someone of equal or higher rank."""


class GroupMemberStatus(StrEnum):
    active = "active"
    pending = "pending"
    """Asked to join a group that needs approval."""
    invited = "invited"
    """Invited directly by an admin; becomes active on accepting."""
    banned = "banned"
    """Kept for as long as the ban stands. See the module docstring."""


_visibility = db_enum(GroupVisibility, name="group_visibility", length=16)
_join_policy = db_enum(GroupJoinPolicy, name="group_join_policy", length=16)
_role = db_enum(GroupRole, name="group_role", length=16)
_member_status = db_enum(GroupMemberStatus, name="group_member_status", length=16)


class Group(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "groups"

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(10_000), default="", server_default="")
    category: Mapped[str] = mapped_column(String(50), index=True)
    """From `GROUP_CATEGORIES`. A plain string, so categories are configuration."""
    rules: Mapped[str | None] = mapped_column(String(20_000), nullable=True)

    visibility: Mapped[GroupVisibility] = mapped_column(_visibility)
    join_policy: Mapped[GroupJoinPolicy] = mapped_column(
        _join_policy, default=GroupJoinPolicy.open, server_default=GroupJoinPolicy.open.value
    )
    requires_post_approval: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )

    cover_media_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="SET NULL"), nullable=True
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    """Null only for a group whose last member was purged; such a group is deleted."""
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    member_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    """Active members. Changed in SQL, never read-modify-write."""

    chat_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
        unique=True,
    )

    # Location — public groups only.
    cell_res9: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cell_res8: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    cell_res7: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cell_res6: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    centroid: Mapped[object | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=True), nullable=True
    )
    """The cell centre of the creator's home when the group was made public. Never
    a position, and never returned: groups are placed by area label and band."""
    area_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("areas.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "visibility <> 'public' OR (centroid IS NOT NULL AND cell_res9 IS NOT NULL)",
            name="group_public_has_location",
        ),
        CheckConstraint(
            "visibility <> 'private' OR (centroid IS NULL AND cell_res9 IS NULL)",
            name="group_private_has_no_location",
        ),
        CheckConstraint("member_count >= 0", name="group_member_count_non_negative"),
        CheckConstraint("length(name) > 0", name="group_name_not_empty"),
        Index(
            "ix_groups_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        # Discovery browses public, live groups by popularity.
        Index("ix_groups_visibility_member_count", "visibility", "member_count"),
    )

    @property
    def is_public(self) -> bool:
        return self.visibility == GroupVisibility.public


class GroupMember(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "group_members"

    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[GroupRole] = mapped_column(
        _role, default=GroupRole.member, server_default=GroupRole.member.value
    )
    status: Mapped[GroupMemberStatus] = mapped_column(_member_status)

    invited_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    muted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Until then, the member can read but not post, comment or react."""

    banned_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    banned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ban_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    """Seen by the group's admins only, never by the banned person."""

    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_group_members_group_id_user_id"),
        Index("ix_group_members_user_id_status", "user_id", "status"),
        Index("ix_group_members_group_id_status_role", "group_id", "status", "role"),
        CheckConstraint(
            "status <> 'banned' OR banned_at IS NOT NULL", name="group_member_ban_has_time"
        ),
    )


class GroupInviteLink(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A shareable link into a group.

    Only a keyed hash of the token is stored, as for refresh tokens: the link is a
    bearer credential for a private group, and a database leak must not hand over
    working invitations.
    """

    __tablename__ = "group_invite_links"

    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("use_count >= 0", name="group_invite_link_use_count_non_negative"),
        CheckConstraint(
            "max_uses IS NULL OR max_uses > 0", name="group_invite_link_max_uses_positive"
        ),
    )

    def is_usable(self, now: datetime) -> bool:
        return (
            self.revoked_at is None
            and self.expires_at > now
            and (self.max_uses is None or self.use_count < self.max_uses)
        )
