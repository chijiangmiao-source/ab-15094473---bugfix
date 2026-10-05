#!/usr/bin/env bash
#
# verify.sh — gate run for the Compose `verify` service.
#
#   1. byte-compile every Python module (build check)
#   2. run the branch-rollback / exception-boundary unit + API tests
#   3. run HTTP/API smoke checks against a temporarily started server
#      (unless TARGET_URL points at an already-running instance)
#
# Exits non-zero on the first failed stage; Compose propagates the code.
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${PORT:-8080}"
export PYTHONPATH="${PYTHONPATH:-}:$(pwd)"

echo "== [1/3] build check: python3 -m compileall =="
python3 -m compileall -q app scripts tests
echo "   compile OK"

echo "== [2/3] unit + API tests (branch rollback & precise exceptions) =="
python3 -m unittest discover -s tests -v

if [ -z "${TARGET_URL:-}" ]; then
  echo "== [3/3] HTTP/API smoke against temporary server on port ${PORT} =="
  PORT="${PORT}" QUIET=1 python3 -m app &
  SRV_PID=$!
  trap 'kill ${SRV_PID} 2>/dev/null || true' EXIT
  TARGET_URL="http://127.0.0.1:${PORT}"
  # Wait for health before smoke runs its own polling.
  for _ in $(seq 1 30); do
    if python3 -c "import urllib.request,sys; urllib.request.urlopen('${TARGET_URL}/health', timeout=2)" 2>/dev/null; then
      break
    fi
    sleep 0.5
  done
  python3 scripts/smoke.py "${TARGET_URL}"
else
  echo "== [3/3] HTTP/API smoke against ${TARGET_URL} =="
  python3 scripts/smoke.py "${TARGET_URL}"
fi

echo
echo "VERIFY PASSED"
