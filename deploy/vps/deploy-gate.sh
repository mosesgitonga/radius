#!/usr/bin/env bash
#
# The only thing the CI deploy key may run on the VPS. Installed as
# /usr/local/bin/myradius-deploy and named as the key's forced command in
# /root/.ssh/authorized_keys, so whatever the client asks for, this runs instead:
#
#   ssh root@VPS "deploy <40-character commit SHA>"
#
# It accepts exactly that, and only for a commit on origin/main, so a leaked key
# can redeploy main and nothing else. One deploy at a time.
set -euo pipefail

REPO=/srv/myradius
request="${SSH_ORIGINAL_COMMAND:-${*:-}}"

if [[ ! "$request" =~ ^deploy\ ([0-9a-f]{40})$ ]]; then
  echo "refused: expected 'deploy <commit sha>'" >&2
  exit 2
fi
sha="${BASH_REMATCH[1]}"

# A deploy that has started finishes even if the SSH connection drops: half a
# `docker compose up` is worse than either version.
trap '' HUP

exec 9>/var/lock/myradius-deploy.lock
if ! flock -n 9; then
  echo "refused: another deploy is running" >&2
  exit 3
fi

cd "$REPO"
previous="$(git rev-parse HEAD)"
git fetch --quiet origin main
if ! git merge-base --is-ancestor "$sha" origin/main; then
  echo "refused: $sha is not on main" >&2
  exit 4
fi
git checkout --quiet --force "$sha"

# The deploy script as of the commit being deployed, not the one being replaced.
exec bash deploy/vps/deploy.sh "$sha" "$previous"
