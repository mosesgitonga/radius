"""Auth module.

Phase 1 — phone OTP, JWT access and refresh tokens, device sessions.

Each module holds `models.py`, `schemas.py`, `service.py`, and `router.py`.
Routers stay thin; the logic lives in the service layer, which is also how other
modules call in — never through another module's router.
"""
