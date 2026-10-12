"""External service providers, each behind an interface.

Every outbound integration is reached through a Protocol with at least two
implementations: the real one, and a fake the tests use. Tests never touch the
network, and swapping a vendor is a configuration change rather than a refactor.

- `sms` — Africa's Talking, plus a console provider for development and tests.
- `push` — Firebase Cloud Messaging, plus a fake (Phase 3).
- `storage` — S3-compatible object storage, plus a local fake (Phase 2).
- `payments` — M-Pesa Daraja, plus a fake (Phase 9).
"""
