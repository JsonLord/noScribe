#!/usr/bin/env bash
#
# taildrive.sh — share/mount folders over Tailscale Drive (Taildrive)
# -------------------------------------------------------------------
# A stand-alone helper (independent of noScribe) for exposing a folder on one
# tailnet machine and mounting it on another, so files appear as a normal
# mounted drive instead of being copied around.
#
# On the machine that HAS the files (e.g. the DGX Spark):
#     ./taildrive.sh share transcribe ~/transcribe/data
#     ./taildrive.sh list
#     ./taildrive.sh unshare transcribe
#
# On the machine that wants to USE them (e.g. your laptop):
#     ./taildrive.sh mount <device> <share> ~/mnt/spark      # via rclone (no root)
#     ./taildrive.sh unmount ~/mnt/spark
#     ./taildrive.sh url <device> <share>                    # print the WebDAV URL
#
# Notes:
#   * Taildrive must be enabled for your tailnet and the nodes must have the
#     "drive" capability (see the Tailscale admin console / ACLs).
#   * Sharing uses the built-in `tailscale drive` subcommands.
#   * Mounting uses rclone's WebDAV backend against the local Tailscale Drive
#     endpoint (http://100.100.100.100:8080). davfs2 or your OS file manager
#     work too — `url` prints the address to use.

set -euo pipefail

DRIVE_HOST="http://100.100.100.100:8080"  # local tailscaled Taildrive endpoint

c_blue=$'\033[1;34m'; c_green=$'\033[1;32m'; c_yellow=$'\033[1;33m'
c_red=$'\033[1;31m'; c_reset=$'\033[0m'
info() { printf '%s==>%s %s\n' "$c_blue"  "$c_reset" "$*"; }
ok()   { printf '%s ok%s %s\n' "$c_green" "$c_reset" "$*"; }
warn() { printf '%s !!%s %s\n' "$c_yellow" "$c_reset" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$c_red" "$c_reset" "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "'$1' is required but not installed."; }

usage() { sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

# Tailnet name (the path segment Taildrive uses), best-effort from status.
tailnet_name() {
    tailscale status --json 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
# MagicDNSSuffix looks like "<tailnet>.ts.net"; the Taildrive path uses <tailnet>.
suffix = d.get("MagicDNSSuffix") or d.get("CurrentTailnet", {}).get("MagicDNSSuffix", "")
print(suffix)
' 2>/dev/null || true
}

build_url() {  # build_url <device> <share>
    local device="$1" share="$2" tn
    tn="$(tailnet_name)"
    [ -n "$tn" ] || die "Could not determine the tailnet name. Pass a full URL with --url, or check 'tailscale status'."
    printf '%s/%s/%s/%s' "$DRIVE_HOST" "$tn" "$device" "$share"
}

cmd="${1:-}"; [ $# -gt 0 ] && shift || true
case "$cmd" in -h|--help|help|"") usage ;; esac

need tailscale

case "$cmd" in
    share)
        name="${1:-transcribe}"
        path="${2:-$HOME/transcribe/data}"
        mkdir -p "$path"
        info "Sharing '$path' as Taildrive share '$name'..."
        tailscale drive share "$name" "$path"
        ok "Shared. Current shares:"
        tailscale drive list
        echo
        echo "  Mount it from another tailnet device with:"
        echo "      ./taildrive.sh mount $(hostname -s) $name ~/mnt/$name"
        ;;

    unshare)
        name="${1:?usage: taildrive.sh unshare <name>}"
        tailscale drive unshare "$name"
        ok "Unshared '$name'."
        ;;

    list|status)
        tailscale drive list
        ;;

    url)
        device="${1:?usage: taildrive.sh url <device> <share>}"
        share="${2:?usage: taildrive.sh url <device> <share>}"
        build_url "$device" "$share"; echo
        ;;

    mount)
        device="${1:?usage: taildrive.sh mount <device> <share> <mountpoint>}"
        share="${2:?usage: taildrive.sh mount <device> <share> <mountpoint>}"
        mnt="${3:?usage: taildrive.sh mount <device> <share> <mountpoint>}"
        need rclone
        url="$(build_url "$device" "$share")"
        mkdir -p "$mnt"
        info "Mounting $url -> $mnt (rclone WebDAV)..."
        # Connection-string remote; no persistent rclone config needed.
        rclone mount \
            ":webdav,url='${DRIVE_HOST}',vendor='other':${url#"$DRIVE_HOST"/}" \
            "$mnt" \
            --vfs-cache-mode writes --dir-cache-time 5s --daemon
        sleep 1
        if mountpoint -q "$mnt" 2>/dev/null || ls "$mnt" >/dev/null 2>&1; then
            ok "Mounted at $mnt"
        else
            warn "Mount may not be ready yet; check with: ls $mnt"
        fi
        echo "  Unmount with: ./taildrive.sh unmount $mnt"
        ;;

    unmount|umount)
        mnt="${1:?usage: taildrive.sh unmount <mountpoint>}"
        if command -v fusermount >/dev/null 2>&1; then
            fusermount -u "$mnt" || umount "$mnt"
        else
            umount "$mnt"
        fi
        ok "Unmounted $mnt"
        ;;

    -h|--help|help) usage ;;
    *) die "Unknown command: $cmd (use --help)" ;;
esac
