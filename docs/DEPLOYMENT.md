# Deployment

How to run MyRadius on a single VPS: Caddy for TLS, the API and worker containers,
PostgreSQL with PostGIS, and Redis, all from `docker-compose.prod.yml`. Media lives in
Cloudflare R2, SMS goes through Africa's Talking, and push goes through FCM.

This is sized for launch in one town. Its limits, and what to change first when they
are reached, are at the end.

## 1. The server

- **Size:** 4 vCPU, 8 GB RAM, 80 GB SSD. PostgreSQL gets most of the memory.
- **OS:** Ubuntu 24.04 LTS, with Docker Engine and the Compose plugin from Docker's
  own repository.
- **Firewall:** only 22 (SSH, keys only), 80 and 443 are open:

  ```sh
  ufw default deny incoming
  ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp
  ufw enable
  ```

  Docker publishes ports past `ufw`, which is why only Caddy publishes any. PostgreSQL,
  Redis, the API and the worker are reachable on the internal Compose network only.
- **Time:** UTC, with NTP on (the Ubuntu default). Staff sign-in codes are time-based
  and are refused if the clock drifts more than 30 seconds.
- **DNS:** an `A` (and `AAAA`) record for the API domain, pointing at the server,
  before the first start. Caddy needs it to get a certificate.

## 2. Logs: 30 days, as the data map promises

The containers log to journald. Docker's default driver keeps logs forever, which
would break the retention the data map promises. Cap journald on the host:

```ini
# /etc/systemd/journald.conf.d/myradius.conf
[Journal]
MaxRetentionSec=30day
SystemMaxUse=4G
```

```sh
systemctl restart systemd-journald
```

Read a service's logs with `docker compose -f docker-compose.prod.yml logs api`, or
`journalctl CONTAINER_TAG=myradius-api`. Application logs are already redacted:
no phone numbers, tokens, coordinates or query strings.

## 3. Configuration and secrets

```sh
git clone https://github.com/mosesgitonga/radius.git /srv/myradius && cd /srv/myradius
cp .env.example .env.production && chmod 600 .env.production
mkdir -m 700 secrets backups
```

`.env.production` holds every secret. It is never committed (`.gitignore`) and never
copied into an image (`.dockerignore`). Settings that must change from the template:

| Setting | Value |
| --- | --- |
| `APP_ENV` | `production` (the app refuses development defaults when this is set) |
| `DEBUG` | `false` |
| `SECRET_KEY` | `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `BAN_LIST_KEY` | Another value made the same way. Keep it when rotating `SECRET_KEY`. |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | A long random password |
| `DATABASE_URL` | `postgresql+asyncpg://USER:PASSWORD@postgres:5432/DB` |
| `REDIS_URL` | `redis://redis:6379/0` |
| `LOG_FORMAT` | `json` |
| `CORS_ORIGINS` | Empty for the mobile app. Set it only for a browser client. |
| `SMS_PROVIDER`, `AFRICASTALKING_*` | `africastalking`, with the account's credentials |
| `STORAGE_PROVIDER`, `STORAGE_*` | `s3`, with R2's endpoint, bucket and keys |
| `PUSH_PROVIDER`, `FCM_CREDENTIALS_FILE` | `fcm`, `/run/secrets/myradius/fcm.json` |
| `SENTRY_DSN` | The project's DSN, or empty to leave error tracking off |
| `METRICS_TOKEN` | A random token, if Prometheus will scrape the API |
| `WORKER_METRICS_PORT` | `9100`, if Prometheus will scrape the worker |
| `API_DOMAIN`, `ACME_EMAIL` | For Caddy: the API's domain, and where Let's Encrypt writes |
| `BACKUP_BUCKET`, `BACKUP_ENDPOINT_URL`, `BACKUP_ACCESS_KEY_ID`, `BACKUP_SECRET_ACCESS_KEY` | A **separate, private** R2 bucket and keys, for off-site dumps |

The Firebase service-account file goes in `secrets/`, readable only by the container
user (UID 1001):

```sh
install -m 600 -o 1001 -g 1001 /path/to/firebase.json secrets/fcm.json
```

## 4. First start

```sh
docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build
docker compose -f docker-compose.prod.yml ps        # all healthy?
curl https://API_DOMAIN/health
```

The API applies migrations as it starts. Then, once:

```sh
# The areas whose names labels are made from: one GeoJSON file per town.
docker compose -f docker-compose.prod.yml exec api python -m scripts.seed_areas seeds/areas/nyeri.geojson

# The first admin. Prints a temporary password and an authenticator secret, once.
docker compose -f docker-compose.prod.yml exec api python -m scripts.create_admin --email you@example.com --name "Your Name"
```

Sign in at `POST /v1/admin/auth/sign-in`, change the password at
`POST /v1/admin/me/password`, and create moderator accounts at `POST /v1/admin/staff`.
The bootstrap script refuses to run again once an admin exists.

## 5. Updating

```sh
git pull
docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build
```

The API migrates before it serves, and every migration has been run down and up on
an empty database in CI. Take a backup first (`scripts/backup.sh`) when a release
includes a migration.

## 6. Backups, and proving they restore

`scripts/backup.sh` dumps the database (`pg_dump`, custom format), checks the dump
reads back, keeps 14 days locally, and copies each dump to the backup bucket when
`BACKUP_BUCKET` is set. Schedule it from root's crontab:

```cron
15 1 * * * cd /srv/myradius && scripts/backup.sh >> /var/log/myradius-backup.log 2>&1
45 1 * * 0 cd /srv/myradius && scripts/restore_test.sh >> /var/log/myradius-backup.log 2>&1
```

`scripts/restore_test.sh` restores the newest dump into a scratch database next to the
live one. It compares the migration revision and every table's row count, then drops
the scratch database, and exits non-zero on any difference. It runs weekly, at a
quiet hour, so rows written since the dump don't show up as differences.

To restore for real, onto a fresh server:

```sh
# Fetch the dump without starting the API, which would migrate an empty database.
docker compose -f docker-compose.prod.yml --env-file .env.production run --rm --no-deps -T api     python -m scripts.backup_offsite get myradius-YYYYMMDDTHHMMSSZ.dump > restore.dump
docker compose -f docker-compose.prod.yml --env-file .env.production up -d postgres
docker compose -f docker-compose.prod.yml exec -T postgres     sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error' < restore.dump
docker compose -f docker-compose.prod.yml --env-file .env.production up -d
```

The dumps are personal data. `backups/` is mode 700 and the bucket must be private.
They age out with the 14-day retention, so a purged account is gone from backups two
weeks after it is gone from the database. The data map states this.

**Redis is not backed up.** It holds only short-lived state: one-time codes, rate-limit
counters, presence, the job queue and the radius cache. Losing it signs nobody out
and loses no content. At worst it drops jobs that were queued and not yet run, and
the nightly sweeps catch those up.

## 7. Monitoring

- **Health:** `GET /health` (liveness) and `GET /ready` (database and Redis). Each
  container has a healthcheck, and Compose restarts anything that dies.
- **Errors:** Sentry, when `SENTRY_DSN` is set. Events are scrubbed by the log
  redaction rules, and carry no headers, query strings, bodies, local variables or
  IP addresses.
- **Metrics:** the API at `http://api:8000/metrics` (bearer `METRICS_TOKEN`) and the
  worker at `http://worker:9100/`, both on the internal network. Caddy returns 404
  for `/metrics` from outside. Alerts worth having from day one:
  - `myradius_worker_job_last_success_timestamp_seconds{task="refresh_density"}` older
    than 1 hour: radii are going stale.
  - The same for `send_event_reminders` (5 minutes), `run_retention` (26 hours),
    `recompute_badges` (26 hours).
  - p95 of `myradius_http_request_duration_seconds` above 500 ms for 10 minutes.
  - Rate of 5xx responses in `myradius_http_requests_total` above 1%.

## 8. Limits of this setup

One machine is one failure domain. Backups and the restore test bound the damage, at
the cost of up to a day's data and the time to rebuild. Once the launch town outgrows
one machine, change these first, in this order:

1. **Managed PostgreSQL**, with point-in-time recovery, in place of the container and
   nightly dumps.
2. **A second API host** behind a load balancer. Run migrations as a separate step
   (`RUN_MIGRATIONS=false`, then `entrypoint.sh migrate`), so two containers don't
   race on the same upgrade.
3. **Managed Redis.** The realtime hub already fans out across processes through
   Redis, so more API hosts need no code change.
