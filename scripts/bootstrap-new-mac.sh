#!/bin/bash
# One-shot setup for a fresh Mac: clones the repo (or updates it if already
# present), creates the venv, installs dependencies, installs the autostart
# LaunchAgent (scripts/setup-autostart.sh), and optionally sets up the
# reverse SSH tunnel for remote access (scripts/setup-tunnel.sh) too.
#
# Safe to commit and re-run on any Mac — like the scripts it wraps, nothing
# machine-specific or secret is hardcoded; everything comes from the
# environment or is generated fresh on first run.
#
# Usage (on the new Mac, no local clone needed first):
#   curl -fsSL https://raw.githubusercontent.com/tomayac/poor-persons-kvm/main/scripts/bootstrap-new-mac.sh -o bootstrap.sh
#   chmod +x bootstrap.sh
#   ./bootstrap.sh
#
# Everything below is overridable via environment variables; sane defaults
# are picked otherwise (a fresh random KVM_TOKEN, ~/Documents/websites/
# poor-persons-kvm as the install dir, a dedicated new SSH key per machine
# if the tunnel is set up). REMOTE_TUNNEL_PORT defaults to 25959 rather
# than the README's other example (15959) specifically so this script is
# safe to run on a second/third machine sharing the same remote host
# without colliding with an existing tunnel — override it if that's still
# not free.
set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
    echo "This script is macOS-only." >&2
    exit 1
fi

REPO_URL="${REPO_URL:-https://github.com/tomayac/poor-persons-kvm.git}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/Documents/websites/poor-persons-kvm}"
KVM_TOKEN="${KVM_TOKEN:-$(openssl rand -base64 18 | tr -d '=+/')}"

echo "== poor-persons-kvm bootstrap =="
echo "Install dir: $INSTALL_DIR"
echo

if ! xcode-select -p >/dev/null 2>&1; then
    echo "Xcode Command Line Tools (needed for git/python3) aren't installed." >&2
    echo "Run: xcode-select --install" >&2
    exit 1
fi

if [[ ! -d "/Applications/iTerm.app" ]]; then
    echo "Warning: iTerm.app wasn't found in /Applications." >&2
    echo "The autostart LaunchAgent launches the server through iTerm" >&2
    echo "specifically (see README.md) — install it from https://iterm2.com" >&2
    echo "before the autostart step will actually work." >&2
    echo
fi

# ---- 1. Clone or update the repo ----
if [[ -d "$INSTALL_DIR/.git" ]]; then
    echo "Repo already exists at $INSTALL_DIR — pulling latest..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    echo "Cloning into $INSTALL_DIR..."
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

# ---- 2. Python venv + deps ----
if [[ ! -d venv ]]; then
    echo "Creating venv..."
    python3 -m venv venv
fi
echo "Installing Python dependencies..."
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt

# ---- 3. Autostart (LaunchAgent) ----
echo
echo "Installing autostart LaunchAgent..."
KVM_TOKEN="$KVM_TOKEN" ./scripts/setup-autostart.sh

# ---- 4. Optional: reverse tunnel for remote access ----
if [[ -z "${SETUP_TUNNEL:-}" ]]; then
    read -r -p "Set up remote access via SSH tunnel too? [y/N] " REPLY
    case "$REPLY" in
        [Yy]*) SETUP_TUNNEL="yes" ;;
        *) SETUP_TUNNEL="no" ;;
    esac
fi

if [[ "$SETUP_TUNNEL" == "yes" ]]; then
    if ! command -v brew >/dev/null 2>&1; then
        echo "Homebrew not found — install it first: https://brew.sh" >&2
        exit 1
    fi
    if ! command -v autossh >/dev/null 2>&1; then
        echo "Installing autossh..."
        brew install autossh
    fi

    SSH_KEY="${SSH_KEY:-$HOME/.ssh/poor-persons-kvm-tunnel-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
    REMOTE_HOST="${REMOTE_HOST:-tomayac.strangled.net}"
    REMOTE_PORT="${REMOTE_PORT:-23}"
    REMOTE_USER="${REMOTE_USER:-hassio}"
    REMOTE_TUNNEL_PORT="${REMOTE_TUNNEL_PORT:-25959}"

    if [[ ! -f "$SSH_KEY" ]]; then
        echo "Generating a dedicated SSH key for this machine's tunnel at $SSH_KEY..."
        ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "poor-persons-kvm-tunnel-$(hostname -s)"
    fi

    echo
    echo "############################################################"
    echo "# ACTION NEEDED before the tunnel will work:"
    echo "# Add this public key to ${REMOTE_USER}@${REMOTE_HOST}'s authorized_keys"
    echo "# (e.g. via the Home Assistant SSH/Terminal add-on's shell):"
    echo "############################################################"
    cat "$SSH_KEY.pub"
    echo "############################################################"
    read -r -p "Press Enter once the key has been added (Ctrl-C to stop here and re-run this script later)... "

    SSH_KEY="$SSH_KEY" REMOTE_HOST="$REMOTE_HOST" REMOTE_PORT="$REMOTE_PORT" \
        REMOTE_USER="$REMOTE_USER" REMOTE_TUNNEL_PORT="$REMOTE_TUNNEL_PORT" \
        ./scripts/setup-tunnel.sh
fi

echo
echo "== Done =="
echo "Local KVM token: $KVM_TOKEN"
echo "(also baked into the LaunchAgent, so you won't need to remember it —"
echo "but it's handy to have while testing the URL below)"
echo
LOCAL_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "<this-macs-lan-ip>")"
echo "Once permissions are granted (see below), open:"
echo "  http://$LOCAL_IP:5959/?token=$KVM_TOKEN"
echo
echo "Remaining manual steps:"
echo "  1. The first time the server actually runs, macOS will prompt for"
echo "     Accessibility + Screen Recording permissions — grant BOTH to"
echo "     iTerm (System Settings > Privacy & Security). Until then,"
echo "     /health returns 500 and the video/input won't work."
if [[ "${SETUP_TUNNEL:-no}" == "yes" ]]; then
echo "  2. On the Home Assistant side, add an nginx server block (or"
echo "     equivalent) proxying a hostname of your choice to"
echo "     172.30.32.1:${REMOTE_TUNNEL_PORT:-25959} — same pattern as any"
echo "     other machine already tunneled to that host — so this machine"
echo "     gets its own public URL."
fi
