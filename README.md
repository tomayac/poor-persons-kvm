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

## Installing as an app

The page is an installable PWA — on the phone's browser, use "Add to Home Screen" (iOS Safari share sheet) or the install prompt (Android Chrome). The installed icon's `start_url` bakes in the current auth token, so launching it opens straight into the app.

Two things worth knowing:
- Since `KVM_TOKEN` isn't pinned by default, a server restart mints a new random token and the installed icon's baked-in `start_url` goes stale (reinstall needed). Set `KVM_TOKEN` if you want the install to survive restarts.
- Android Chrome's automatic "Install App" prompt requires a secure context (HTTPS, or the `localhost` exception) — it likely won't trigger over the plain `http://192.168.x.x` LAN address this currently uses. iOS Safari's manual "Add to Home Screen" isn't gated the same way and should work regardless. This resolves itself once the app is served over HTTPS (e.g. after the planned tunnel/reverse-proxy setup).

## Status

Prototype / thought experiment. Not hardened for exposure beyond a trusted local network.
