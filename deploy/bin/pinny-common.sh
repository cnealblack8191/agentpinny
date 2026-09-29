# shellcheck shell=bash
# Shared functions for pinny-deploy, pinny-rollback and pinny-members.
# Sourced, not run. pinny-bootstrap installs it next to them in /usr/local/sbin.

PINNY_OPT=/opt/pinny
PINNY_ETC=/etc/pinny
PINNY_SRV=/srv/pinny
PINNY_BACKUP_STATE=/var/lib/pinny-backup
UNIT_DIR=/etc/systemd/system

say() { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
# Print a command that changes the system, then run it.
run() { printf '+ %s\n' "$*"; "$@"; }

need_root() {
  [ "$(id -u)" -eq 0 ] || die "run this as root: sudo $0 $*"
}

check_profile() {
  case "${1:-}" in
    production|staging) ;;
    *) die "the profile must be 'production' or 'staging', not '${1:-}'" ;;
  esac
}

# env_value FILE KEY: the value of KEY=value in a systemd environment file.
env_value() {
  local line
  line=$(grep -E "^$2=" "$1" 2>/dev/null | tail -n 1) || return 1
  line=${line#*=}
  # Values may be quoted (they must be when they contain spaces, so the file
  # can also be sourced by a shell); systemd strips the quotes the same way.
  case "$line" in \"*\") line=${line#\"}; line=${line%\"} ;; esac
  printf '%s\n' "$line"
}

release_version() { env_value "$1/release.env" PINNY_VERSION; }

current_release() { readlink -f "$PINNY_OPT/$1/current" 2>/dev/null || true; }

# The worker pools the release's pinny.jobs.worker accepts.
release_pools() {
  "$1/venv/bin/python" -c 'from pinny.jobs.worker import POOL_NAMES; print(" ".join(POOL_NAMES))'
}

# Pools that have an installed unit file (pinny-bootstrap installs them).
installed_pools() {
  local f pool
  for f in "$UNIT_DIR"/pinny-worker-*@.service; do
    [ -e "$f" ] || continue
    pool=${f##*/pinny-worker-}
    printf '%s\n' "${pool%@.service}"
  done
}

# run_as_web PROFILE RELEASE_DIR COMMAND...: run COMMAND as the profile's
# web user, with the profile's settings and the release's PINNY_VERSION,
# in the data directory, like the pinny-web unit does.
run_as_web() {
  local p=$1 rel=$2
  shift 2
  local -a envs
  mapfile -t envs < <(grep -E '^[A-Z_][A-Z0-9_]*=' "$PINNY_ETC/$p.env")
  envs+=("PINNY_VERSION=$(release_version "$rel")")
  (
    umask 0007
    cd "$PINNY_SRV/$p" || exit 1
    runuser -u "pinny-$p" -- env -i PATH=/usr/bin:/bin HOME=/nonexistent LANG=C.UTF-8 \
      PYTHONDONTWRITEBYTECODE=1 "${envs[@]}" "$@"
  )
}

# switch_current PROFILE RELEASE_DIR: atomically point "current" at a release.
switch_current() {
  local base="$PINNY_OPT/$1"
  run ln -sfn "$2" "$base/current.new"
  run mv -Tf "$base/current.new" "$base/current"
}

# The units of a profile that should run with this release: the web unit
# and one worker per pool the release knows. Pools the release does not
# know are disabled, so a missing pool never crash-loops.
enable_units() {
  local p=$1 rel=$2 pools pool
  pools=" $(release_pools "$rel") "
  run systemctl enable --quiet "pinny@$p.target" "pinny-web@$p.service"
  for pool in $pools; do
    if [ -e "$UNIT_DIR/pinny-worker-$pool@.service" ]; then
      run systemctl enable --quiet "pinny-worker-$pool@$p.service"
    else
      warn "this release has a '$pool' worker pool but no unit file for it; run pinny-bootstrap again"
    fi
  done
  for pool in $(installed_pools); do
    if [[ $pools != *" $pool "* ]] && systemctl is-enabled --quiet "pinny-worker-$pool@$p.service" 2>/dev/null; then
      run systemctl disable --now --quiet "pinny-worker-$pool@$p.service"
    fi
  done
}

enabled_units() {
  local p=$1 u
  for u in "pinny-web@$p.service" $(installed_pools | sed "s/.*/pinny-worker-&@$p.service/"); do
    if systemctl is-enabled --quiet "$u" 2>/dev/null; then
      printf '%s\n' "$u"
    fi
  done
}

# training_running PROFILE: a training, dataset or benchmark job is running.
training_running() {
  local data n
  data=$(env_value "$PINNY_ETC/$1.env" PINNY_DATA_DIR) || return 1
  [ -e "$data/jobs/jobs.sqlite3" ] || return 1
  n=$(sqlite3 -readonly "$data/jobs/jobs.sqlite3" \
    "SELECT COUNT(*) FROM jobs WHERE pool='train' AND status='running'" 2>/dev/null) || return 1
  [ "${n:-0}" -gt 0 ]
}

restart_units() {
  local -a units keep
  mapfile -t units < <(enabled_units "$1")
  # Restarting the train worker would cut off a run that may have taken hours
  # (it is not retried). Leave it on the old release until the run ends; it
  # picks up the new release at its next restart (at the latest the evening stop).
  if [ "${PINNY_RESTART_TRAINING:-no}" != yes ] && training_running "$1"; then
    warn "a training run is in progress on $1: the train worker keeps the old release until it" \
      "finishes. Restart it afterwards: systemctl restart pinny-worker-train@$1" \
      "(or deploy with PINNY_RESTART_TRAINING=yes to stop the run now)"
    keep=()
    for u in "${units[@]}"; do [ "$u" = "pinny-worker-train@$1.service" ] || keep+=("$u"); done
    units=("${keep[@]}")
  fi
  run systemctl restart "${units[@]}"
  run systemctl start "pinny@$1.target"
}

# wait_healthy PROFILE VERSION [SECONDS]: /healthz answers ok with VERSION.
wait_healthy() {
  local p=$1 want=$2 limit=${3:-90} port origin host body deadline
  port=$(env_value "$PINNY_ETC/$p.env" PINNY_PORT) || die "PINNY_PORT is not set in $PINNY_ETC/$p.env"
  origin=$(env_value "$PINNY_ETC/$p.env" PINNY_ORIGIN) || origin=""
  host=${origin#*://}
  deadline=$((SECONDS + limit))
  while [ "$SECONDS" -lt "$deadline" ]; do
    body=$(curl -fsS --max-time 3 -H "Host: ${host:-localhost}" -H "X-Forwarded-Proto: https" \
      "http://127.0.0.1:$port/healthz" 2>/dev/null) || body=""
    if [ -n "$body" ] && python3 -c 'import json, sys
d = json.loads(sys.argv[2]); sys.exit(0 if d.get("ok") is True and d.get("version") == sys.argv[1] else 1)' \
      "$want" "$body" 2>/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 1
}

# verify_release PROFILE VERSION: /healthz shows the version, every enabled
# unit is running, and a no-op job goes through a worker's sandbox.
verify_release() {
  local p=$1 version=$2 rel u
  rel=$(current_release "$p")
  say "Waiting for /healthz to report $version"
  wait_healthy "$p" "$version" 90 || { warn "/healthz did not report $version within 90 s"; return 1; }
  sleep 3
  for u in $(enabled_units "$p"); do
    systemctl is-active --quiet "$u" || { warn "$u is not running"; return 1; }
  done
  say "Running a test job through the worker sandbox"
  run_as_web "$p" "$rel" "$rel/venv/bin/python" -c '
import os, sys
from pinny.jobs.limits import LIMITS
from pinny.jobs.queue import JobQueue
if "selftest" not in LIMITS:
    print("no selftest job kind in this release; skipped")
    sys.exit(0)
print("job result:", JobQueue(os.environ["PINNY_DATA_DIR"]).run("selftest", {"action": "ok"}, timeout=90))
' || { warn "the test job failed (is the interactive worker running and sandboxed?)"; return 1; }
}

show_recent_logs() {
  local u
  for u in $(enabled_units "$1"); do
    printf -- '--- last log lines of %s ---\n' "$u"
    journalctl -u "$u" -n 15 --no-pager 2>/dev/null || true
  done
}

# snapshot_dbs PROFILE: copy the databases (online backup API) before a
# release switch, so a schema change can be undone. Keeps the newest 5.
snapshot_dbs() {
  local p=$1 base="$PINNY_BACKUP_STATE/$1/pre-deploy" out old
  if ! find "$PINNY_SRV/$p" -maxdepth 2 -name '*.sqlite3' -type f 2>/dev/null | grep -q .; then
    return 0
  fi
  out="$base/$(date -u +%Y%m%dT%H%M%SZ)"
  run install -d -o "pinny-$p" -g "pinny-$p" -m 0755 "$PINNY_BACKUP_STATE/$p"
  run install -d -o "pinny-$p" -g "pinny-$p" -m 0700 "$base"
  run runuser -u "pinny-$p" -- /usr/local/sbin/pinny-backup --profile "$p" --data-dir "$PINNY_SRV/$p" \
    --dbs-only --out "$out"
  for old in $(find "$base" -mindepth 1 -maxdepth 1 -type d | sort -r | tail -n +6); do
    rm -rf -- "$old"
  done
  # shellcheck disable=SC2034  # read by pinny-deploy
  LAST_SNAPSHOT=$out
}
