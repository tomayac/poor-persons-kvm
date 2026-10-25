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
# Why launchd starts the server through a launcher app: macOS ties the Screen Recording/Accessibility grants of
# a launchd-spawned process to Homebrew's actual interpreter binary, a
# path that moves every time python@3.14 gets upgraded. A tiny AppleScript
# applet, built once and granted both permissions once, starts the server
# as its own child, so the server inherits the applet's stable grants.
# The applet is a background-only app (LSUIElement) launched hidden with
# `open -gj`, so no window or Dock icon ever shows up. See README.md's
# "Running at login" section for more.
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
APP_PATH="$SUPPORT_DIR/Poor Persons KVM.app"

if [[ ! -x "$PROJECT_DIR/venv/bin/python" ]]; then
    echo "No venv at $PROJECT_DIR/venv — run the Setup steps in README.md first." >&2
    exit 1
fi

mkdir -p "$SUPPORT_DIR" "$LAUNCH_AGENTS_DIR" "$LOG_DIR"

cat > "$SUPPORT_DIR/launch_detached.sh" <<EOF
#!/bin/zsh
# Run by the launcher app so the server process is created as the app's
# child and inherits its already-granted Screen Recording + Accessibility
# permissions. Backgrounding + disown detaches it from this shell
# immediately, so the app quits right away and the server keeps running
# as an orphaned process. That doesn't affect its permissions, since macOS
# resolves those once at launch. Every output
# stream points away from the app's pipe, which \`do shell script\` would
# otherwise wait on until the server exits.
cd "$PROJECT_DIR" || exit 1
export KVM_TOKEN="$KVM_TOKEN"
nohup ./venv/bin/python server.py >> "$LOG_DIR/poor-persons-kvm.log" 2>&1 < /dev/null &
disown
EOF
chmod +x "$SUPPORT_DIR/launch_detached.sh"

# Left over from when the server was launched through an iTerm window.
rm -f "$SUPPORT_DIR/launch_in_iterm.applescript"

# Built only once: macOS ties the permission grants to the app's code
# signature, so rebuilding it would silently revoke them. The app holds
# nothing but the path to launch_detached.sh, so a new token doesn't need
# a rebuild. Delete the app to force one, then grant it again.
if [[ ! -d "$APP_PATH" ]]; then
    osacompile -o "$APP_PATH" \
        -e "do shell script \"/bin/zsh \" & quoted form of \"$SUPPORT_DIR/launch_detached.sh\""
    # Background-only, so launching it never shows a Dock icon or menu bar.
    /usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$APP_PATH/Contents/Info.plist"
    /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $PLIST_LABEL.launcher" "$APP_PATH/Contents/Info.plist" 2>/dev/null ||
        /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string $PLIST_LABEL.launcher" "$APP_PATH/Contents/Info.plist"
    # Editing Info.plist invalidated osacompile's signature, so re-sign it.
    codesign --force --sign - "$APP_PATH"
    NEW_APP=yes
fi

cat > "$SUPPORT_DIR/check_and_launch.sh" <<EOF
#!/bin/zsh
# Invoked by the LaunchAgent at login and every 60s after — a no-op if the
# server's already up AND actually responding, otherwise (cold start,
# crash, or a hang that still holds the port open) relaunches it via the
# launcher app. This indirection lets the server inherit the app's
# already-granted permissions, which stay put across Homebrew upgrades
# (see setup-autostart.sh for the full explanation).

# Keep the server's log from growing forever — trim it to its last ~5MB
# whenever this runs, since nothing else rotates it.
LOG="$LOG_DIR/poor-persons-kvm.log"
MAX_BYTES=\$((5 * 1024 * 1024))
if [ -f "\$LOG" ] && [ "\$(stat -f%z "\$LOG" 2>/dev/null || echo 0)" -gt "\$MAX_BYTES" ]; then
    tail -c "\$MAX_BYTES" "\$LOG" > "\$LOG.tmp" && mv "\$LOG.tmp" "\$LOG"
fi

if lsof -i :5959 -sTCP:LISTEN -t >/dev/null 2>&1; then
    # Something's listening — but a hung process (a macOS screen-capture
    # API deadlock, etc.) can hold the port open for days without ever
    # answering a request, and a plain "is the port open" check can't
    # tell the difference from a healthy server. A real request can.
    if curl -s -o /dev/null -m 5 "http://127.0.0.1:5959/health?token=$KVM_TOKEN"; then
        exit 0
    fi
    lsof -i :5959 -sTCP:LISTEN -t | xargs kill -9 2>/dev/null
    sleep 1
fi
# -g keeps it in the background, -j launches it hidden.
open -gj "$APP_PATH"
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
if [[ "${NEW_APP:-no}" == "yes" ]]; then
    echo
    echo "Built a new launcher app at:"
    echo "  $APP_PATH"
    echo "Grant it Screen Recording + Accessibility (System Settings >"
    echo "Privacy & Security, add it with the + button), then restart the"
    echo "server so it picks the grants up. That's the only manual step, and"
    echo "it's one-time."
fi
