#!/usr/bin/env bash
#
# noScribe cloud installer
# ------------------------
# Bootstraps noScribe for use with a cloud-hosted, OpenAI-compatible
# transcription endpoint. Run it inside the project folder where you want
# noScribe to live:
#
#     bash install.sh
#
# It will:
#   1. git clone noScribe (and the noScribe Editor) into ./noScribe
#   2. create a Python virtual environment and install all dependencies
#   3. ask you for your OpenAI-compatible endpoint credentials and store
#      them in ./noScribe/.env (chmod 600 — never committed)
#   4. write ./noScribe/run.sh, a launcher that loads those credentials
#      into the environment and starts noScribe
#
# By default the large local Whisper models are NOT downloaded, because the
# cloud endpoint does the transcription. Pass --with-models if you also want
# offline/local transcription available.
#
# Options:
#   --repo <url>       Git URL to clone        (default: this repository)
#   --branch <name>    Branch to check out     (default: cloud feature branch)
#   --dir <path>       Target directory        (default: ./noScribe)
#   --base-url <url>   Pre-set the endpoint base URL (skips that prompt)
#   --model <name>     Pre-set the model name        (skips that prompt)
#   --with-models      Also download the local Whisper models (several GB)
#   -h, --help         Show this help and exit

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
REPO_URL="https://github.com/JsonLord/noScribe.git"
# The cloud-transcription feature; switch to "main" once it has been merged.
BRANCH="claude/vibrant-pasteur-m7vu9m"
EDITOR_REPO="https://github.com/kaixxx/noScribeEditor.git"
TARGET_DIR="noScribe"
WITH_MODELS=0
DEFAULT_BASE_URL="${NOSCRIBE_OPENAI_BASE_URL:-${OPENAI_BASE_URL:-https://api.openai.com/v1}}"
DEFAULT_MODEL="${NOSCRIBE_OPENAI_MODEL:-${OPENAI_MODEL:-whisper-1}}"
PRESET_BASE_URL=""
PRESET_MODEL=""

# Model repositories (only used with --with-models)
MODEL_FAST_REPO="https://huggingface.co/mukowaty/faster-whisper-int8"
MODEL_PRECISE_REPO="https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
c_blue=$'\033[1;34m'; c_green=$'\033[1;32m'; c_yellow=$'\033[1;33m'
c_red=$'\033[1;31m'; c_reset=$'\033[0m'

info()  { printf '%s==>%s %s\n' "$c_blue"  "$c_reset" "$*"; }
ok()    { printf '%s ok%s %s\n' "$c_green" "$c_reset" "$*"; }
warn()  { printf '%s !!%s %s\n' "$c_yellow" "$c_reset" "$*" >&2; }
die()   { printf '%serror%s %s\n' "$c_red" "$c_reset" "$*" >&2; exit 1; }

usage() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

need() { command -v "$1" >/dev/null 2>&1 || die "'$1' is required but not installed."; }

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --repo)      REPO_URL="${2:?--repo needs a value}"; shift 2 ;;
        --branch)    BRANCH="${2:?--branch needs a value}"; shift 2 ;;
        --dir)       TARGET_DIR="${2:?--dir needs a value}"; shift 2 ;;
        --base-url)  PRESET_BASE_URL="${2:?--base-url needs a value}"; shift 2 ;;
        --model)     PRESET_MODEL="${2:?--model needs a value}"; shift 2 ;;
        --with-models) WITH_MODELS=1; shift ;;
        -h|--help)   usage ;;
        *) die "Unknown option: $1 (use --help)" ;;
    esac
done

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------
info "Checking prerequisites..."
need git
need python3
python3 -m venv --help >/dev/null 2>&1 || die "The python3 'venv' module is missing (install python3-venv)."
python3 -m pip --version >/dev/null 2>&1 || die "pip for python3 is missing (install python3-pip)."
command -v git-lfs >/dev/null 2>&1 || warn "git-lfs not found. It is recommended (and required for --with-models)."
ok "Prerequisites look fine."

# Pick the requirements file for this platform.
uname_s="$(uname -s)"
uname_m="$(uname -m)"
case "$uname_s" in
    Linux)
        if [ "$uname_m" = "aarch64" ] || [ "$uname_m" = "arm64" ]; then
            # e.g. NVIDIA DGX Spark / Grace-Blackwell — the x86 torch pins have
            # no aarch64 wheels (torchcodec 0.7.0 in particular).
            REQ_FILE="environments/requirements_linux_aarch64.txt"
        else
            REQ_FILE="environments/requirements_linux.txt"
        fi
        ;;
    Darwin)
        if [ "$uname_m" = "arm64" ]; then
            REQ_FILE="environments/requirements_macOS_arm64.txt"
        else
            warn "Intel macOS is not officially supported by noScribe; using the x86_64 requirements file."
            REQ_FILE="environments/requirements_macOS_x86_64_NOT_WORKING.txt"
        fi
        ;;
    *) die "Unsupported platform '$uname_s'. Run this on Linux or macOS." ;;
esac

# ---------------------------------------------------------------------------
# Clone the repository (supports installing into an existing folder)
# ---------------------------------------------------------------------------
if [ -d "$TARGET_DIR/.git" ]; then
    info "Found existing checkout in '$TARGET_DIR', fetching '$BRANCH'..."
    git -C "$TARGET_DIR" fetch --depth 1 origin "$BRANCH"
    git -C "$TARGET_DIR" checkout -B "$BRANCH" FETCH_HEAD
elif [ -e "$TARGET_DIR" ] && [ -n "$(ls -A "$TARGET_DIR" 2>/dev/null)" ]; then
    # Existing, non-empty directory that is not a git checkout (e.g. you ran
    # the installer from inside the folder you want to use). Clone into a temp
    # location and copy the files in. This overwrites same-named files such as
    # this install script, but leaves your other files alone.
    info "Installing into existing directory '$TARGET_DIR' (in place)..."
    _tmp_clone="$(mktemp -d)"
    trap 'rm -rf "$_tmp_clone"' EXIT
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$_tmp_clone/repo"
    cp -a "$_tmp_clone/repo/." "$TARGET_DIR/"
    rm -rf "$_tmp_clone"
    trap - EXIT
else
    info "Cloning $REPO_URL (branch $BRANCH) into '$TARGET_DIR'..."
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$TARGET_DIR"
fi
ok "Repository ready."

cd "$TARGET_DIR"
PROJECT_ROOT="$(pwd)"

# ---------------------------------------------------------------------------
# noScribe Editor
# ---------------------------------------------------------------------------
info "Installing the noScribe Editor..."
rm -rf noScribeEdit
git clone --depth 1 "$EDITOR_REPO" noScribeEdit
ok "Editor installed."

# ---------------------------------------------------------------------------
# Virtual environment + dependencies
# ---------------------------------------------------------------------------
info "Creating Python virtual environment in ./venv ..."
python3 -m venv venv
# shellcheck disable=SC1091
. venv/bin/activate
python3 -m pip install --upgrade pip >/dev/null

[ -f "$REQ_FILE" ] || die "Requirements file '$REQ_FILE' not found in the repository."
info "Installing noScribe dependencies from $REQ_FILE ..."
pip install -r "$REQ_FILE"

if [ -f "noScribeEdit/environments/requirements.txt" ]; then
    info "Installing editor dependencies..."
    pip install -r noScribeEdit/environments/requirements.txt
fi
ok "Dependencies installed."

# ---------------------------------------------------------------------------
# tkinter (system package; required even in cloud/CLI mode via customtkinter)
# ---------------------------------------------------------------------------
if ! python3 -c "import tkinter" >/dev/null 2>&1; then
    warn "Python 'tkinter' is missing — noScribe needs it even in cloud/CLI mode."
    if [ "$uname_s" = "Linux" ] && command -v apt-get >/dev/null 2>&1; then
        info "Installing python3-tk (requires sudo)..."
        sudo apt-get install -y python3-tk \
            || warn "Could not auto-install. Run manually: sudo apt-get install -y python3-tk"
    elif [ "$uname_s" = "Darwin" ]; then
        warn "Install a Python that bundles Tk, e.g.: brew install python-tk"
    else
        warn "Install your distro's Tk package for python3 (e.g. python3-tk)."
    fi
    python3 -c "import tkinter" >/dev/null 2>&1 && ok "tkinter is now available."
else
    ok "tkinter is available."
fi

# ---------------------------------------------------------------------------
# Optional: local Whisper models
# ---------------------------------------------------------------------------
if [ "$WITH_MODELS" -eq 1 ]; then
    info "Downloading local Whisper models (this can be several GB)..."
    rm -rf models/fast models/precise
    git clone "$MODEL_FAST_REPO" models/fast
    git clone "$MODEL_PRECISE_REPO" models/precise
    ok "Local models downloaded."
else
    info "Skipping local model download (cloud mode). Re-run with --with-models to add them."
fi

# ---------------------------------------------------------------------------
# Collect cloud credentials
# ---------------------------------------------------------------------------
echo
info "Configure your OpenAI-compatible transcription endpoint."
echo "  These are stored only in ${PROJECT_ROOT}/.env (chmod 600), never committed."
echo

if [ -n "$PRESET_BASE_URL" ]; then
    BASE_URL="$PRESET_BASE_URL"
else
    read -r -p "  Endpoint base URL [${DEFAULT_BASE_URL}]: " BASE_URL
    BASE_URL="${BASE_URL:-$DEFAULT_BASE_URL}"
fi

# API key (hidden input). Required.
API_KEY=""
while [ -z "$API_KEY" ]; do
    read -r -s -p "  API key (Bearer token): " API_KEY
    echo
    [ -z "$API_KEY" ] && warn "The API key cannot be empty."
done

if [ -n "$PRESET_MODEL" ]; then
    MODEL="$PRESET_MODEL"
else
    read -r -p "  Model name [${DEFAULT_MODEL}]: " MODEL
    MODEL="${MODEL:-$DEFAULT_MODEL}"
fi

# Escape single quotes so values are safe inside single-quoted .env entries.
esc() { printf "%s" "$1" | sed "s/'/'\\\\''/g"; }

ENV_FILE="${PROJECT_ROOT}/.env"
umask 177
cat > "$ENV_FILE" <<EOF
# noScribe cloud transcription credentials.
# Loaded by run.sh. Keep this file private — do not commit it.
NOSCRIBE_OPENAI_BASE_URL='$(esc "$BASE_URL")'
NOSCRIBE_OPENAI_API_KEY='$(esc "$API_KEY")'
NOSCRIBE_OPENAI_MODEL='$(esc "$MODEL")'
EOF
chmod 600 "$ENV_FILE"
ok "Credentials written to ${ENV_FILE}"

# Make sure .env is never committed if this checkout is used for development.
if [ -f .gitignore ] && ! grep -qxF '.env' .gitignore 2>/dev/null; then
    printf '\n# Local cloud credentials (added by install.sh)\n.env\n' >> .gitignore
fi

# ---------------------------------------------------------------------------
# Launcher
# ---------------------------------------------------------------------------
LAUNCHER="${PROJECT_ROOT}/run.sh"
cat > "$LAUNCHER" <<'EOF'
#!/usr/bin/env bash
# Launch noScribe with the cloud credentials from .env loaded into the env.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

if [ -f ./.env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
else
    echo "Warning: .env not found; noScribe will use the local model if available." >&2
fi

exec ./venv/bin/python -m noScribe "$@"
EOF
chmod +x "$LAUNCHER"
ok "Launcher written to ${LAUNCHER}"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo
ok "noScribe is installed and configured for cloud transcription."
echo
echo "  Start it with:"
echo "      ${c_green}cd ${PROJECT_ROOT} && ./run.sh${c_reset}"
echo
echo "  To change the endpoint or token later, edit:"
echo "      ${PROJECT_ROOT}/.env"
echo
