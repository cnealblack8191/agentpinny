#!/usr/bin/env bash
# Syntax-check every script in deploy/bin: bash -n and shellcheck for the
# shell scripts, py_compile for the Python ones (chosen by their first line).
set -euo pipefail
cd "$(dirname "$0")/../bin"
status=0
for f in *; do
  [ -f "$f" ] || continue
  first=$(head -n 1 "$f")
  case "$first" in
    *python3*)
      python3 -m py_compile "$f" && echo "ok (python)  $f" || status=1
      ;;
    *bash*)
      if bash -n "$f" && shellcheck -x "$f"; then echo "ok (bash)    $f"; else status=1; fi
      ;;
    *)
      echo "unknown script type: $f ($first)" >&2
      status=1
      ;;
  esac
done
rm -rf __pycache__
exit "$status"
