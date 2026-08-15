#!/bin/bash
# Installs a launchd LaunchAgent that keeps an autossh reverse tunnel alive,
# so this Mac stays reachable through Home Assistant's nginx regardless of
# which network it's actually on (home LAN or anywhere else) — nginx ends
# up proxying to 127.0.0.1:<REMOTE_TUNNEL_PORT> on the Home Assistant side,
# which autossh keeps forwarding back to this machine's real server port.
# Safe to commit and re-run on any Mac: no secrets or machine-specific
# values are hardcoded, everything comes from the environment.
#
# Usage:
#   brew install autossh   # if not done already
#   SSH_KEY=~/.ssh/poor-persons-kvm-tunnel-secondary \
#   REMOTE_HOST=tomayac.strangled.net \
#   REMOTE_PORT=23 \
#   REMOTE_USER=hassio \
#   REMOTE_TUNNEL_PORT=15959 \
#   ./scripts/setup-tunnel.sh
#
# REMOTE_TUNNEL_PORT must be different per machine sharing the same Home
# Assistant box (e.g. 15959 for one laptop, 25959 for another) — it's the
# port nginx's proxy_pass will target for THIS machine specifically.
#
# Uses /usr/bin/ssh explicitly rather than whatever "ssh" resolves to on
# $PATH — on at least one Google-managed Mac, /usr/local/bin/ssh silently
# shadows it with gnubby-ssh (corp SSH tooling for Google-internal hosts),
# which accepts -R forwarding requests but never actually relays data for
# non-Google destinations. Bypassing it is what makes the tunnel work at
# all on such machines; harmless on any other Mac.
set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
    echo "This is macOS-only (launchd)." >&2
    exit 1
fi

: "${SSH_KEY:?Set SSH_KEY to the private key path, e.g. ~/.ssh/poor-persons-kvm-tunnel-secondary}"
: "${REMOTE_HOST:?Set REMOTE_HOST, e.g. tomayac.strangled.net}"
: "${REMOTE_PORT:?Set REMOTE_PORT, e.g. 23}"
: "${REMOTE_USER:?Set REMOTE_USER, e.g. hassio}"
: "${REMOTE_TUNNEL_PORT:?Set REMOTE_TUNNEL_PORT — must be unique per machine, e.g. 15959}"
LOCAL_PORT="${LOCAL_PORT:-5959}"

SSH_KEY="$(cd "$(dirname "$SSH_KEY")" && pwd)/$(basename "$SSH_KEY")"
if [[ ! -f "$SSH_KEY" ]]; then
    echo "No such key: $SSH_KEY" >&2
    exit 1
fi

AUTOSSH_BIN="$(command -v autossh || true)"
if [[ -z "$AUTOSSH_BIN" ]]; then
    echo "autossh not found — run: brew install autossh" >&2
    exit 1
fi

LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs"
PLIST_LABEL="com.tomayac.poor-persons-kvm-tunnel"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$PLIST_LABEL.plist"

mkdir -p "$LAUNCH_AGENTS_DIR" "$LOG_DIR"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_LABEL</string>

    <!-- KeepAlive is correct here (unlike the server's own LaunchAgent) —
         autossh is a plain network client with no macOS permission
         entanglements, so launchd can supervise it directly. -->
    <key>ProgramArguments</key>
    <array>
        <string>$AUTOSSH_BIN</string>
        <string>-M</string>
        <string>0</string>
        <string>-N</string>
        <string>-i</string>
        <string>$SSH_KEY</string>
        <string>-p</string>
        <string>$REMOTE_PORT</string>
        <string>-o</string>
        <string>BatchMode=yes</string>
        <string>-o</string>
        <string>ExitOnForwardFailure=yes</string>
        <string>-o</string>
        <string>StrictHostKeyChecking=accept-new</string>
        <string>-o</string>
        <string>ConnectTimeout=10</string>
        <string>-o</string>
        <string>ServerAliveInterval=15</string>
        <string>-o</string>
        <string>ServerAliveCountMax=3</string>
        <string>-R</string>
        <string>$REMOTE_TUNNEL_PORT:localhost:$LOCAL_PORT</string>
        <string>$REMOTE_USER@$REMOTE_HOST</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>AUTOSSH_PATH</key>
        <string>/usr/bin/ssh</string>
        <key>AUTOSSH_GATETIME</key>
        <string>0</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>$LOG_DIR/poor-persons-kvm-tunnel.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/poor-persons-kvm-tunnel.err.log</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST_PATH" >/dev/null

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"

echo "Installed and started. Verify with:"
echo "  ssh -i $SSH_KEY -p $REMOTE_PORT $REMOTE_USER@$REMOTE_HOST \\"
echo "    \"curl -s http://127.0.0.1:$REMOTE_TUNNEL_PORT/health\""
echo "Logs: $LOG_DIR/poor-persons-kvm-tunnel.log / .err.log"
