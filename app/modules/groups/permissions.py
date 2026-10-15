"""Who may do what in a group. The one table every group route checks.

Two rules, applied together:

1. **A role needs a minimum rank for each action** — the table below.
2. **Nobody acts on someone of equal or higher rank.** An admin cannot remove,
   ban, mute or demote another admin, and nobody can do any of it to the owner.

Kept as data rather than scattered `if role == ...` checks, so the matrix in
`PROGRESS.md` and the code are the same thing, and a test can walk every cell.
"""

from __future__ import annotations

from enum import StrEnum

from app.core.errors import ForbiddenError
from app.modules.groups.models import ROLE_RANK, GroupRole


class GroupAction(StrEnum):
    participate = "participate"
    """Post, comment and react."""
    review_posts = "review_posts"
    """Approve or reject pending posts."""
    remove_posts = "remove_posts"
    mute_members = "mute_members"
    pin_posts = "pin_posts"
    review_join_requests = "review_join_requests"
    invite = "invite"
    manage_invite_links = "manage_invite_links"
    remove_members = "remove_members"
    ban_members = "ban_members"
    edit_settings = "edit_settings"
    enable_chat = "enable_chat"
    manage_events = "manage_events"
    """Organise events on the group's behalf, including group-only ones."""
    appoint_moderators = "appoint_moderators"
    appoint_admins = "appoint_admins"
    transfer_ownership = "transfer_ownership"
    delete_group = "delete_group"


MINIMUM_ROLE: dict[GroupAction, GroupRole] = {
    GroupAction.participate: GroupRole.member,
    GroupAction.review_posts: GroupRole.moderator,
    GroupAction.remove_posts: GroupRole.moderator,
    GroupAction.mute_members: GroupRole.moderator,
    GroupAction.pin_posts: GroupRole.admin,
    GroupAction.review_join_requests: GroupRole.admin,
    GroupAction.invite: GroupRole.admin,
    GroupAction.manage_invite_links: GroupRole.admin,
    GroupAction.remove_members: GroupRole.admin,
    GroupAction.ban_members: GroupRole.admin,
    GroupAction.edit_settings: GroupRole.admin,
    GroupAction.enable_chat: GroupRole.admin,
    GroupAction.manage_events: GroupRole.admin,
    GroupAction.appoint_moderators: GroupRole.admin,
    GroupAction.appoint_admins: GroupRole.owner,
    GroupAction.transfer_ownership: GroupRole.owner,
    GroupAction.delete_group: GroupRole.owner,
}


def can(role: GroupRole, action: GroupAction) -> bool:
    return ROLE_RANK[role] >= ROLE_RANK[MINIMUM_ROLE[action]]


def outranks(actor: GroupRole, target: GroupRole) -> bool:
    return ROLE_RANK[actor] > ROLE_RANK[target]


def require(role: GroupRole, action: GroupAction) -> None:
    """Refuse with 403. Only ever called for an active member of a group they can
    see — refusals to *see* a group are 404s, decided earlier."""
    if not can(role, action):
        raise ForbiddenError(
            "Your role in this group does not allow that",
            details={"action": action.value, "required_role": MINIMUM_ROLE[action].value},
        )


def require_outranks(actor: GroupRole, target: GroupRole) -> None:
    if not outranks(actor, target):
        raise ForbiddenError(
            "You cannot do that to someone of your own rank or above",
            details={"your_role": actor.value, "their_role": target.value},
        )
