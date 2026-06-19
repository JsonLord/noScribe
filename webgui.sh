#!/usr/bin/env bash
#
# noScribe in the browser, over Tailscale
# ---------------------------------------
# noScribe is a desktop (tkinter) app, so this does NOT turn it into a web app.
# Instead it runs noScribe on a headless virtual display and streams that
# display to your browser with noVNC, reachable only from your tailnet.
#
# Pipeline:  Xvfb  ->  (window manager)  ->  x11vnc  ->  websockify/noVNC  ->  browser
# noScribe itself is started via ./run.sh, so your cloud (.env) settings apply.
#
# Usage (run on the machine, e.g. the DGX Spark):
#     ./webgui.sh                 # bind to the Tailscale IP, http://<ts-ip>:6080/vnc.html
#     ./webgui.sh --tailscale-serve   # also publish HTTPS at https://<magicdns>/vnc.html
#     ./webgui.sh --password         # prompt for a VNC password (extra layer on top of tailnet)
#     ./webgui.sh --install-deps     # apt-get the required packages first (uses sudo)
#
# Options:
#     --display <:N>     Virtual X display      (default: :99)
#     --web-port <port>  noVNC/websockify port  (default: 6080)
#     --vnc-port <port>  x11vnc port (localhost) (default: 5900)
#     --geometry <WxHxD> Virtual screen size    (default: 1440x900x24)
#     --bind <addr|auto> Address noVNC binds to (default: auto = Tailscale IP)
#     --tailscale-serve  Publish via `tailscale serve` (HTTPS + MagicDNS, tailnet only)
#     --password         Set a VNC password (otherwise none; access gated by tailnet)
#     --upload           Also serve a drag-and-drop file upload/download page
#     --upload-port <p>  Port for the upload page    (default: 6081)
#     --upload-dir <d>   Shared folder for transfers (default: ~/transcribe/data)
#     --install-deps     Install missing system packages with apt (sudo)
#     -h, --help         Show this help and exit

set -euo pipefail

DISPLAY_NUM=":99"
WEB_PORT="6080"
VNC_PORT="5900"
GEOMETRY="1440x900x24"
BIND_ADDR="auto"
USE_TS_SERVE=0
USE_PASSWORD=0
INSTALL_DEPS=0
USE_UPLOAD=0
UPLOAD_PORT="6081"
UPLOAD_DIR="$HOME/transcribe/data"

c_blue=$'\033[1;34m'; c_green=$'\033[1;32m'; c_yellow=$'\033[1;33m'
c_red=$'\033[1;31m'; c_reset=$'\033[0m'
info() { printf '%s==>%s %s\n' "$c_blue"  "$c_reset" "$*"; }
ok()   { printf '%s ok%s %s\n' "$c_green" "$c_reset" "$*"; }
warn() { printf '%s !!%s %s\n' "$c_yellow" "$c_reset" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$c_red" "$c_reset" "$*" >&2; exit 1; }
usage() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
    case "$1" in
        --display)   DISPLAY_NUM="${2:?}"; shift 2 ;;
        --web-port)  WEB_PORT="${2:?}"; shift 2 ;;
        --vnc-port)  VNC_PORT="${2:?}"; shift 2 ;;
        --geometry)  GEOMETRY="${2:?}"; shift 2 ;;
        --bind)      BIND_ADDR="${2:?}"; shift 2 ;;
        --tailscale-serve) USE_TS_SERVE=1; shift ;;
        --password)  USE_PASSWORD=1; shift ;;
        --upload)    USE_UPLOAD=1; shift ;;
        --upload-port) UPLOAD_PORT="${2:?}"; shift 2 ;;
        --upload-dir)  UPLOAD_DIR="${2:?}"; shift 2 ;;
        --install-deps) INSTALL_DEPS=1; shift ;;
        -h|--help)   usage ;;
        *) die "Unknown option: $1 (use --help)" ;;
    esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
[ -x ./run.sh ] || die "run.sh not found in $HERE. Run install.sh first."

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
if [ "$INSTALL_DEPS" -eq 1 ]; then
    info "Installing system packages (Xvfb, x11vnc, novnc, websockify, fluxbox)..."
    sudo apt-get update
    sudo apt-get install -y xvfb x11vnc novnc websockify fluxbox
fi

need() { command -v "$1" >/dev/null 2>&1; }
missing=()
need Xvfb     || missing+=("xvfb")
need x11vnc   || missing+=("x11vnc")
need websockify || missing+=("websockify")
if [ "${#missing[@]}" -gt 0 ]; then
    die "Missing packages: ${missing[*]}. Re-run with --install-deps, or: sudo apt-get install -y xvfb x11vnc novnc websockify fluxbox"
fi

# Locate the noVNC web root (vnc.html lives here).
NOVNC_WEB=""
for d in /usr/share/novnc /usr/share/webapps/novnc /usr/local/share/novnc "$HOME/noVNC"; do
    if [ -f "$d/vnc.html" ]; then NOVNC_WEB="$d"; break; fi
done
[ -n "$NOVNC_WEB" ] || die "noVNC web files not found. Install with: sudo apt-get install -y novnc"

# ---------------------------------------------------------------------------
# Resolve bind address (Tailscale)
# ---------------------------------------------------------------------------
TS_IP=""; TS_DNS=""
if need tailscale; then
    TS_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
    TS_DNS="$(tailscale status --json 2>/dev/null \
        | python3 -c 'import sys,json;print(json.load(sys.stdin).get("Self",{}).get("DNSName","").rstrip("."))' 2>/dev/null || true)"
fi

if [ "$BIND_ADDR" = "auto" ]; then
    if [ -n "$TS_IP" ]; then
        BIND_ADDR="$TS_IP"
    else
        warn "Could not determine the Tailscale IP. Binding to 0.0.0.0 (reachable on every interface)."
        BIND_ADDR="0.0.0.0"
    fi
fi

# ---------------------------------------------------------------------------
# Optional VNC password
# ---------------------------------------------------------------------------
X11VNC_AUTH=(-nopw)
PASSWD_FILE=""
if [ "$USE_PASSWORD" -eq 1 ]; then
    PASSWD_FILE="$(mktemp)"
    read -r -s -p "  Choose a VNC password: " _vncpw; echo
    [ -n "$_vncpw" ] || die "Empty password."
    x11vnc -storepasswd "$_vncpw" "$PASSWD_FILE" >/dev/null 2>&1
    unset _vncpw
    X11VNC_AUTH=(-rfbauth "$PASSWD_FILE")
fi

# ---------------------------------------------------------------------------
# Start the stack, with cleanup on exit
# ---------------------------------------------------------------------------
PIDS=()
cleanup() {
    info "Shutting down..."
    for pid in "${PIDS[@]:-}"; do
        [ -n "${pid:-}" ] && kill "$pid" 2>/dev/null || true
    done
    if [ "$USE_TS_SERVE" -eq 1 ] && need tailscale; then
        tailscale serve --https=443 off 2>/dev/null || tailscale serve reset 2>/dev/null || true
    fi
    [ -n "$PASSWD_FILE" ] && rm -f "$PASSWD_FILE"
}
trap cleanup EXIT INT TERM

info "Starting virtual display $DISPLAY_NUM ($GEOMETRY)..."
Xvfb "$DISPLAY_NUM" -screen 0 "$GEOMETRY" -nolisten tcp >/dev/null 2>&1 &
PIDS+=($!)
export DISPLAY="$DISPLAY_NUM"
sleep 1

if need fluxbox; then
    info "Starting window manager (fluxbox)..."
    fluxbox >/dev/null 2>&1 &
    PIDS+=($!)
elif need openbox; then
    openbox >/dev/null 2>&1 &
    PIDS+=($!)
else
    warn "No window manager (fluxbox/openbox) found; the window may not be movable/resizable."
fi
sleep 1

info "Starting x11vnc on 127.0.0.1:$VNC_PORT (display $DISPLAY_NUM)..."
x11vnc -display "$DISPLAY_NUM" -rfbport "$VNC_PORT" -localhost \
       "${X11VNC_AUTH[@]}" -forever -shared -noxdamage -quiet >/dev/null 2>&1 &
PIDS+=($!)
sleep 1

info "Starting noVNC/websockify on $BIND_ADDR:$WEB_PORT ..."
websockify --web="$NOVNC_WEB" "$BIND_ADDR:$WEB_PORT" "127.0.0.1:$VNC_PORT" >/dev/null 2>&1 &
PIDS+=($!)
sleep 1

# Optional HTTPS publication over the tailnet.
TS_URL=""
if [ "$USE_TS_SERVE" -eq 1 ]; then
    if need tailscale; then
        info "Publishing via tailscale serve (HTTPS)..."
        if tailscale serve --bg --https=443 "http://127.0.0.1:$WEB_PORT" 2>/dev/null \
           || tailscale serve --bg "$WEB_PORT" 2>/dev/null; then
            [ -n "$TS_DNS" ] && TS_URL="https://$TS_DNS/vnc.html"
            ok "tailscale serve is active."
        else
            warn "tailscale serve failed (check 'tailscale serve status' and HTTPS/MagicDNS in the admin console)."
        fi
    else
        warn "tailscale not found; skipping --tailscale-serve."
    fi
fi

# Optional drag-and-drop upload/download page.
UPLOAD_URL=""
if [ "$USE_UPLOAD" -eq 1 ]; then
    info "Starting upload page on $BIND_ADDR:$UPLOAD_PORT (folder: $UPLOAD_DIR)..."
    mkdir -p "$UPLOAD_DIR"
    python3 "$HERE/upload_server.py" --dir "$UPLOAD_DIR" --bind "$BIND_ADDR" --port "$UPLOAD_PORT" >/dev/null 2>&1 &
    PIDS+=($!)
    if [ "$USE_TS_SERVE" -eq 1 ] && need tailscale; then
        tailscale serve --bg --https=443 --set-path=/files "http://127.0.0.1:$UPLOAD_PORT" 2>/dev/null \
            && [ -n "$TS_DNS" ] && UPLOAD_URL="https://$TS_DNS/files/"
    fi
    [ -z "$UPLOAD_URL" ] && [ -n "$TS_IP" ] && UPLOAD_URL="http://$TS_IP:$UPLOAD_PORT/"
    sleep 1
fi

info "Launching noScribe..."
./run.sh &
PIDS+=($!)

echo
ok "noScribe is running in your browser."
echo
echo "  Open one of these from any device on your tailnet:"
[ -n "$TS_URL" ]            && echo "      ${c_green}${TS_URL}${c_reset}   (HTTPS)"
[ -n "$TS_DNS" ]           && echo "      http://${TS_DNS}:${WEB_PORT}/vnc.html"
[ -n "$TS_IP" ]            && echo "      http://${TS_IP}:${WEB_PORT}/vnc.html"
[ "$BIND_ADDR" = "0.0.0.0" ] && echo "      http://<this-host>:${WEB_PORT}/vnc.html"
if [ "$USE_UPLOAD" -eq 1 ]; then
    echo
    echo "  Upload / download files at:"
    echo "      ${c_green}${UPLOAD_URL}${c_reset}"
    echo "      (uploads land in $UPLOAD_DIR — open them from noScribe's file dialog)"
fi
echo
echo "  Press Ctrl+C here to stop everything."
echo

# Wait until noScribe (or anything in the stack) exits, then cleanup runs.
wait
