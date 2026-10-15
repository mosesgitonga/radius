"""Trust and safety

Staff accounts and sessions, moderator warnings, the phone and device ban lists,
vouches, the users' suspension, restriction and badge-lock columns, and the
installation-ID hash on device sessions. The audit-action constraint widens for
the staff actions.

Reviewed by hand: the ban lists hold keyed hashes only, a vouch cannot be for
oneself and is unique per pair, staff rows reference only staff, and autogenerate's
duplicate enum constraint is removed.

Revision ID: 0013
Revises: 0012
Created: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUDIT_ACTIONS_BEFORE = (
    "account_created",
    "account_deletion_requested",
    "account_deletion_cancelled",
    "account_purged",
    "data_exported",
    "consent_granted",
    "consent_withdrawn",
    "profile_updated",
    "sessions_revoked",
    "refresh_token_reuse_detected",
    "home_location_set",
    "home_location_changed",
    "location_spoof_suspected",
    "user_blocked",
    "user_unblocked",
    "report_submitted",
    "content_auto_hidden",
    "content_flagged",
    "group_deleted",
    "group_member_removed",
    "group_member_banned",
    "group_member_unbanned",
    "group_role_changed",
    "group_ownership_transferred",
    "group_post_removed",
)
AUDIT_ACTIONS_AFTER = (
    *AUDIT_ACTIONS_BEFORE,
    "staff_signed_in",
    "staff_sign_in_failed",
    "staff_account_created",
    "staff_account_updated",
    "content_removed",
    "content_restored",
    "report_resolved",
    "user_warned",
    "user_suspended",
    "user_unsuspended",
    "user_banned",
    "user_unbanned",
    "user_restricted",
    "user_unrestricted",
    "badge_awarded",
    "badge_revoked",
    "phone_banned",
    "phone_unbanned",
    "device_banned",
    "device_unbanned",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _replace_check(table: str, suffix: str, condition: str) -> None:
    op.drop_constraint(op.f(f"ck_{table}_{suffix}"), table, type_="check")
    op.create_check_constraint(suffix, table, condition)


def upgrade() -> None:
    op.create_table(
        "staff_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "moderator",
                "admin",
                name="staff_role",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("totp_secret", sa.String(length=64), nullable=False),
        sa.Column("totp_last_step", sa.Integer(), nullable=True),
        sa.Column("failed_logins", sa.Integer(), server_default="0", nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["staff_accounts.id"],
            name=op.f("fk_staff_accounts_created_by_id_staff_accounts"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_accounts")),
        sa.UniqueConstraint("email", name=op.f("uq_staff_accounts_email")),
    )
    op.create_table(
        "staff_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("staff_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["staff_id"],
            ["staff_accounts.id"],
            name=op.f("fk_staff_sessions_staff_id_staff_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_sessions")),
    )
    op.create_index("ix_staff_sessions_expires_at", "staff_sessions", ["expires_at"], unique=False)
    op.create_index(
        op.f("ix_staff_sessions_staff_id"), "staff_sessions", ["staff_id"], unique=False
    )
    op.create_table(
        "banned_devices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("device_hash", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=True),
        sa.Column("staff_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["staff_id"],
            ["staff_accounts.id"],
            name=op.f("fk_banned_devices_staff_id_staff_accounts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_banned_devices_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_banned_devices")),
        sa.UniqueConstraint("device_hash", name=op.f("uq_banned_devices_device_hash")),
    )
    op.create_index(op.f("ix_banned_devices_user_id"), "banned_devices", ["user_id"], unique=False)
    op.create_table(
        "banned_phones",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("phone_hash", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=True),
        sa.Column("staff_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["staff_id"],
            ["staff_accounts.id"],
            name=op.f("fk_banned_phones_staff_id_staff_accounts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_banned_phones_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_banned_phones")),
        sa.UniqueConstraint("phone_hash", name=op.f("uq_banned_phones_phone_hash")),
    )
    op.create_index(op.f("ix_banned_phones_user_id"), "banned_phones", ["user_id"], unique=False)
    op.create_table(
        "vouches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("voucher_id", sa.Uuid(), nullable=False),
        sa.Column("vouchee_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("voucher_id <> vouchee_id", name=op.f("ck_vouches_vouch_not_for_self")),
        sa.ForeignKeyConstraint(
            ["vouchee_id"],
            ["users.id"],
            name=op.f("fk_vouches_vouchee_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["voucher_id"],
            ["users.id"],
            name=op.f("fk_vouches_voucher_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vouches")),
        sa.UniqueConstraint("voucher_id", "vouchee_id", name="uq_vouches_voucher_id_vouchee_id"),
    )
    op.create_index(op.f("ix_vouches_vouchee_id"), "vouches", ["vouchee_id"], unique=False)
    op.create_index(op.f("ix_vouches_voucher_id"), "vouches", ["voucher_id"], unique=False)
    op.create_table(
        "user_warnings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("staff_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column("report_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["reports.id"],
            name=op.f("fk_user_warnings_report_id_reports"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["staff_id"],
            ["staff_accounts.id"],
            name=op.f("fk_user_warnings_staff_id_staff_accounts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_warnings_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_warnings")),
    )
    op.create_index(op.f("ix_user_warnings_user_id"), "user_warnings", ["user_id"], unique=False)
    op.add_column(
        "device_sessions", sa.Column("device_id_hash", sa.String(length=64), nullable=True)
    )
    op.create_index(
        op.f("ix_device_sessions_device_id_hash"),
        "device_sessions",
        ["device_id_hash"],
        unique=False,
    )
    op.add_column("users", sa.Column("badge_locked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("suspended_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("restricted_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("restriction_reason", sa.String(length=500), nullable=True))

    _replace_check("audit_events", "audit_action", _in("action", AUDIT_ACTIONS_AFTER))


def downgrade() -> None:
    # Fails while staff audit events exist, which is right: narrowing would orphan them.
    _replace_check("audit_events", "audit_action", _in("action", AUDIT_ACTIONS_BEFORE))
    op.drop_column("users", "restriction_reason")
    op.drop_column("users", "restricted_until")
    op.drop_column("users", "suspended_until")
    op.drop_column("users", "badge_locked_at")
    op.drop_index(op.f("ix_device_sessions_device_id_hash"), table_name="device_sessions")
    op.drop_column("device_sessions", "device_id_hash")
    op.drop_index(op.f("ix_user_warnings_user_id"), table_name="user_warnings")
    op.drop_table("user_warnings")
    op.drop_index(op.f("ix_vouches_voucher_id"), table_name="vouches")
    op.drop_index(op.f("ix_vouches_vouchee_id"), table_name="vouches")
    op.drop_table("vouches")
    op.drop_index(op.f("ix_banned_phones_user_id"), table_name="banned_phones")
    op.drop_table("banned_phones")
    op.drop_index(op.f("ix_banned_devices_user_id"), table_name="banned_devices")
    op.drop_table("banned_devices")
    op.drop_index(op.f("ix_staff_sessions_staff_id"), table_name="staff_sessions")
    op.drop_index("ix_staff_sessions_expires_at", table_name="staff_sessions")
    op.drop_table("staff_sessions")
    op.drop_table("staff_accounts")
