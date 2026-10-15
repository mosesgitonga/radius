"""The phone and device ban lists.

Both hold keyed hashes, never the values: the list must not itself be a list of
people's numbers. The key is `BAN_LIST_KEY` (falling back to `SECRET_KEY`), and the
kind of value is part of the hashed input, so a phone number and an installation
ID never collide.

Sign-in from a banned number or device fails exactly as a wrong code does. A
banned number is not even sent a code — the response to the request is the same,
so the ban is not disclosed, and no SMS is paid for.
"""

from __future__ import annotations

import hashlib
import hmac

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.moderation.models import BannedDevice, BannedPhone


def _hash(kind: str, value: str, settings: Settings) -> str:
    message = f"{kind}:{value.strip()}".encode()
    return hmac.new(settings.ban_list_secret, message, hashlib.sha256).hexdigest()


def phone_hash(phone_number: str, settings: Settings) -> str:
    """Of the normalised E.164 number."""
    return _hash("phone", phone_number, settings)


def device_hash(device_id: str, settings: Settings) -> str:
    return _hash("device", device_id, settings)


async def is_phone_banned(session: AsyncSession, phone_number: str, settings: Settings) -> bool:
    found = await session.scalar(
        select(BannedPhone.id).where(BannedPhone.phone_hash == phone_hash(phone_number, settings))
    )
    return found is not None


async def is_device_banned(session: AsyncSession, device_id_hash: str | None) -> bool:
    if not device_id_hash:
        return False
    found = await session.scalar(
        select(BannedDevice.id).where(BannedDevice.device_hash == device_id_hash)
    )
    return found is not None
