#!/usr/bin/env bash
#
# One-time setup of MyRadius on the shared VPS, as root. Safe to run again: it
# creates only what is missing and never overwrites a secret.
#
#   bash deploy/vps/bootstrap.sh          # from a checkout in /srv/myradius
#
# Makes, if missing:
#   - /srv/myradius (a clone of the public repository), secrets/ and backups/ (700);
#   - .env.production (600), from .env.example, for staging, with fresh random
#     secrets: SECRET_KEY, BAN_LIST_KEY, the database password, the storage keys;
#   - secrets/s3.json (600), the object-storage credentials matching it;
#   - /usr/local/bin/myradius-deploy, the deploy gate the CI key is forced to run;
#   - the nginx site for myradius.chatara.co (HTTP; certbot adds TLS later).
# Touches nothing that belongs to the other projects on this server.
set -euo pipefail

REPO=/srv/myradius
DOMAIN=myradius.chatara.co
ORIGIN=https://github.com/mosesgitonga/radius.git

if [[ ! -d "$REPO/.git" ]]; then
  git clone --quiet "$ORIGIN" "$REPO"
fi
cd "$REPO"
install -d -m 700 secrets backups

random() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }

if [[ ! -f .env.production ]]; then
  echo "bootstrap: writing .env.production (staging, fresh secrets)"
  storage_secret="$(random 32)"
  db_password="$(random 24)"
  STORAGE_SECRET="$storage_secret" DB_PASSWORD="$db_password" \
  SECRET_KEY="$(random 48)" BAN_LIST_KEY="$(random 48)" DOMAIN="$DOMAIN" \
  python3 - <<'PY'
import os, pathlib

overrides = {
    "APP_ENV": "staging",
    "DEBUG": "false",
    "SECRET_KEY": os.environ["SECRET_KEY"],
    "BAN_LIST_KEY": os.environ["BAN_LIST_KEY"],
    "POSTGRES_USER": "myradius",
    "POSTGRES_PASSWORD": os.environ["DB_PASSWORD"],
    "POSTGRES_DB": "myradius",
    "DATABASE_URL": f"postgresql+asyncpg://myradius:{os.environ['DB_PASSWORD']}@postgres:5432/myradius",
    "REDIS_URL": "redis://redis:6379/0",
    "LOG_FORMAT": "json",
    "CORS_ORIGINS": "",
    "TRUST_FORWARDED_FOR": "true",
    # Staging: sign-in codes are written to the API log instead of sent by SMS.
    "SMS_PROVIDER": "console",
    "STORAGE_PROVIDER": "s3",
    "STORAGE_ENDPOINT_URL": "http://storage:8333",
    "STORAGE_PRESIGN_ENDPOINT_URL": f"https://{os.environ['DOMAIN']}",
    "STORAGE_ACCESS_KEY_ID": "myradius",
    "STORAGE_SECRET_ACCESS_KEY": os.environ["STORAGE_SECRET"],
    "STORAGE_PUBLIC_BASE_URL": "",
    "PUSH_PROVIDER": "fake",
    "FCM_CREDENTIALS_FILE": "",
    # Read by the Caddy service, which is off on this server; set so Compose
    # can still interpolate the production file.
    "API_DOMAIN": os.environ["DOMAIN"],
    "ACME_EMAIL": "unused@example.invalid",
}
lines = []
seen = set()
for line in pathlib.Path(".env.example").read_text().splitlines():
    key = line.split("=", 1)[0] if "=" in line and not line.startswith("#") else None
    if key in overrides:
        lines.append(f"{key}={overrides[key]}")
        seen.add(key)
    else:
        lines.append(line)
lines += [f"{key}={value}" for key, value in overrides.items() if key not in seen]
path = pathlib.Path(".env.production")
path.write_text("\n".join(lines) + "\n")
path.chmod(0o600)
PY
  STORAGE_SECRET="$storage_secret" python3 - <<'PY'
import json, os, pathlib
config = {"identities": [{
    "name": "myradius",
    "credentials": [{"accessKey": "myradius", "secretKey": os.environ["STORAGE_SECRET"]}],
    "actions": ["Admin", "Read", "Write", "List", "Tagging"],
}]}
path = pathlib.Path("secrets/s3.json")
path.write_text(json.dumps(config, indent=2) + "\n")
path.chmod(0o600)
PY
fi

install -m 755 deploy/vps/deploy-gate.sh /usr/local/bin/myradius-deploy

site=/etc/nginx/sites-available/$DOMAIN
if [[ ! -f "$site" ]]; then
  echo "bootstrap: installing the nginx site for $DOMAIN"
  install -m 644 deploy/vps/nginx-myradius.conf "$site"
  ln -sf "$site" "/etc/nginx/sites-enabled/$DOMAIN"
  if nginx -t 2>/dev/null; then
    systemctl reload nginx
  else
    # Never leave nginx unable to reload: the other sites depend on it.
    rm -f "/etc/nginx/sites-enabled/$DOMAIN"
    nginx -t
    echo "bootstrap: nginx rejected the site; removed it again" >&2
    exit 1
  fi
fi

echo "bootstrap: done. Deploy with: myradius-deploy 'deploy <sha>'"
