#!/usr/bin/env bash
#
# Stop everything this project runs on a development machine:
#
#   scripts/stop-everything.sh            # stop it all
#   scripts/stop-everything.sh --dry-run  # only list what would be stopped
#   scripts/stop-everything.sh --keep-docker-desktop
#
# 1. the app's containers (api, worker, postgres, redis, storage), with
#    `docker compose down`; volumes are kept, so the database survives;
# 2. the Android emulator and adb;
# 3. Gradle daemons and any `flutter run` for this app;
# 4. Python processes started from this repository (uvicorn, arq, pytest);
# 5. Docker Desktop itself, unless --keep-docker-desktop is given.
#
# Only processes that belong to this project or to the emulator are matched, so
# an editor's own Dart or Java tools are left alone. Written for Git Bash on
# Windows; on Linux or macOS it falls back to pkill.
set -uo pipefail

DRY_RUN=0
KEEP_DOCKER_DESKTOP=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --keep-docker-desktop) KEEP_DOCKER_DESKTOP=1 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_NAME="$(basename "$REPO")"

say() { printf '%s\n' "$*"; }
run() {
  if [[ $DRY_RUN -eq 1 ]]; then say "  would run: $*"; else "$@"; fi
}

is_windows() {
  case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) return 0 ;; *) return 1 ;; esac
}

# Stop Windows processes whose name matches $1 (a regex) and, if given, whose
# command line matches $2. PowerShell does the matching, since Git Bash cannot
# see Windows command lines.
stop_windows() {
  local names="$1" cmdline="${2:-}"
  local filter="\$_.Name -match '$names'"
  [[ -n "$cmdline" ]] && filter="$filter -and \$_.CommandLine -match '$cmdline'"
  local action='Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue'
  [[ $DRY_RUN -eq 1 ]] && action='$null'
  powershell.exe -NoProfile -Command "
    Get-CimInstance Win32_Process |
      Where-Object { $filter -and \$_.ProcessId -ne \$PID } |
      ForEach-Object { '  ' + \$_.Name + ' (' + \$_.ProcessId + ')'; $action }
  " 2>/dev/null | tr -d '\r'
}

stop_unix() {
  local pattern="$1"
  pgrep -fl "$pattern" 2>/dev/null | sed 's/^/  /'
  [[ $DRY_RUN -eq 1 ]] || pkill -f "$pattern" 2>/dev/null || true
}

[[ $DRY_RUN -eq 1 ]] && say "Dry run: nothing will be stopped."

say "1. Containers"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  (cd "$REPO" && run docker compose down --remove-orphans)
else
  say "  Docker is not running."
fi

say "2. Android emulator and adb"
if is_windows; then
  stop_windows '^(emulator|qemu-system-.*|emulator64-crash-service)\.exe$'
  stop_windows '^adb\.exe$'
else
  stop_unix 'qemu-system|emulator -avd'
  stop_unix 'adb .*server'
fi

say "3. Gradle and flutter run"
if is_windows; then
  stop_windows '^java\.exe$' 'GradleDaemon|GradleWrapperMain|kotlin-daemon'
  stop_windows '^dart(aotruntime)?\.exe$' "flutter_tools|$REPO_NAME"
else
  stop_unix 'GradleDaemon|kotlin-daemon'
  stop_unix "flutter_tools.*run"
fi

say "4. Python from this repository"
if is_windows; then
  stop_windows '^(python|uv|uvicorn|arq)\.exe$' "$REPO_NAME"
else
  stop_unix "$REPO.*(uvicorn|arq|pytest)"
fi

say "5. Docker Desktop"
if [[ $KEEP_DOCKER_DESKTOP -eq 1 ]]; then
  say "  Kept (--keep-docker-desktop)."
elif is_windows; then
  stop_windows '^(Docker Desktop|com\.docker\.backend|com\.docker\.build|com\.docker\.dev-envs|docker-sandbox)\.exe$'
  # Docker's Linux VM runs in WSL; closing the app leaves it up until WSL stops it.
  if [[ $DRY_RUN -eq 1 ]]; then
    say "  would run: wsl --terminate docker-desktop"
  else
    wsl.exe --terminate docker-desktop >/dev/null 2>&1 || true
  fi
elif [[ "$(uname -s)" == Darwin ]]; then
  run osascript -e 'quit app "Docker"'
else
  say "  On Linux the Docker daemon is a system service; stop it with: sudo systemctl stop docker"
fi

say "Done."
