"""Message visibility, deletable content, device sessions, message reports

Four changes, all written by hand because autogenerate sees none of the check
constraint changes:

* `conversation_members.visible_from_seq` — a member added to a group chat later
  does not see what was said before they joined.
* `messages` kind constraint relaxed for deleted rows. Deleting a message clears
  its body or image, which the old constraint forbade.
* `device_tokens.device_session_id` — pushes go only to tokens whose sign-in is
  still live, so signing out of a shared phone stops its notifications. Existing
  rows (there are none outside development) keep a null and are never pushed to.
* `reports.target_type` gains `message`.

Revision ID: 0008
Revises: 0007
Created: 2026-10-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Literal, as in 0006: a migration must mean the same thing after the enum grows.
REPORT_TARGET_TYPES_BEFORE = (
    "post",
    "comment",
    "story",
    "user",
    "listing",
    "service_post",
    "event",
    "group",
)
REPORT_TARGET_TYPES_AFTER = (
    "post",
    "comment",
    "story",
    "user",
    "message",
    "listing",
    "service_post",
    "event",
    "group",
)

MESSAGE_CONTENT_BEFORE = (
    "(kind = 'text' AND body IS NOT NULL)"
    " OR (kind = 'image' AND media_id IS NOT NULL)"
    " OR (kind = 'system' AND body IS NOT NULL AND sender_id IS NULL)"
)
MESSAGE_CONTENT_AFTER = (
    "(kind = 'text' AND (body IS NOT NULL OR deleted_at IS NOT NULL))"
    " OR (kind = 'image' AND (media_id IS NOT NULL OR deleted_at IS NOT NULL))"
    " OR (kind = 'system' AND body IS NOT NULL AND sender_id IS NULL)"
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _replace_check(table: str, suffix: str, condition: str) -> None:
    op.drop_constraint(op.f(f"ck_{table}_{suffix}"), table, type_="check")
    op.create_check_constraint(suffix, table, condition)


def upgrade() -> None:
    op.add_column(
        "conversation_members",
        sa.Column("visible_from_seq", sa.BigInteger(), server_default="0", nullable=False),
    )

    _replace_check("messages", "message_kind_matches_content", MESSAGE_CONTENT_AFTER)

    op.add_column("device_tokens", sa.Column("device_session_id", sa.Uuid(), nullable=True))
    op.create_index(
        op.f("ix_device_tokens_device_session_id"), "device_tokens", ["device_session_id"]
    )
    op.create_foreign_key(
        op.f("fk_device_tokens_device_session_id_device_sessions"),
        "device_tokens",
        "device_sessions",
        ["device_session_id"],
        ["id"],
        ondelete="CASCADE",
    )

    _replace_check("reports", "report_target_type", _in("target_type", REPORT_TARGET_TYPES_AFTER))


def downgrade() -> None:
    # Fails if a message report exists, which is correct: dropping the value would
    # orphan evidence. Resolve or remove those reports first.
    _replace_check("reports", "report_target_type", _in("target_type", REPORT_TARGET_TYPES_BEFORE))

    op.drop_constraint(
        op.f("fk_device_tokens_device_session_id_device_sessions"),
        "device_tokens",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_device_tokens_device_session_id"), table_name="device_tokens")
    op.drop_column("device_tokens", "device_session_id")

    # Likewise fails while deleted messages exist with their content cleared.
    _replace_check("messages", "message_kind_matches_content", MESSAGE_CONTENT_BEFORE)

    op.drop_column("conversation_members", "visible_from_seq")
