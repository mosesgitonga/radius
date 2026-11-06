#!/usr/bin/env bash
#
# Deploy a commit that deploy-gate.sh has already checked out in /srv/myradius:
#
#   deploy/vps/deploy.sh <sha> <previous sha>
#
# 1. backs up the database first if the release adds a migration;
# 2. rebuilds the image and restarts what changed (the API migrates as it starts);
# 3. waits for /ready; if it never comes, checks out the previous commit and
#    starts that again, and exits non-zero so CI shows the deploy failed.
#
# A rollback restores the code, not the schema: a migration that ran stays applied.
# Every migration is reversible and run down and up in CI, so after a failed
# release with a migration, downgrade by hand if the old code needs it.
set -euo pipefail

sha="$1"
previous="$2"
cd /srv/myradius

compose() {
  docker compose -f docker-compose.prod.yml -f docker-compose.vps.yml \
    --env-file .env.production "$@"
}

ready() {
  for _ in $(seq 1 60); do
    if curl -fsS --max-time 5 http://127.0.0.1:8031/ready >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  return 1
}

echo "deploy: ${previous:0:12} -> ${sha:0:12}"

if [[ "$previous" != "$sha" ]] && ! git diff --quiet "$previous" "$sha" -- migrations/; then
  echo "deploy: this release changes migrations; backing up first"
  COMPOSE_FILE=docker-compose.prod.yml:docker-compose.vps.yml ENV_FILE=.env.production \
    bash scripts/backup.sh
fi

if compose up -d --build --remove-orphans && ready; then
  # The photo bucket, provisioned on purpose here (the app never creates one
  # outside development). Already existing is fine.
  compose exec -T storage sh -c 'echo "s3.bucket.create -name myradius-media" | weed shell' \
    >/dev/null 2>&1 || true
  echo "deploy: ${sha:0:12} is live"
  docker image prune -f >/dev/null
  exit 0
fi

echo "deploy: ${sha:0:12} did not become ready; rolling back to ${previous:0:12}" >&2
compose logs --tail 80 api >&2 || true
git checkout --quiet --force "$previous"
compose up -d --build --remove-orphans
ready && echo "deploy: rolled back to ${previous:0:12}" >&2
exit 1
