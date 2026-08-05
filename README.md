# poor-persons-kvm

A minimal, self-hosted "KVM over WiFi" for your own Mac: view the screen and send clicks/keystrokes from a phone or another device on the same local network, built with Flask + pyautogui.

Currently **LAN-only by design** — there's no internet-facing tunnel. Remote access beyond the local network is a possible future addition.

## How it works

- `server.py` runs a small Flask server on the Mac being controlled.
- `GET /screenshot` returns a compressed AVIF (falls back to JPEG) frame of the screen.
- `POST /input/*` endpoints inject mouse/keyboard events via `pyautogui`.
- The served page polls for frames and forwards touch/pointer/keyboard input, with pinch-to-zoom, a virtual cursor overlay, and a fullscreen/compact mode.
- Every request (including the page itself) requires a bearer token, checked via `X-Auth-Token` header or `?token=` query param.

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

## Status

Prototype / thought experiment. Not hardened for exposure beyond a trusted local network.
