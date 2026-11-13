#!/usr/bin/env bash
#
# A full Android release through Shorebird (DECISIONS M.12). Run from the
# repository, after `shorebird login` (or with SHOREBIRD_TOKEN set in CI):
#
#   scripts/app_release.sh
#
# Use it when a change can't go over the air: a new plugin, native code, a Flutter
# upgrade, or new images or fonts. Bump `version:` in mobile/pubspec.yaml first;
# the number after `+` is the build number the app reports to the version check.
# Phones need the new APK installed once; patches then apply to it.
#
# API_BASE_URL picks the server (staging by default).
set -euo pipefail

cd "$(dirname "$0")/../mobile"
api="${API_BASE_URL:-https://myradius.chatara.co}"
version="$(grep -E '^version:' pubspec.yaml | awk '{print $2}')"
build="${version##*+}"

echo "Releasing ${version} (build ${build}) against ${api}"
shorebird release android --artifact apk --target-platform android-arm64 \
  -- --dart-define=API_BASE_URL="${api}" --dart-define=APP_BUILD="${build}"

echo
echo "APK: mobile/build/app/outputs/flutter-apk/app-release.apk"
echo "Then set APP_LATEST_BUILD=${build} on the server to offer it to older builds."
