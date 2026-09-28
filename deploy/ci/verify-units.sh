#!/usr/bin/env bash
# Check the systemd units with systemd-analyze verify, the way
# pinny-bootstrap installs them (one worker file per pool, drop-ins). Run
# as root on a throwaway machine (CI): it creates placeholder files at the
# paths the units reference, because verify checks that executables exist.
set -euo pipefail

src=$(cd "$(dirname "$0")/../systemd" && pwd)
units=$(mktemp -d)
trap 'rm -rf "$units"' EXIT

cp "$src"/*.service "$src"/*.timer "$src"/*.target "$units"/
rm "$units/pinny-worker@.service"
for pool in interactive scan train; do
  cp "$src/pinny-worker@.service" "$units/pinny-worker-$pool@.service"
done
for d in "$src"/*.d; do
  cp -r "$d" "$units/"
done

for p in production staging; do
  install -d "/opt/pinny/$p/current/venv/bin" "/srv/pinny/$p/documents" "/srv/pinny/$p/tmp" "/srv/pinny/$p/jobs"
  ln -sf /usr/bin/true "/opt/pinny/$p/current/venv/bin/python"
  touch "/opt/pinny/$p/current/release.env"
done
install -d /etc/pinny
for f in production.env staging.env ops.env; do touch "/etc/pinny/$f"; done
for s in pinny-backup pinny-restore pinny-check-alerts pinny-notify; do
  [ -e "/usr/local/sbin/$s" ] || ln -sf /usr/bin/true "/usr/local/sbin/$s"
done

status=0
for u in pinny@production.target pinny-web@production.service pinny-web@staging.service \
  pinny-worker-interactive@production.service pinny-worker-scan@production.service \
  pinny-worker-train@production.service pinny-worker-train@staging.service \
  pinny-backup@production.service pinny-backup@production.timer pinny-backup@staging.timer \
  pinny-restore-test@production.service pinny-restore-test@production.timer \
  pinny-alert@pinny-backup@production.service.service \
  pinny-check-alerts.service pinny-check-alerts.timer; do
  # verify exits 0 even for unknown keys or bad values, so any message counts as a failure.
  if out=$(cd "$units" && systemd-analyze verify --man=no "$units/$u" 2>&1) && [ -z "$out" ]; then
    echo "ok  $u"
  else
    echo "FAIL $u"
    printf '%s\n' "$out" | sed 's/^/    /'
    status=1
  fi
done
exit "$status"
