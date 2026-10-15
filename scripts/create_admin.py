"""Create the first admin.

    docker compose exec api python -m scripts.create_admin --email you@example.com --name "You"

Staff accounts are made by admins through the API, which leaves the first one.
This creates it, and only while no admin exists — afterwards it refuses, so the
script cannot be used to mint a back-door admin on a running system.

Prints a temporary password and an authenticator secret, once, to the terminal
and nowhere else. Change the password after the first sign-in.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.db import dispose_engine, session_scope
from app.modules.staff import service as staff_service
from app.modules.staff.models import StaffAccount, StaffRole


def _say(line: str) -> None:
    # Straight to the terminal, never through logging: these lines carry
    # credentials, and logs are shipped and kept.
    sys.stdout.write(f"{line}\n")


async def _create(email: str, name: str) -> int:
    settings = get_settings()
    async with session_scope() as session:
        admins = await session.scalar(
            select(func.count(StaffAccount.id)).where(StaffAccount.role == StaffRole.admin)
        )
        if admins:
            _say("An admin already exists. Create further staff through the admin API.")
            return 1
        issued = await staff_service.create_account(
            session,
            email=email,
            display_name=name,
            role=StaffRole.admin,
            created_by=None,
            settings=settings,
        )
        await session.commit()
    await dispose_engine()
    _say(f"Admin created: {issued.account.email}")
    _say(f"Temporary password: {issued.password}")
    _say(f"Authenticator secret: {issued.totp_secret}")
    _say(f"Authenticator URI (for a QR code): {issued.totp_uri}")
    _say("Shown once. Sign in at POST /v1/admin/auth/sign-in, then change the password.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    return asyncio.run(_create(args.email, args.name))


if __name__ == "__main__":
    sys.exit(main())
