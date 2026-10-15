"""User, consent, and block models.

The user row carries the home location directly rather than in a side table,
because there is only ever one of them: a change overwrites, and no history is
kept. That is the whole point — see `docs/DATA_MAP.md`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from geoalchemy2 import Geography
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin, db_enum

# How many days of home-presence history the badge rule looks at. The mask is
# this many bits wide, which is the entire location history the system keeps.
HOME_PRESENCE_WINDOW_DAYS = 14


class UserStatus(StrEnum):
    """Account states. A closed set, unlike the product's configurable category
    lists, so it earns a constrained column."""

    active = "active"
    suspended = "suspended"
    """Temporarily restricted by a moderator. Can read, cannot post."""
    banned = "banned"
    deletion_pending = "deletion_pending"
    """Deletion requested and inside the grace period. Still reversible."""


class ConsentType(StrEnum):
    terms = "terms"
    privacy_policy = "privacy_policy"
    location_use = "location_use"
    marketing = "marketing"


# Rendered as VARCHAR with a CHECK constraint rather than a native PostgreSQL
# enum type. Adding a value to a native enum needs ALTER TYPE, which cannot run
# inside a transaction on older servers and is awkward to reverse; a check
# constraint is an ordinary, reversible migration.
_user_status = db_enum(UserStatus, name="user_status", length=32)
_consent_type = db_enum(ConsentType, name="consent_type", length=32)


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """A neighbour.

    There is no password. Identity is a phone number proved by a one-time code,
    which is both the Kenyan norm and one less secret to store badly.
    """

    __tablename__ = "users"

    # -- Identity ------------------------------------------------------------
    phone_number: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    """E.164, normalised on the way in. Stored in the clear and uniquely indexed
    because sign-in has to find an account from the number alone; there is no
    second factor to look it up by. Redaction keeps it out of logs.

    **Nullable, and null means purged.** An erased account holds no number at all,
    which is both the honest outcome of a Data Protection Act erasure and simpler
    than the alternative: the first implementation wrote a `purged:<uuid>`
    tombstone to keep the unique index satisfied, which did not fit the column and
    made the nightly retention job fail silently. PostgreSQL permits many nulls in
    a unique index, so nothing needs to be invented to keep rows distinct.

    Every live account has one. Null is unreachable through the API, because a
    purged account cannot authenticate."""

    phone_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    display_name: Mapped[str] = mapped_column(String(50))

    status: Mapped[UserStatus] = mapped_column(
        _user_status, default=UserStatus.active, server_default=UserStatus.active.value
    )

    adult_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    """When the user confirmed being 18 or over. The timestamp, not a boolean: the
    Data Protection Act wants evidence of when a declaration was made, and a bare
    `true` records nothing useful."""

    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    """Drives the active-user counts the radius engine is built on."""

    avatar_media_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="SET NULL", use_alter=True), nullable=True
    )
    """The current avatar. Declared in Phase 1 without a constraint because the
    media table did not exist; Phase 2 adds the foreign key — exactly the cheap,
    constraint-only migration the brief asked the data model to allow for.

    `SET NULL`, so removing an asset clears the avatar rather than failing.
    `use_alter`, because users and media_assets reference each other: the
    constraint is added after both tables exist, which is also how migration
    0005 created it."""

    # -- Home location -------------------------------------------------------
    # Four resolutions of the same cell, denormalised on purpose. Deriving the
    # parents in SQL is not possible (H3 is computed in Python), and recomputing
    # them per query would make every k-anonymity and density lookup a round trip.
    home_cell_res9: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    home_cell_res8: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    home_cell_res7: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    home_cell_res6: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    home_centroid: Mapped[object | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=True), nullable=True
    )
    """The centroid of the res-9 cell, never the coordinates the client sent. Every
    proximity query runs against this, so two users in the same cell are exactly
    the same distance from everything — which is the privacy guarantee."""

    home_area_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("areas.id", ondelete="SET NULL"), nullable=True
    )
    """Resolved when the location is set. Nullable because a user may be outside
    every seeded area, which is normal before a town is seeded."""

    home_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    home_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Enforces the change cooldown. Separate from `home_set_at` so that setting a
    home for the first time does not start a cooldown."""

    # -- Verified-neighbour badge (computed in Phase 8) ----------------------
    home_presence_mask: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    """A 14-bit rolling window: bit 0 is the most recent day, 1 meaning the user
    was seen in their home area that day. This is the entire location history the
    system keeps — fourteen bits instead of fourteen positions."""

    home_presence_updated_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    """The day the mask was last rolled, so a gap of several days shifts by the
    right number of bits instead of one."""

    is_verified_neighbour: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    verified_neighbour_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    badge_locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """A moderator revoked the badge and locked it: the daily recomputation leaves
    it off until a moderator unlocks it."""

    # -- Trust and safety (Phase 8) -------------------------------------------
    suspended_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """When a suspension lifts on its own. Null with status `suspended` means until
    a moderator lifts it."""
    restricted_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """Restricted: held to new-account limits, no message requests or job posts,
    and posts held for review — slowed down, not silenced."""
    restriction_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # -- Reputation (populated in Phase 6) -----------------------------------
    rating_average: Mapped[float | None] = mapped_column(Numeric(3, 2), nullable=True)
    rating_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # -- Data rights ---------------------------------------------------------
    deletion_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """Start of the grace period. The purge worker reads this, not `deleted_at`."""

    __table_args__ = (
        CheckConstraint(
            "home_presence_mask >= 0 AND home_presence_mask < 16384",
            name="user_home_presence_mask_is_14_bits",
        ),
        CheckConstraint("rating_count >= 0", name="user_rating_count_non_negative"),
        CheckConstraint(
            "rating_average IS NULL OR (rating_average >= 1 AND rating_average <= 5)",
            name="user_rating_average_in_range",
        ),
        # Either the whole home location is set or none of it is. A row with a
        # cell but no centroid would silently vanish from every proximity query.
        CheckConstraint(
            "(home_cell_res9 IS NULL) = (home_centroid IS NULL)",
            name="user_home_cell_and_centroid_together",
        ),
        # The active-user count filters on status and recency together.
        Index("ix_users_status_last_active_at", "status", "last_active_at"),
        # Density aggregation groups by res-8 cell over active users.
        Index("ix_users_home_cell_res8_last_active_at", "home_cell_res8", "last_active_at"),
        # The retention job scans for grace periods that have run out.
        Index("ix_users_deletion_requested_at", "deletion_requested_at"),
        Index(
            "ix_users_display_name_trgm",
            "display_name",
            postgresql_using="gin",
            postgresql_ops={"display_name": "gin_trgm_ops"},
        ),
    )

    @property
    def has_home_location(self) -> bool:
        return self.home_cell_res9 is not None

    @property
    def is_active_account(self) -> bool:
        return self.status == UserStatus.active and self.deleted_at is None

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<User {self.id} {self.display_name!r}>"


class Consent(Base, UUIDPrimaryKeyMixin):
    """One row per consent decision, append-only.

    Append-only rather than a mutable flag per type, because the Data Protection
    Act's question is not "does this user consent?" but "what did they agree to,
    when, and to which version of the document?" — and a withdrawal is itself a
    fact worth keeping. Current state is the newest row per `(user, type)`.
    """

    __tablename__ = "consents"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    consent_type: Mapped[ConsentType] = mapped_column(_consent_type)

    granted: Mapped[bool] = mapped_column(Boolean)
    """False rows are withdrawals, and are kept as evidence of the withdrawal."""

    document_version: Mapped[str] = mapped_column(String(32))
    """Which version of the terms or policy this decision was about."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    __table_args__ = (
        Index(
            "ix_consents_user_id_consent_type_created_at", "user_id", "consent_type", "created_at"
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<Consent {self.consent_type} granted={self.granted}>"


class Block(Base, UUIDPrimaryKeyMixin):
    """One user blocking another.

    Blocking is symmetric in effect but not in record: A blocking B hides each from
    the other, but only A's row exists, so unblocking is A's to do. Every module
    from Phase 2 onwards filters through `users.service.blocked_user_ids`, which is
    the single implementation of that rule.
    """

    __tablename__ = "blocks"

    blocker_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    blocked_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("blocker_id", "blocked_id", name="uq_blocks_blocker_id_blocked_id"),
        CheckConstraint("blocker_id <> blocked_id", name="block_not_self"),
    )

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<Block {self.blocker_id} -> {self.blocked_id}>"


class Vouch(Base, UUIDPrimaryKeyMixin):
    """A verified neighbour vouching for someone in the same area.

    Two vouches from verified neighbours earn the badge (the brief's second route),
    so a vouch is only counted while its giver is verified and still lives in the
    same res-7 area as the person vouched for — checked when the badge is computed,
    not frozen when the vouch was given.
    """

    __tablename__ = "vouches"

    voucher_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    vouchee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("voucher_id", "vouchee_id", name="uq_vouches_voucher_id_vouchee_id"),
        CheckConstraint("voucher_id <> vouchee_id", name="vouch_not_for_self"),
    )
