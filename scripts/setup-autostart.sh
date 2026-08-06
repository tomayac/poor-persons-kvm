#!/bin/bash
# Installs a launchd LaunchAgent that keeps server.py running across
# reboots/logins. Safe to commit and re-run on any Mac — it derives the
# project path from its own location and takes the auth token from the
# environment, so nothing machine-specific or secret is hardcoded here.
#
# Usage:
#   ./venv/bin/pip install -r requirements.txt   # if not done already
#   KVM_TOKEN=your-token-here ./scripts/setup-autostart.sh
#
# Why this goes through iTerm instead of having launchd run Python
# directly: launchd-spawned processes don't carry the Screen
# Recording/Accessibility grants an interactive terminal app already has,
# and granting them separately means targeting Homebrew's actual
# interpreter binary — a path that moves every time python@3.14 gets
# upgraded. Launching via iTerm (as its own child, backgrounded + disowned
# immediately after) lets the server inherit iTerm's existing, stable
# grants instead. See README.md's "Running at login" section for more.
set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
    echo "This is macOS-only (launchd + iTerm)." >&2
    exit 1
fi

if [[ -z "${KVM_TOKEN:-}" ]]; then
    echo "Set KVM_TOKEN first, e.g.:" >&2
    echo "  KVM_TOKEN=\$(openssl rand -base64 18 | tr -d '=+/') $0" >&2
    exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUPPORT_DIR="$HOME/Library/Application Support/poor-persons-kvm"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs"
PLIST_LABEL="com.tomayac.poor-persons-kvm"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$PLIST_LABEL.plist"

if [[ ! -x "$PROJECT_DIR/venv/bin/python" ]]; then
    echo "No venv at $PROJECT_DIR/venv — run the Setup steps in README.md first." >&2
    exit 1
fi

mkdir -p "$SUPPORT_DIR" "$LAUNCH_AGENTS_DIR" "$LOG_DIR"

cat > "$SUPPORT_DIR/launch_detached.sh" <<EOF
#!/bin/zsh
# Run by iTerm (via launch_in_iterm.applescript) so the server process is
# created as iTerm's child and inherits its already-granted Screen
# Recording + Accessibility permissions. Backgrounding + disown detaches it
# from this shell immediately, so once this script exits (right away) and
# iTerm auto-closes the now-empty window, the server keeps running as an
# orphaned process — verified this doesn't affect its permissions, since
# those are resolved once, not re-checked against live ancestry. No window
# stays open or hidden; nothing bounces in the Dock (server.py itself marks
# its process as a background accessory app).
cd "$PROJECT_DIR" || exit 1
export KVM_TOKEN="$KVM_TOKEN"
nohup ./venv/bin/python server.py >> "$LOG_DIR/poor-persons-kvm.log" 2>&1 &
disown
EOF
chmod +x "$SUPPORT_DIR/launch_detached.sh"

cat > "$SUPPORT_DIR/launch_in_iterm.applescript" <<EOF
-- Addressing "application iTerm" auto-launches it if it isn't already
-- running, so this works the same whether iTerm is already open or this
-- is a cold boot. The window this opens closes itself automatically as
-- soon as launch_detached.sh exits (which it does almost immediately,
-- having backgrounded and disowned the actual server process).
tell application "iTerm"
    create window with default profile command "/bin/zsh -l '$SUPPORT_DIR/launch_detached.sh'"
end tell
EOF

cat > "$SUPPORT_DIR/check_and_launch.sh" <<EOF
#!/bin/zsh
# Invoked by the LaunchAgent at login and every 60s after — a no-op if the
# server's already up, otherwise (cold start or crash) relaunches it via
# iTerm. This indirection through iTerm is what lets the server inherit
# already-granted permissions instead of needing its own grant tied to a
# Homebrew path (see setup-autostart.sh for the full explanation).

# Keep the server's log from growing forever — trim it to its last ~5MB
# whenever this runs, since nothing else rotates it.
LOG="$LOG_DIR/poor-persons-kvm.log"
MAX_BYTES=\$((5 * 1024 * 1024))
if [ -f "\$LOG" ] && [ "\$(stat -f%z "\$LOG" 2>/dev/null || echo 0)" -gt "\$MAX_BYTES" ]; then
    tail -c "\$MAX_BYTES" "\$LOG" > "\$LOG.tmp" && mv "\$LOG.tmp" "\$LOG"
fi

if lsof -i :5959 -sTCP:LISTEN -t >/dev/null 2>&1; then
    exit 0
fi
osascript "$SUPPORT_DIR/launch_in_iterm.applescript"
EOF
chmod +x "$SUPPORT_DIR/check_and_launch.sh"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>$SUPPORT_DIR/check_and_launch.sh</string>
    </array>

    <key>RunAtLoad</key>
    <true/>
    <key>StartInterval</key>
    <integer>60</integer>

    <key>StandardOutPath</key>
    <string>$LOG_DIR/poor-persons-kvm-launcher.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/poor-persons-kvm-launcher.err.log</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST_PATH" >/dev/null

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"

echo "Installed and started. Logs:"
echo "  $LOG_DIR/poor-persons-kvm.log"
echo "  $LOG_DIR/poor-persons-kvm-launcher.log / .err.log"
echo "Grant Screen Recording + Accessibility to iTerm (System Settings >"
echo "Privacy & Security) if the server's screenshots/input don't work yet —"
echo "that's the only manual step, and it's one-time."
