#!/usr/bin/env bash
# Phase 2 acceptance on the Mac, in real headless Chrome (PLAN Phase 2; docs/runbooks/ui-dev.md "Phase 2 acceptance").
#
#   npm run acceptance            # from frontend/hud; needs Chrome (CHROME_BIN or the macOS default) and backend/.venv
#
# Builds the HUD, then runs scripts/browser-check.mjs against:
#   1-3. the built HUD + the mock server (every scenario, looping) at a wide, a medium and a phone width, with axe:
#        UI-SEC-3 (no off-origin request), UI-SEC-4 (no CSP violation), A11Y-1, and every orb state rendered (wide);
#   4.   the same with prefers-reduced-motion emulated: only the static tier may run (A11Y-2);
#   5.   the mock with kiosk permissions: the kiosk profile renders cleanly;
#   6.   the REAL atlas-ui (dev runner: HTTPS, throwaway CA, real core and control socket), paired through /api/pair
#        like a browser would be: the paired HUD connects and renders cleanly over WSS.
# UI-SEC-1/2 (unauthenticated and cross-origin WebSockets refused) are pytest: backend/tests/test_ui_security.py.
# UP-1..3 and B8 need the kiosk on the Pi (and voice, for UP-2/3); they are not part of this script.
set -euo pipefail

hud="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
backend="$(cd "$hud/../../backend" && pwd)"
py="$backend/.venv/bin/python"
work="$(mktemp -d)"
pids=()
results=()
failed=0

cleanup() {
  for pid in "${pids[@]+"${pids[@]}"}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

wait_port() { # wait_port PORT: until something listens on 127.0.0.1:PORT (20 s)
  for _ in $(seq 100); do
    if (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; then return 0; fi
    sleep 0.2
  done
  echo "nothing listening on port $1" >&2
  return 1
}

check() { # check NAME ARGS...: one browser-check run, recorded in the summary
  local name="$1"
  shift
  echo "== $name"
  if node "$hud/scripts/browser-check.mjs" "$@" >"$work/out" 2>&1; then
    results+=("PASS  $name")
  else
    results+=("FAIL  $name")
    failed=1
  fi
  grep -E '^(states seen|axe:|OK:|FAILED|  - )' "$work/out" | cut -c1-220 || true
}

start_mock() { # start_mock [EXTRA ARGS]: the mock event server on :8765 (what vite preview proxies /ws/events to)
  if [ -n "${mock_pid:-}" ]; then kill "$mock_pid" 2>/dev/null || true; wait "$mock_pid" 2>/dev/null || true; fi
  (cd "$backend" && exec "$py" -m atlas.ui_server.mock --scenario all --loop --speed 4 "$@") >"$work/mock.log" 2>&1 &
  mock_pid=$!
  pids+=("$mock_pid")
  wait_port 8765
}

[ -x "$py" ] || { echo "backend/.venv missing: see CLAUDE.md (Backend)" >&2; exit 2; }
if (exec 3<>/dev/tcp/127.0.0.1/8765) 2>/dev/null || (exec 3<>/dev/tcp/127.0.0.1/4173) 2>/dev/null; then
  echo "port 8765 or 4173 is already in use: stop the running mock server or vite preview first" >&2
  exit 2
fi

cd "$hud"
npm run -s build >/dev/null

start_mock
(cd "$hud" && exec npx vite preview --port 4173 --strictPort) >"$work/preview.log" 2>&1 &
pids+=($!)
wait_port 4173
url=http://127.0.0.1:4173/

check "remote, 1920x1080, every state, axe" "$url" --width 1920 --height 1080 --wait 60000 --axe --all-states --expect-profile remote
check "remote, 1000x800, axe" "$url" --width 1000 --height 800 --wait 8000 --axe
check "remote, 390x844 (phone), axe" "$url" --width 390 --height 844 --wait 8000 --axe
check "reduced motion: static tier only" "$url" --width 1280 --height 800 --wait 15000 --reduced-motion

start_mock --device kiosk
check "kiosk profile, 1280x800, axe" "$url" --width 1280 --height 800 --wait 8000 --axe --expect-profile kiosk

echo "== real atlas-ui (dev runner, HTTPS) with pairing"
port=8443
# -u: the pairing code is printed to a file, which Python would otherwise buffer until exit.
(cd "$backend" && exec "$py" -u -m atlas.ui_server.dev --port "$port" --dir "$work/atlas-dev") >"$work/ui.log" 2>&1 &
pids+=($!)
wait_port "$port"
code=""
for _ in $(seq 50); do
  code="$(sed -n 's/^Pairing code (remote device[^:]*: *\([A-Z0-9-]*\).*/\1/p' "$work/ui.log" | head -1)"
  [ -n "$code" ] && break
  sleep 0.2
done
origin="https://localhost:$port"
cookie="$(curl -sk -D - -o /dev/null "$origin/api/pair" -H "Origin: $origin" -H "X-Atlas-Pairing-Code: $code" |
  sed -n 's/^[Ss]et-[Cc]ookie: *\(__Host-atlas-device=[^;]*\).*/\1/p' | tr -d '\r' | head -1)"
if [ -z "$code" ] || [ -z "$cookie" ]; then
  results+=("FAIL  real atlas-ui: pairing (code ${code:+found}${code:-missing}, cookie ${cookie:+set}${cookie:-missing})")
  failed=1
else
  check "real atlas-ui over HTTPS/WSS, paired remote device, axe" "$origin/" --insecure --cookie "$cookie" --width 1600 --height 900 --wait 8000 --axe --expect-profile remote
fi

echo
echo "Phase 2 acceptance (Mac, headless Chrome):"
printf '  %s\n' "${results[@]}"
exit "$failed"
