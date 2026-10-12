"""Kenyan phone number normalisation.

One number can be written at least four ways — `0712345678`, `712345678`,
`254712345678`, `+254712345678` — and a user will type whichever they are used
to. All of them must resolve to one stored value, because the phone number *is*
the account: two spellings becoming two accounts would split someone's identity
in half, and would let the same person collect two sets of free posting limits.

Everything is stored in E.164 (`+254712345678`). Normalisation happens at the
edge, before any lookup.
"""

from __future__ import annotations

import phonenumbers

from app.core.errors import BadRequestError

DEFAULT_REGION = "KE"

# Kenyan mobile numbers are 07xx and, since the range expansion, 01xx. Safaricom,
# Airtel and Telkom all sit in these. Landlines and premium ranges are rejected:
# this is a mobile product and an OTP to a landline cannot arrive.
MOBILE_TYPES = frozenset(
    {
        phonenumbers.PhoneNumberType.MOBILE,
        phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE,
    }
)


def normalise_phone_number(raw: str, *, region: str = DEFAULT_REGION) -> str:
    """Parse and normalise a phone number to E.164, or raise `BadRequestError`.

    `region` defaults to Kenya so bare local forms parse, but an explicitly
    international number is honoured as given — the launch market is Kenya, not
    the only possible market, and nothing here assumes otherwise.

    The error message never echoes the number back: error payloads are logged, and
    a phone number is personal data.
    """
    candidate = (raw or "").strip().replace(" ", "").replace("-", "")
    if not candidate:
        raise BadRequestError("A phone number is required")

    try:
        parsed = phonenumbers.parse(candidate, region)
    except phonenumbers.NumberParseException as exc:
        raise BadRequestError("That phone number could not be understood") from exc

    if not phonenumbers.is_valid_number(parsed):
        raise BadRequestError("That phone number is not valid")

    if phonenumbers.number_type(parsed) not in MOBILE_TYPES:
        raise BadRequestError(
            "A mobile number is required, because the verification code is sent by SMS"
        )

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def is_kenyan(e164: str) -> bool:
    """Whether a normalised number is Kenyan.

    Not used to reject anyone. It exists so later phases can reason about reach —
    M-Pesa works on Kenyan numbers, and SMS costs differ by destination.
    """
    try:
        parsed = phonenumbers.parse(e164, None)
    except phonenumbers.NumberParseException:
        return False
    return phonenumbers.region_code_for_number(parsed) == DEFAULT_REGION


def mask_for_display(e164: str) -> str:
    """A number with its middle hidden, for "we sent a code to +2547••••678".

    The client usually knows the number it just submitted, but a confirmation
    screen after a redirect or a retry may not, and showing the whole number again
    puts it somewhere it does not need to be.
    """
    if len(e164) < 7:
        return "•" * len(e164)
    return f"{e164[:5]}{'•' * (len(e164) - 8)}{e164[-3:]}"
