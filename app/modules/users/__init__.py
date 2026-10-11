"""Users module.

Phase 1 — profiles, consent records, blocking, data export and deletion.

Each module holds `models.py`, `schemas.py`, `service.py`, and `router.py`.
Routers stay thin; the logic lives in the service layer, which is also how other
modules call in — never through another module's router.
"""
