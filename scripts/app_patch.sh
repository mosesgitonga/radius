#!/usr/bin/env bash
#
# Sends a code patch to phones over the air (DECISIONS M.12). Run from the
# repository, after `shorebird login` (or with SHOREBIRD_TOKEN set in CI):
#
#   scripts/app_patch.sh
#
# Patches the release whose version is in mobile/pubspec.yaml, so leave
# `version:` alone between a release and its patches. Dart changes only: Shorebird
# refuses a patch that needs native changes, and then scripts/app_release.sh is
# the way. Phones download it in the background and run it from their next launch.
#
# API_BASE_URL must match the release's (staging by default).
set -euo pipefail

cd "$(dirname "$0")/../mobile"
api="${API_BASE_URL:-https://myradius.chatara.co}"
version="$(grep -E '^version:' pubspec.yaml | awk '{print $2}')"
build="${version##*+}"

echo "Patching release ${version} against ${api}"
shorebird patch android --release-version="${version}" \
  -- --dart-define=API_BASE_URL="${api}" --dart-define=APP_BUILD="${build}"
