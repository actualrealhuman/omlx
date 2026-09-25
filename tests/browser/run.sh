#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# One-command launcher for the chat storage browser harness.
#
#   bash tests/browser/run.sh
#
# The worktree root is served so that /tests/browser/index.html and
# /omlx/admin/static/js/*.js resolve under a single origin. file:// cannot be
# used: browsers treat it as an opaque origin, where IndexedDB is blocked or
# non-persistent and fetch() of local files is CORS-blocked.
#
# No dependencies beyond POSIX sh and python3. Does not start the oMLX server
# and does not load a model; this is a static file server only.

set -eu

# CDPATH would make a relative cd search elsewhere; unset it rather than
# assigning a dummy value, which sh treats as a real search path.
unset CDPATH

if ! SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" 2>/dev/null && pwd); then
    printf 'run.sh could not locate its own directory ($0=%s, cwd=%s).\n' "$0" "$(pwd)" >&2
    printf 'Invoke it by path from inside the worktree, e.g.:\n' >&2
    printf '  cd <worktree> && bash tests/browser/run.sh\n' >&2
    exit 1
fi
ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
# 127.0.0.1 is a secure context in every browser, so on its own this harness can
# never exercise the plain-HTTP configuration the app actually ships on. Run with
# LAN=1 to bind every interface and open the page over the machine's LAN address:
# navigator.storage and crypto.subtle then disappear while IndexedDB keeps working,
# which is the LAN/Tailscale case. HOST=<addr> overrides the bind address directly.
HOST=${HOST:-127.0.0.1}
if [ "${LAN:-0}" = "1" ]; then
    HOST=0.0.0.0
fi
START_PORT=${PORT:-8765}
SCAN=50

if ! command -v python3 >/dev/null 2>&1; then
    printf 'python3 is required but was not found on PATH.\n' >&2
    exit 1
fi

# Binds every candidate port in turn and reports the first one that is free.
free_port() {
    python3 - "$HOST" "$1" "$SCAN" <<'PY'
import socket, sys
host, start, span = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
for port in range(start, start + span):
    try:
        with socket.socket() as s:
            s.bind((host, port))
    except OSError:
        continue
    print(port)
    break
else:
    sys.exit(1)
PY
}

if ! PORT=$(free_port "$START_PORT"); then
    printf 'no free port found between %s and %s.\n' "$START_PORT" "$((START_PORT + SCAN - 1))" >&2
    exit 1
fi

# A wildcard bind is addressed through the machine's own LAN address, so the
# browser actually loads a non-secure origin rather than a loopback one.
lan_addr() {
    if command -v ipconfig >/dev/null 2>&1; then
        ipconfig getifaddr en0 2>/dev/null && return 0
    fi
    if command -v hostname >/dev/null 2>&1; then
        hostname -I 2>/dev/null | cut -d' ' -f1
    fi
}

DISPLAY_HOST=$HOST
if [ "$HOST" = "0.0.0.0" ]; then
    DISPLAY_HOST=$(lan_addr)
    if [ -z "$DISPLAY_HOST" ]; then
        printf 'LAN=1 was requested but no LAN address could be determined; falling back to 127.0.0.1,\n' >&2
        printf 'which is a secure context and therefore does not test the plain-HTTP case.\n' >&2
        DISPLAY_HOST=127.0.0.1
    fi
fi

# Readiness is probed on loopback: a wildcard bind is always reachable there,
# whereas the LAN address may be filtered by a firewall on this host.
PROBE_HOST=$HOST
if [ "$HOST" = "0.0.0.0" ]; then PROBE_HOST=127.0.0.1; fi

URL="http://${DISPLAY_HOST}:${PORT}/tests/browser/index.html"
LOG="${TMPDIR:-/tmp}/omlx-chat-storage-harness-${PORT}.log"

python3 -m http.server "$PORT" --bind "$HOST" --directory "$ROOT" >"$LOG" 2>&1 &
SERVER=$!

cleanup() {
    trap - INT TERM EXIT
    kill "$SERVER" 2>/dev/null || true
    wait "$SERVER" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

# Do not open a browser against a socket that is not listening yet.
i=0
until python3 - "$PROBE_HOST" "$PORT" <<'PY'
import socket, sys
s = socket.socket()
s.settimeout(0.3)
try:
    s.connect((sys.argv[1], int(sys.argv[2])))
except OSError:
    s.close()
    sys.exit(1)
s.close()
PY
do
    i=$((i + 1))
    if ! kill -0 "$SERVER" 2>/dev/null; then
        printf 'the static server exited before it became reachable. Log:\n' >&2
        cat "$LOG" >&2 || true
        exit 1
    fi
    if [ "$i" -ge 100 ]; then
        printf 'the static server did not become reachable. Log:\n' >&2
        cat "$LOG" >&2 || true
        exit 1
    fi
    sleep 0.1
done

printf '\n  oMLX chat storage — real IndexedDB harness\n\n'
printf '  origin   %s\n' "http://${DISPLAY_HOST}:${PORT}"
printf '  page     %s\n' "$URL"
printf '  root     %s\n' "$ROOT"
printf '  log      %s\n\n' "$LOG"
if [ "$DISPLAY_HOST" = "127.0.0.1" ] || [ "$DISPLAY_HOST" = "localhost" ]; then
    printf '  This origin is a secure context: navigator.storage and crypto.subtle are\n'
    printf '  present. Run LAN=1 to test the plain-HTTP case, where both are absent.\n\n'
else
    printf '  This origin is NOT a secure context: navigator.storage and crypto.subtle are\n'
    printf '  absent while IndexedDB still works. That is the LAN / Tailscale case, and it\n'
    printf '  is a supported deployment — the quota and persistence rows report as notes.\n\n'
fi
printf '  Leave this running while you test. Press Ctrl-C to stop the server.\n\n'

if command -v open >/dev/null 2>&1; then
    open "$URL" >/dev/null 2>&1 \
        || printf 'Could not open a browser automatically. Open %s\n\n' "$URL"
elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 \
        || printf 'Could not open a browser automatically. Open %s\n\n' "$URL"
else
    printf 'No browser opener found. Open %s manually.\n\n' "$URL"
fi

wait "$SERVER"
