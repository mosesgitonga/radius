"""Enforce enum values in the database

Every status, reason, kind and role column was declared as a constrained
VARCHAR, and decision 1.16 says so — but none of them had a CHECK constraint.
SQLAlchemy has not emitted one for non-native enums by default since 1.4, and
`create_constraint=True` was never set. The database accepted any string: a direct
insert of `status = 'definitely_not_a_status'` into `users` succeeded. The ORM
would only have noticed on the next read of that row, by failing it.

This adds the constraints to every enum column created in revisions 0002-0005.
Columns created from here on get them from `app.core.models.db_enum`.

Existing rows are valid — every one was written through the ORM, which validates —
so the constraints are added directly rather than as NOT VALID and validated later.

Revision ID: 0006
Revises: 0005
Created: 2026-10-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, constraint suffix, column, allowed values) as of this revision.
# Literal on purpose: a migration must mean the same thing when it is run in a
# year, after the Python enums have grown.
CONSTRAINTS: list[tuple[str, str, str, tuple[str, ...]]] = [
    (
        "audit_events",
        "audit_action",
        "action",
        (
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
        ),
    ),
    (
        "audit_events",
        "audit_actor_type",
        "actor_type",
        (
            "user",
            "moderator",
            "system",
        ),
    ),
    (
        "media_assets",
        "media_purpose",
        "purpose",
        (
            "avatar",
            "post",
            "story",
            "message",
            "listing",
            "event_cover",
        ),
    ),
    (
        "media_assets",
        "media_status",
        "status",
        (
            "pending",
            "processing",
            "ready",
            "rejected",
        ),
    ),
    (
        "users",
        "user_status",
        "status",
        (
            "active",
            "suspended",
            "banned",
            "deletion_pending",
        ),
    ),
    (
        "consents",
        "consent_type",
        "consent_type",
        (
            "terms",
            "privacy_policy",
            "location_use",
            "marketing",
        ),
    ),
    (
        "device_sessions",
        "session_revocation_reason",
        "revoked_reason",
        (
            "logout",
            "logout_all",
            "rotated",
            "reuse_detected",
            "expired",
            "max_sessions",
            "account_deleted",
            "admin_action",
        ),
    ),
    (
        "posts",
        "content_status",
        "status",
        (
            "visible",
            "hidden_pending_review",
            "removed",
        ),
    ),
    (
        "reports",
        "report_reason",
        "reason",
        (
            "spam",
            "scam",
            "job_scam",
            "harassment",
            "hate_speech",
            "violence",
            "sexual_content",
            "prohibited_item",
            "impersonation",
            "misinformation",
            "other",
        ),
    ),
    (
        "reports",
        "report_status",
        "status",
        (
            "pending",
            "upheld",
            "dismissed",
        ),
    ),
    (
        "reports",
        "report_target_type",
        "target_type",
        (
            "post",
            "comment",
            "story",
            "user",
            "listing",
            "service_post",
            "event",
            "group",
        ),
    ),
    (
        "stories",
        "content_status",
        "status",
        (
            "visible",
            "hidden_pending_review",
            "removed",
        ),
    ),
    (
        "stories",
        "story_kind",
        "kind",
        (
            "image",
            "text",
        ),
    ),
    (
        "comments",
        "content_status",
        "status",
        (
            "visible",
            "hidden_pending_review",
            "removed",
        ),
    ),
]


def _condition(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    for table, suffix, column, values in CONSTRAINTS:
        # The naming convention prefixes `ck_<table>_`, so pass only the suffix.
        op.create_check_constraint(suffix, table, _condition(column, values))


def downgrade() -> None:
    for table, suffix, _column, _values in reversed(CONSTRAINTS):
        op.drop_constraint(op.f(f"ck_{table}_{suffix}"), table, type_="check")
