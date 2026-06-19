#!/usr/bin/env bash
#
# serve.sh — the simple noScribe workflow in the browser (no noVNC)
# ----------------------------------------------------------------
# Starts the lightweight web page where you upload an audio file, pick a
# language, press "Transcribe" (which runs the noScribe CLI headlessly) and
# download the result. Speaker identification is on by default.
#
# For interactive work on big projects, use ./webgui.sh instead (full GUI
# streamed over noVNC).
#
# Usage (on the machine, e.g. the DGX Spark):
#     ./serve.sh                     # bind to the Tailscale IP, http://<ts-ip>:6080/
#     ./serve.sh --tailscale-serve   # publish HTTPS at https://<magicdns>/ (tailnet only)
#
# Options:
#     --port <port>      Port (default: 6080)
#     --dir <path>       Shared working folder (default: ~/transcribe/data)
#     --bind <addr|auto> Address to bind (default: auto = Tailscale IP)
#     --no-transcribe    Upload/download only (hide the Transcribe button)
#     --tailscale-serve  Publish via `tailscale serve` (HTTPS + MagicDNS)
#     -h, --help         Show this help and exit

set -euo pipefail

PORT="6080"
WORK_DIR="$HOME/transcribe/data"
BIND_ADDR="auto"
TRANSCRIBE=1
USE_TS_SERVE=0

c_blue=$'\033[1;34m'; c_green=$'\033[1;32m'; c_yellow=$'\033[1;33m'
c_red=$'\033[1;31m'; c_reset=$'\033[0m'
info() { printf '%s==>%s %s\n' "$c_blue"  "$c_reset" "$*"; }
ok()   { printf '%s ok%s %s\n' "$c_green" "$c_reset" "$*"; }
warn() { printf '%s !!%s %s\n' "$c_yellow" "$c_reset" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$c_red" "$c_reset" "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1; }
usage() { sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="${2:?}"; shift 2 ;;
        --dir)  WORK_DIR="${2:?}"; shift 2 ;;
        --bind) BIND_ADDR="${2:?}"; shift 2 ;;
        --no-transcribe) TRANSCRIBE=0; shift ;;
        --tailscale-serve) USE_TS_SERVE=1; shift ;;
        -h|--help) usage ;;
        *) die "Unknown option: $1 (use --help)" ;;
    esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# Pick the Python interpreter (prefer the venv so the CLI subprocess matches).
PYTHON="$HERE/venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

# Load cloud credentials so the CLI subprocess transcribes via the endpoint.
if [ -f ./.env ]; then
    set -a; # shellcheck disable=SC1091
    . ./.env; set +a
    ok "Loaded cloud credentials from .env"
else
    warn ".env not found — transcription will fall back to a local model if installed."
fi

# Resolve bind address from Tailscale.
TS_IP=""; TS_DNS=""
if need tailscale; then
    TS_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
    TS_DNS="$(tailscale status --json 2>/dev/null \
        | python3 -c 'import sys,json;print(json.load(sys.stdin).get("Self",{}).get("DNSName","").rstrip("."))' 2>/dev/null || true)"
fi
if [ "$BIND_ADDR" = "auto" ]; then
    if [ -n "$TS_IP" ]; then BIND_ADDR="$TS_IP"
    else warn "Could not determine the Tailscale IP; binding to 0.0.0.0."; BIND_ADDR="0.0.0.0"; fi
fi

mkdir -p "$WORK_DIR"

TS_URL=""
cleanup() {
    if [ "$USE_TS_SERVE" -eq 1 ] && need tailscale; then
        tailscale serve --https=443 off 2>/dev/null || tailscale serve reset 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

if [ "$USE_TS_SERVE" -eq 1 ] && need tailscale; then
    info "Publishing via tailscale serve (HTTPS)..."
    if tailscale serve --bg --https=443 "http://127.0.0.1:$PORT" 2>/dev/null \
       || tailscale serve --bg "$PORT" 2>/dev/null; then
        [ -n "$TS_DNS" ] && TS_URL="https://$TS_DNS/"
        # When fronted by HTTPS, only localhost needs the raw port.
        BIND_ADDR="127.0.0.1"
        ok "tailscale serve active."
    else
        warn "tailscale serve failed; falling back to direct port. Check 'tailscale serve status'."
    fi
fi

ARGS=(upload_server.py --dir "$WORK_DIR" --bind "$BIND_ADDR" --port "$PORT")
[ "$TRANSCRIBE" -eq 1 ] && ARGS+=(--transcribe)

echo
ok "noScribe simple web workflow is starting."
echo
echo "  Open from any device on your tailnet:"
[ -n "$TS_URL" ]            && echo "      ${c_green}${TS_URL}${c_reset}   (HTTPS)"
[ -n "$TS_DNS" ] && [ -z "$TS_URL" ] && echo "      http://${TS_DNS}:${PORT}/"
[ -n "$TS_IP" ]  && [ -z "$TS_URL" ] && echo "      http://${TS_IP}:${PORT}/"
[ "$BIND_ADDR" = "0.0.0.0" ] && echo "      http://<this-host>:${PORT}/"
echo
echo "  Speaker identification is ON by default (uncheck it per file to skip)."
echo "  Press Ctrl+C to stop."
echo

exec "$PYTHON" "${ARGS[@]}"
