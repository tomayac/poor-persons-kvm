# poor-persons-kvm

A minimal, self-hosted "KVM over WiFi" for your own Mac: view the screen and send clicks/keystrokes from a phone or another device on the same local network, built with Flask + pyautogui.

Currently **LAN-only by design** — there's no internet-facing tunnel. Remote access beyond the local network is a possible future addition.

## How it works

- `server.py` runs a small Flask server on the Mac being controlled.
- `GET /screenshot` returns a compressed AVIF (falls back to JPEG) frame of the screen.
- `POST /input/*` endpoints inject mouse/keyboard events via `pyautogui`.
- The served page forwards touch/pointer/keyboard input, with pinch-to-zoom, a virtual cursor overlay, and a fullscreen/compact mode. A dropdown picks the transport: **Polling** (repeated `GET /screenshot` + `POST /input/*`) or **WebSocket** (`/ws`, one persistent connection pushing frames and carrying input as JSON messages) — both fully implemented, Polling is the default.
- Every request (including the page itself) requires a bearer token, checked via `X-Auth-Token` header or `?token=` query param.
- Installable as a PWA (manifest, icons, minimal non-caching service worker) — see below.

## Setup

macOS requires two permissions for the process running this script (Terminal, iTerm, or `python3` itself — whichever you launch it from):

- **Accessibility** — lets `pyautogui` send synthetic clicks/keystrokes.
- **Screen Recording** — lets `pyautogui.screenshot()` capture pixels.

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python server.py
```

The server prints a URL with an auth token, e.g. `http://192.168.x.x:5959/?token=...`. Open that on a phone/laptop browser on the **same WiFi network**.

Set `KVM_TOKEN` in the environment to pin a fixed token across restarts (handy during development); otherwise a fresh random token is generated on every launch.

The browser remembers the token in `localStorage` after a successful visit, so a later bare visit to the domain (no `?token=` in the URL) gets transparently redirected rather than rejected — "Log Out" in Settings clears it. This only actually persists across server restarts if `KVM_TOKEN` is pinned; otherwise the remembered token goes stale the same way a bookmarked URL would, and the sign-in page's field takes a fresh token (or a pasted full URL — it extracts `?token=` from either).

## Running at login (autostart)

```bash
KVM_TOKEN=your-token-here ./scripts/setup-autostart.sh
```

Installs (and immediately starts) a `launchd` LaunchAgent that keeps `server.py` running across reboots/logins. Re-running it (e.g. on another Mac, with that machine's own token) regenerates everything from scratch — nothing machine-specific is hardcoded in the script itself.

launchd-spawned processes don't carry the Screen Recording/Accessibility grants an interactive terminal app already has, and granting them separately means targeting Homebrew's actual interpreter binary, whose path moves every time `python@3.14` gets upgraded. Two things sidestep that:

- The LaunchAgent doesn't run Python directly. It periodically (via `StartInterval`) runs a cheap check — if the server's already listening on its port, it's a no-op; otherwise it tells iTerm (via `osascript`/AppleScript, `create window with default profile command "..."`) to launch it. As iTerm's own child, the server inherits iTerm's already-granted permissions instead of needing its own. The launch command backgrounds the server and `disown`s it, so it survives iTerm's window closing right back down — permission is resolved once at first use, not re-checked against live process ancestry, so the server keeps working fully detached.
- Framework Python re-execs itself through a bundled `Python.app` stub to get WindowServer access (needed for the Quartz-based screenshot/input calls) — which makes it a real, Dock-visible app by default despite having no window. Since it never runs a Cocoa event loop, macOS considers it "unresponsive" (bouncing Dock icon). `server.py` sets its activation policy to "accessory" at startup specifically to suppress this.

The generated LaunchAgent plist and its helper scripts (which do contain the actual token) are written to `~/Library/LaunchAgents` and `~/Library/Application Support/poor-persons-kvm/` — outside this repo, since it's public. The server's own log (`~/Library/Logs/poor-persons-kvm.log`) has no automatic rotation from macOS, so the periodic check trims it to its last ~5MB itself whenever it runs.

The first time this runs, Screen Recording/Accessibility calls will fail (500s from `/health`) until iTerm itself has been granted both in System Settings > Privacy & Security — a one-time step, since iTerm's own app path doesn't move around the way Homebrew's does.

## Installing as an app

The page is an installable PWA — on the phone's browser, use "Add to Home Screen" (iOS Safari share sheet) or the install prompt (Android Chrome). The installed icon's `start_url` bakes in the current auth token, so launching it opens straight into the app.

Two things worth knowing:
- Since `KVM_TOKEN` isn't pinned by default, a server restart mints a new random token and the installed icon's baked-in `start_url` goes stale (reinstall needed). Set `KVM_TOKEN` if you want the install to survive restarts.
- Android Chrome's automatic "Install App" prompt requires a secure context (HTTPS, or the `localhost` exception) — it likely won't trigger over the plain `http://192.168.x.x` LAN address this currently uses. iOS Safari's manual "Add to Home Screen" isn't gated the same way and should work regardless. This resolves itself once the app is served over HTTPS (e.g. after the planned tunnel/reverse-proxy setup).

## Status

Prototype / thought experiment. Not hardened for exposure beyond a trusted local network.
