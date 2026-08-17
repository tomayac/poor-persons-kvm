"""Poor person's KVM — LAN-only remote view + input for your own Mac.

Run this on the Mac you want to control. Open the printed URL (which
includes an auth token) from a phone/laptop browser on the SAME WiFi
network. There is no internet-facing tunnel here on purpose — this is
local-network only for now.

Permissions required (System Settings > Privacy & Security):
  - Accessibility: lets pyautogui send clicks/keystrokes.
  - Screen Recording: lets pyautogui.screenshot() capture pixels.
Both are granted to whatever process runs this script (e.g. Terminal.app,
iTerm, or python3 itself) — keep using the same terminal app each time.
"""
import hashlib
import hmac
import io
import json
import os
import secrets
import socket
import threading
import time

import pyautogui
import Quartz
from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
from flask import Flask, Response, abort, jsonify, request, send_file
from flask_sock import Sock
from PIL import Image

from generate_icons import draw_icon

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.01

# Framework Python re-execs itself through Resources/Python.app to get
# WindowServer access (needed for the Quartz screenshot/input calls below),
# which makes it a real, Dock-visible application by default — but since
# this process never runs an actual Cocoa event loop, macOS sees it as
# unresponsive (bouncing Dock icon, "Application Not Responding"). Marking
# it as an accessory app tells macOS not to expect a Dock presence or an
# event loop from it at all.
NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)

# static_folder=None: the only "static" assets are the three PWA icons
# below, which are now generated per-request (see machine_icon_color())
# rather than served as files, so there's nothing left for Flask's default
# /static/<path:filename> auto-route to serve.
app = Flask(__name__, static_folder=None)
sock = Sock(app)

# Set KVM_TOKEN in the environment to pin a fixed token across restarts
# (handy during development); otherwise a fresh random one is generated
# on every launch.
AUTH_TOKEN = os.environ.get("KVM_TOKEN") or secrets.token_urlsafe(24)

# Streaming rate over /ws is self-adjusting by default ("optimal" — see
# ws_handler): the server never sends a frame until the client has acked the
# previous one, so the rate naturally settles to whatever the connection can
# actually sustain instead of guessing a fixed interval. "Slow" mode adds a
# deliberate floor on top of that, for when you'd rather conserve bandwidth
# than get the fastest possible updates.
WS_SLOW_INTERVAL = float(os.environ.get("KVM_WS_SLOW_INTERVAL", "2.0"))
# Safety cap: if a frame_ack never arrives (e.g. one got dropped), don't
# stall the sender forever — proceed anyway after this long. Actual dead
# connections are caught by ws.send() itself failing, not this.
MAX_FRAME_ACK_WAIT = 5.0

# Fixed resolution choices, keyed by the value the client's <select> sends —
# each a cap on the image's longer edge in pixels (aspect-preserving);
# "full" (None) sends the capture untouched. "auto" isn't in here: it's not
# a single value but a live, per-connection choice the WS frame sender
# steps between at runtime — see AUTO_RESOLUTION_TIERS below.
RESOLUTION_TIERS = {
    "full": None,
    "2560": 2560,
    "1920": 1920,
    "1280": 1280,
}
# Same idea, ordered best (index 0, full res) to worst, for "auto" mode to
# step through. Only meaningful over WS, which has a persistent connection
# to adapt over time; polling's "auto" just means "full" (see /screenshot)
# — polling already gets simpler treatment than WS throughout this app
# (e.g. no staleness detection, no ack-gated pacing), and building a second,
# independent adaptive mechanism for it wasn't worth it given WS is the
# default transport.
AUTO_RESOLUTION_TIERS = [None, 2560, 1920, 1280, 854]
# A real frame's full round trip (capture + resize + hash + encode +
# network + client ack) beyond this suggests the connection is struggling —
# step down a tier. Comfortably under this for several frames in a row
# suggests there's headroom to try stepping back up. Capture alone is
# ~250-300ms on this machine regardless of resolution (measured; it's not
# something resizing affects), so these thresholds have to sit meaningfully
# above that floor rather than close to zero.
AUTO_STEP_DOWN_S = 0.9
AUTO_STEP_UP_S = 0.4
AUTO_STEP_UP_STREAK = 5

# cmd/alt are Mac-friendly aliases for pyautogui's actual key names.
KEY_ALIASES = {
    "cmd": "command",
    "alt": "option",
    "return": "enter",
    "esc": "escape",
}


def pag(fn, *args, **kwargs):
    """Call a pyautogui function, retrying on KeyError.

    pyobjc's lazy Quartz symbol binding can transiently KeyError the very
    first time a given Quartz function (e.g. CGEventCreateMouseEvent) is
    resolved in this process, then works reliably every call after. Retrying
    once or twice absorbs that one-time hiccup instead of surfacing a 500 on
    a user's first click.
    """
    last_err = None
    for _ in range(3):
        try:
            return fn(*args, **kwargs)
        except KeyError as e:
            last_err = e
    raise last_err


# macOS's screen-capture API errors ("could not create image from display")
# when hit by overlapping calls — with threaded=True, polling tabs, /health
# checks, and the /ws frame sender can all land at once. Capture itself is
# serialized by this; see input_lock below for why keyboard input isn't
# actually safe to leave fully concurrent the way this comment used to claim.
screenshot_lock = threading.Lock()

# do_text()/do_key()/do_doubleclick() each post a *sequence* of synthetic OS
# events (one keystroke per character, a hotkey's down+down+up+up, a scripted
# click-click) — safe when the whole sequence runs uninterrupted, but Flask's
# threaded=True (needed for capture/input concurrency generally) means two of
# these could genuinely run at once on separate threads: an HTTP polling
# client's own concurrent requests, or a WS text send racing a same-connection
# key press. An interleaved sequence can leave a modifier key logically stuck
# "down" from the OS's point of view — this is suspected to be exactly what
# briefly made macOS's own Control-tap Dictation shortcut fire once, and
# garbled at least one character send. Serializing the actual event-posting
# (not the whole request/dispatch, which still returns promptly) closes that
# without reintroducing the latency problem this was blocking on.
input_lock = threading.Lock()

# The client maps clicks to the streamed image's own pixel dimensions
# (naturalWidth/naturalHeight), but pyautogui.moveTo() expects logical
# points (pyautogui.size()). Those are equal on a non-Retina display or at
# "full" resolution on some Retina configurations, but not on a true 2x
# Retina display sending anything less than full res (e.g. a 3420x2224
# capture resized to 1920 wide for a 1710x1112-point screen) — without a
# scaling correction, clicks land at the wrong distance from the top-left.
# Updated only by resize_for_stream() below, i.e. only for frames actually
# sent to the client for display — deliberately NOT inside take_screenshot()
# itself, since /health also calls that (to verify capture works at all)
# independent of whatever resolution the video stream is currently using;
# if it updated this too, a health poll landing between two real frames
# would transiently clobber it back to native size and briefly send clicks
# to the wrong place whenever streaming below full resolution.
_last_screenshot_size = None


def take_screenshot():
    with screenshot_lock:
        return pag(pyautogui.screenshot)


def resize_for_stream(img, max_dim):
    """Downscale img so its longer edge is at most max_dim px (aspect
    preserving) before it's actually hashed/encoded/sent to the client —
    "full" resolution (max_dim=None) or an already-smaller capture is a
    no-op. Always updates _last_screenshot_size to match, since that's what
    scale_to_screen() needs — see its comment for why this must be the only
    place that happens.
    """
    global _last_screenshot_size
    if max_dim is not None and max(img.size) > max_dim:
        ratio = max_dim / max(img.size)
        new_size = (round(img.width * ratio), round(img.height * ratio))
        img = img.resize(new_size, Image.LANCZOS)
    _last_screenshot_size = img.size
    return img


def scale_to_screen(x, y):
    """Map a click's screenshot-pixel coordinates to pyautogui's logical points."""
    if _last_screenshot_size is None:
        return x, y
    screen_w, screen_h = pyautogui.size()
    shot_w, shot_h = _last_screenshot_size
    if shot_w == screen_w and shot_h == screen_h:
        return x, y
    return x * screen_w / shot_w, y * screen_h / shot_h


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't actually send any packets, just asks the OS to resolve
        # which local interface would be used to route there.
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


@app.before_request
def check_auth():
    # /offline.html is intentionally reachable with no token at all — it
    # reveals nothing (static "can't reach Home Assistant" markup, no screen
    # data or control), and the service worker needs to be able to precache
    # and serve it unconditionally, including on a device that's never
    # actually completed the token check.
    if request.path == "/offline.html":
        return
    token = request.args.get("token") or request.headers.get("X-Auth-Token")
    if not token or not hmac.compare_digest(token, AUTH_TOKEN):
        abort(401)


# Served with no auth check at all (see check_auth's exemption below) and
# precached by the service worker's install handler, so it's available even
# on a device that's never successfully reached the token check — the whole
# point is being reachable when literally nothing else is. Distinct from the
# real app shell's own "Mac unreachable" state (which needs Home Assistant
# itself to be up, just not the Mac) — this is what the service worker falls
# back to specifically when the network request itself fails outright, i.e.
# Home Assistant isn't reachable either.
OFFLINE_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>APP_TITLE_PLACEHOLDER — Offline</title>
    <style>
        body {
            margin: 0; min-height: 100vh; display: flex; flex-direction: column; align-items: center;
            justify-content: center; gap: 10px; text-align: center; padding: 24px;
            background: #111; color: #eee; font-family: -apple-system, sans-serif;
        }
        b { font-size: 17px; }
        p { color: #999; font-size: 13px; margin: 0; max-width: 320px; }
        button {
            margin-top: 8px; padding: 10px 20px; border-radius: 6px; border: 1px solid #555;
            background: #2a63c9; border-color: #2a63c9; color: #fff; font-size: 15px;
            user-select: none; -webkit-user-select: none;
        }
    </style>
</head>
<body>
    <b>⚠ Can't reach Home Assistant</b>
    <p>Check this device's internet connection. This page will reload on its own once it's back — or tap below to try right now.</p>
    <button id="retryBtn">Retry now</button>
    <script>
        document.getElementById('retryBtn').addEventListener('click', () => location.reload());
        // Reloading (rather than probing connectivity separately) is the
        // simplest reliable check here: the service worker's own fetch
        // handler already knows how to route a successful reload to
        // whichever state is actually correct (the real app, or "Mac
        // unreachable" if just Home Assistant is up) — this page doesn't
        // need to duplicate that logic, just retry until one of those
        // succeeds. navigator.onLine only reflects the local network
        // interface, not real internet reachability (e.g. Wi-Fi connected
        // but the router itself has no uplink), so the periodic retry is
        // the real safety net; 'online' is just a fast path when it fires.
        window.addEventListener('online', () => location.reload());
        setInterval(() => location.reload(), 10000);
    </script>
</body>
</html>
"""


LOGIN_SHELL_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>APP_TITLE_PLACEHOLDER — Sign in</title>
    <style>
        body {
            margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
            background: #111; color: #eee; font-family: -apple-system, sans-serif;
        }
        form { display: flex; flex-direction: column; gap: 12px; width: min(90vw, 360px); }
        input, button { padding: 10px 12px; border-radius: 6px; border: 1px solid #555; font-size: 15px; }
        input { background: #222; color: #eee; }
        button { background: #2a63c9; color: #fff; border-color: #2a63c9; user-select: none; -webkit-user-select: none; }
        p { color: #999; font-size: 13px; margin: 0; }
    </style>
</head>
<body>
    <form id="loginForm">
        <b>APP_TITLE_PLACEHOLDER</b>
        <input id="tokenField" placeholder="Access token (or paste the full URL)" autocomplete="off" autocapitalize="off">
        <button type="submit">Connect</button>
        <p>Find this in the terminal output where the server was started.</p>
    </form>
    <script>
        (function () {
            const params = new URLSearchParams(location.search);
            if (params.get('token')) {
                // A token was already tried (in the URL, or from a previous
                // localStorage redirect below) and it still landed here —
                // don't loop forever retrying the same bad value, just ask.
                localStorage.removeItem('kvmToken');
            } else {
                const saved = localStorage.getItem('kvmToken');
                if (saved) {
                    location.replace('/?token=' + encodeURIComponent(saved));
                    return;
                }
            }
            document.getElementById('loginForm').addEventListener('submit', (e) => {
                e.preventDefault();
                let value = document.getElementById('tokenField').value.trim();
                const m = value.match(/[?&]token=([^&]+)/);
                if (m) value = decodeURIComponent(m[1]);
                if (!value) return;
                location.href = '/?token=' + encodeURIComponent(value);
            });
        })();
    </script>
</body>
</html>
"""


@app.errorhandler(401)
def unauthorized(_e):
    # A plain fetch()/XHR call (all our /screenshot, /input/*, etc. routes)
    # should keep getting a plain JSON 401. Only the page itself needs the
    # login shell — that's what actually gets navigated to by the browser.
    if request.path == "/":
        html = LOGIN_SHELL_HTML.replace("APP_TITLE_PLACEHOLDER", app_title())
        return Response(html, mimetype="text/html"), 401
    return jsonify({"error": "unauthorized"}), 401


@app.route("/")
def index():
    # pyautogui.size() just queries display info (no capture involved) — a
    # few ms, not the ~1-2s a screenshot costs — so it's fine to call on
    # every page load. Baking the real aspect ratio into the CSS lets the
    # browser reserve #screenWrap's correct height before the first frame
    # ever arrives, instead of it collapsing to ~0 and then jumping once an
    # image with real dimensions finally loads.
    try:
        screen_w, screen_h = pyautogui.size()
        aspect_ratio = f"{screen_w} / {screen_h}"
    except Exception:
        aspect_ratio = "16 / 9"
    html = (
        HTML_PAGE.replace("TOKEN_PLACEHOLDER", AUTH_TOKEN)
        .replace("SCREEN_ASPECT_PLACEHOLDER", aspect_ratio)
        .replace("APP_TITLE_PLACEHOLDER", app_title())
        .replace("APP_SHORT_TITLE_PLACEHOLDER", app_short_title())
    )
    return Response(html, mimetype="text/html")


@app.route("/offline.html")
def offline():
    html = OFFLINE_HTML.replace("APP_TITLE_PLACEHOLDER", app_title())
    return Response(html, mimetype="text/html")


# Both machines in this deployment run this exact same server.py (see
# kvm.conf's two server blocks, each proxying a different hostname to a
# different Mac) — the only thing that tells them apart, from the server's
# own point of view, is which hostname a client used to reach it, which
# nginx forwards transparently via the Host header. Used purely to give
# each machine's installed PWA a visibly different icon/name so they're
# distinguishable on a phone's home screen — no separate per-machine
# config needed, and any other hostname (plain LAN access, etc.) just
# gets the original default look.
MACHINE_COLORS = {
    "Primary": (42, 99, 201, 255),     # the app's existing accent blue
    "Secondary": (201, 130, 42, 255),  # amber — clearly distinct from blue
}
DEFAULT_ICON_COLOR = MACHINE_COLORS["Primary"]


def machine_label():
    host = (request.host or "").lower()
    if "primary" in host:
        return "Primary"
    if "secondary" in host:
        return "Secondary"
    return None


def machine_icon_color():
    return MACHINE_COLORS.get(machine_label(), DEFAULT_ICON_COLOR)


def app_title():
    label = machine_label()
    return f"Poor Person's KVM ({label})" if label else "Poor Person's KVM"


def app_short_title():
    label = machine_label()
    return f"KVM {label}" if label else "KVM"


def _icon_response(size):
    img = draw_icon(machine_icon_color())
    if size != 512:
        img = img.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    resp = send_file(buf, mimetype="image/png")
    # Constant for a given host — cheap to regenerate, but no reason not to
    # let the browser skip asking again for a while.
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.route("/static/icon-192.png")
def icon_192():
    return _icon_response(192)


@app.route("/static/icon-512.png")
def icon_512():
    return _icon_response(512)


@app.route("/static/apple-touch-icon.png")
def apple_touch_icon():
    return _icon_response(180)


@app.route("/manifest.json")
def manifest():
    # start_url carries the token so tapping the installed home-screen icon
    # opens straight into the authenticated app — no re-entering anything.
    # Note: this token is only as durable as AUTH_TOKEN itself. Without
    # KVM_TOKEN pinned in the environment, a server restart mints a new
    # random token and the installed icon's start_url goes stale (reinstall
    # needed) — set KVM_TOKEN if you want the install to survive restarts.
    icon = lambda name, purpose: {  # noqa: E731
        "src": f"/static/{name}?token={AUTH_TOKEN}",
        "type": "image/png",
        "purpose": purpose,
    }
    manifest_json = {
        "name": app_title(),
        "short_name": app_short_title(),
        "description": "Self-hosted remote view + control for your own Mac",
        "start_url": f"/?token={AUTH_TOKEN}",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#111",
        "theme_color": "#111",
        "icons": [
            {**icon("icon-192.png", "any"), "sizes": "192x192"},
            {**icon("icon-192.png", "maskable"), "sizes": "192x192"},
            {**icon("icon-512.png", "any"), "sizes": "512x512"},
            {**icon("icon-512.png", "maskable"), "sizes": "512x512"},
        ],
    }
    return jsonify(manifest_json)


@app.route("/sw.js")
def service_worker():
    # Network-first, and ONLY for the static app shell (this page, the
    # manifest, icons). Screenshots/health/input stay pure network-only —
    # a stale cached "fallback" for any of those would be actively
    # misleading in a live remote-control tool, and /screenshot's
    # cache-busted URLs would otherwise grow the cache without bound.
    # skipWaiting + clients.claim make a new SW version take over
    # immediately instead of waiting for every tab to close; the page
    # listens for the resulting controllerchange and reloads itself so an
    # update is never silently stuck on old JS.
    js = """
const CACHE_NAME = 'kvm-shell-v2';
const SHELL_PATHS = ['/', '/manifest.json', '/sw.js', '/offline.html'];

function isShellRequest(url) {
    const path = new URL(url).pathname;
    return SHELL_PATHS.includes(path) || path.startsWith('/static/');
}

self.addEventListener('install', (e) => {
    // Precached unconditionally (not lazily on first successful fetch like
    // the rest of the shell below) so it's available even on a device that
    // has never once loaded the app while genuinely online — the whole
    // point of this page is being there when nothing else works. If this
    // very fetch fails (installing while already offline), the install
    // itself fails and the standard SW lifecycle just retries on a later
    // visit — nothing more to do about that here.
    e.waitUntil(
        caches.open(CACHE_NAME).then((cache) => cache.add('/offline.html')).then(() => self.skipWaiting())
    );
});
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('fetch', (e) => {
    if (e.request.method === 'GET' && isShellRequest(e.request.url)) {
        e.respondWith(
            fetch(e.request)
                .then((response) => {
                    if (!response.ok) {
                        // The proxy answered (Home Assistant is reachable),
                        // but its own upstream — the Mac itself — didn't
                        // (502/504 while the tunnel/laptop is down/asleep).
                        // Don't cache this error page over a previously-good
                        // shell, and don't serve it either — fall back to
                        // the last real one, same as an outright network
                        // failure below. The cached shell's own JS still
                        // polls /health once it loads, which already
                        // reports "Mac unreachable" for exactly this case —
                        // no separate offline view needed, just not
                        // clobbering the real one with a proxy error page.
                        return caches.match(e.request).then((cached) => cached || response);
                    }
                    const copy = response.clone();
                    // waitUntil keeps the worker alive for this write — without
                    // it the browser can terminate the worker right after the
                    // response above resolves, before the cache is ever updated.
                    e.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.put(e.request, copy)));
                    return response;
                })
                .catch(() => {
                    // fetch() itself rejected — no HTTP response at all,
                    // meaning Home Assistant isn't reachable either (not
                    // just the Mac, handled above via response.ok). The
                    // cached real shell can't do anything useful here (no
                    // health polling or WS reconnect target exists at all
                    // without a network path), so this is the dedicated
                    // "can't reach Home Assistant" page, precached on
                    // install for exactly this case, rather than a
                    // fully-interactive-looking shell that would just fail
                    // every single action.
                    return caches.match('/offline.html').then((offline) => offline || new Response('Offline', { status: 503 }));
                })
        );
        return;
    }
    e.respondWith(fetch(e.request));
});
"""
    return Response(js, mimetype="application/javascript")


@app.route("/health")
def health():
    try:
        img = take_screenshot()
        min_val, max_val = img.convert("L").getextrema()
        blank = (max_val - min_val) < 3
        return jsonify({
            "screenshot_ok": not blank,
            "size": [img.width, img.height],
            "warning": "capture looks blank — Screen Recording permission "
                       "may be denied" if blank else None,
        })
    except Exception as e:
        return jsonify({"screenshot_ok": False, "error": str(e)}), 500


def frame_hash(img):
    """Fast fixed-size fingerprint of the raw pixels, used to detect an
    unchanged screen without depending on JPEG/AVIF encoders being
    byte-for-byte deterministic (comparing encoded output directly would be
    a bet on encoder internals we don't need to make)."""
    return hashlib.blake2b(img.tobytes(), digest_size=16).hexdigest()


def encode_frame(img, prefer_speed=False):
    """Encode an already-captured screenshot — split out from capturing so
    callers can hash the raw pixels first and skip encoding entirely when
    the frame hasn't changed (see /screenshot's ETag handling and /ws's
    frame_sender).

    AVIF compresses much smaller than JPEG (good for bandwidth), but on this
    machine encoding it costs ~500ms+ versus JPEG's ~15ms — measured
    directly, not assumed — which single-handedly dominates per-frame
    latency once capture (~1-2s) is added. "optimal" mode's whole point is
    speed, so it uses JPEG; "slow" mode already isn't optimizing for
    latency, so it keeps AVIF for the smaller payload.
    """
    buf = io.BytesIO()
    if prefer_speed:
        img.convert("RGB").save(buf, "JPEG", quality=70)
        return buf.getvalue(), "image/jpeg"
    try:
        img.save(buf, "AVIF", quality=55)
        return buf.getvalue(), "image/avif"
    except Exception:
        # Fall back to JPEG if AVIF isn't available in this Pillow build.
        # JPEG has no alpha channel, but macOS screenshots come back as RGBA
        # — drop the alpha or Pillow's JPEG encoder raises KeyError('RGBA').
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=70)
        return buf.getvalue(), "image/jpeg"


@app.route("/screenshot")
def screenshot():
    try:
        img = take_screenshot()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # "auto" isn't handled per-request here the way WS's frame sender does
    # it over its persistent connection — see AUTO_RESOLUTION_TIERS' comment
    # — so it, and any unrecognized value, just means "full".
    img = resize_for_stream(img, RESOLUTION_TIERS.get(request.args.get("resolution"), None))

    etag = frame_hash(img)
    # Cache-Control: no-cache means "always ask the server first" (not "don't
    # cache") — the browser sends If-None-Match on every poll, so an
    # unchanged screen costs a small conditional request instead of a full
    # image transfer + decode. Requires the client to stop cache-busting the
    # URL with a timestamp, or every request looks like a different resource.
    if request.headers.get("If-None-Match") == etag:
        resp = Response(status=304)
    else:
        data, mimetype = encode_frame(img, prefer_speed=request.args.get("rate") == "optimal")
        resp = send_file(io.BytesIO(data), mimetype=mimetype)
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def resolve_button(name):
    return name if name in ("left", "right", "middle") else "left"


# pyautogui.mouseDown()/mouseUp() never set the CGEvent click-state field on
# macOS, so the OS (and apps) see every click as click #1 — two separate,
# genuinely fast down/up pairs still don't register as a double-click,
# no matter how quickly they arrive. Fixed by posting the CGEvents
# ourselves with that field set correctly, using the same timing+distance
# heuristic real click hardware relies on for multi-click recognition.
try:
    from AppKit import NSEvent
    DOUBLE_CLICK_INTERVAL = NSEvent.doubleClickInterval()
except Exception:
    DOUBLE_CLICK_INTERVAL = 0.5
DOUBLE_CLICK_DISTANCE = 5  # px tolerance for "still the same spot"

_MOUSE_EVENT_TYPES = {
    "left": (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGMouseButtonLeft),
    "right": (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp, Quartz.kCGMouseButtonRight),
    "middle": (Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp, Quartz.kCGMouseButtonCenter),
}

_click_state = {"time": 0.0, "pos": None, "button": None, "count": 0}


def _next_click_count(pos, button):
    now = time.time()
    same_spot = (
        _click_state["pos"] is not None
        and abs(_click_state["pos"][0] - pos[0]) <= DOUBLE_CLICK_DISTANCE
        and abs(_click_state["pos"][1] - pos[1]) <= DOUBLE_CLICK_DISTANCE
    )
    if _click_state["button"] == button and same_spot and now - _click_state["time"] <= DOUBLE_CLICK_INTERVAL:
        count = _click_state["count"] + 1
    else:
        count = 1
    _click_state.update(time=now, pos=pos, button=button, count=count)
    return count


def _post_mouse_event(event_type, pos, button_const, click_count):
    event = Quartz.CGEventCreateMouseEvent(None, event_type, pos, button_const)
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, click_count)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


# Shared input handlers — called from both the HTTP /input/* routes (for
# "Polling" transport) and the /ws message loop (for "WebSocket" transport),
# so the two transports can't drift in behavior.

def do_mousedown(button, x=None, y=None):
    # x/y are optional: the Left/Right Click buttons press wherever the
    # cursor already is (positioned by prior mousemove calls) rather than
    # moving it.
    if x is not None and y is not None:
        x, y = scale_to_screen(x, y)
        pag(pyautogui.moveTo, x, y)
        pos = (x, y)
    else:
        pos = tuple(pyautogui.position())
    button = resolve_button(button)
    down_type, _up_type, btn_const = _MOUSE_EVENT_TYPES[button]
    count = _next_click_count(pos, button)
    pag(_post_mouse_event, down_type, pos, btn_const, count)


def do_mousemove(x, y):
    x, y = scale_to_screen(x, y)
    pag(pyautogui.moveTo, x, y)


def do_mouseup(button):
    pos = tuple(pyautogui.position())
    button = resolve_button(button)
    _down_type, up_type, btn_const = _MOUSE_EVENT_TYPES[button]
    # Match whatever click count the most recent mousedown used — mouseup
    # needs the same click-state value to complete that click correctly.
    count = _click_state["count"] or 1
    pag(_post_mouse_event, up_type, pos, btn_const, count)


def do_doubleclick(button):
    # A dedicated action rather than relying on two fast taps of the
    # Left/Right Click buttons reliably landing within DOUBLE_CLICK_INTERVAL
    # of each other — on a real device, each tap is a separate network round
    # trip (POST or WS message), and real-world latency/jitter makes that
    # timing far less predictable than it looked in local testing. Doing
    # both clicks here, back to back with no network gap between them,
    # sidesteps the timing question entirely.
    pos = tuple(pyautogui.position())
    button = resolve_button(button)
    down_type, up_type, btn_const = _MOUSE_EVENT_TYPES[button]
    with input_lock:
        for count in (1, 2):
            pag(_post_mouse_event, down_type, pos, btn_const, count)
            pag(_post_mouse_event, up_type, pos, btn_const, count)
            _click_state.update(time=time.time(), pos=pos, button=button, count=count)
            if count == 1:
                time.sleep(0.05)


def do_scroll(dx, dy):
    if dy:
        pag(pyautogui.scroll, -dy)
    if dx:
        pag(pyautogui.hscroll, dx)


def _post_unicode_char(char):
    """Post one character directly via its Unicode value, bypassing physical
    key/modifier simulation entirely — see do_text() for why this replaced
    pyautogui.typewrite() for actual text content. The keycode argument (0)
    is irrelevant/unused once CGEventKeyboardSetUnicodeString overrides what
    character the event actually carries; it still needs *some* valid
    keycode to construct the event object at all.
    """
    down = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
    Quartz.CGEventKeyboardSetUnicodeString(down, len(char), char)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
    Quartz.CGEventKeyboardSetUnicodeString(up, len(char), char)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def do_text(text):
    if not text:
        return
    with input_lock:
        lines = text.split("\n")
        for i, line in enumerate(lines):
            for char in line:
                # pyautogui.typewrite() was posting a real physical Shift
                # key press/release around *every individual* uppercase
                # letter or shifted symbol (!@#$%^&*()_+{}|:"<>? — see
                # pyautogui's isShiftCharacter) rather than just once across
                # a run of them — so any normal sentence (capital letters,
                # punctuation) posted a burst of Shift down/up toggles in
                # rapid succession. That's suspected to be exactly what was
                # tripping some macOS modifier-key accessibility heuristic
                # (Dictation and/or Sticky Keys both key off repeated
                # taps of a single modifier) partway through typing a
                # message. Posting each character directly by its Unicode
                # value sidesteps physical modifier simulation for text
                # content entirely — there's simply no Shift event to
                # mis-trigger anything with.
                pag(_post_unicode_char, char)
                time.sleep(0.01)
            if i < len(lines) - 1:
                # A line break is a real functional keystroke, not text
                # content, so this still goes through a physical key combo
                # rather than _post_unicode_char — a single Shift toggle per
                # line break isn't the rapid-repeat pattern above; plain
                # Enter here would submit in most chat-style apps (Slack,
                # Discord, Messages, ...) instead of inserting a line break,
                # which Shift+Enter is the common convention for avoiding.
                pag(pyautogui.hotkey, "shift", "enter")


def do_key(combo):
    keys = [KEY_ALIASES.get(k.lower(), k.lower()) for k in combo.split("+")]
    with input_lock:
        if len(keys) > 1:
            pag(pyautogui.hotkey, *keys)
        else:
            pag(pyautogui.press, keys[0])


def dispatch_input(data):
    """Run one input event dict (as sent over /ws) against the shared handlers."""
    kind = data.get("type")
    if kind == "mousedown":
        do_mousedown(data.get("button", "left"), data.get("x"), data.get("y"))
    elif kind == "mousemove":
        x, y = data.get("x"), data.get("y")
        if x is not None and y is not None:
            do_mousemove(x, y)
    elif kind == "mouseup":
        do_mouseup(data.get("button", "left"))
    elif kind == "doubleclick":
        do_doubleclick(data.get("button", "left"))
    elif kind == "scroll":
        do_scroll(int(data.get("dx", 0)), int(data.get("dy", 0)))
    elif kind == "text":
        do_text(data.get("text", ""))
    elif kind == "key":
        combo = data.get("key", "")
        if combo:
            do_key(combo)


@app.route("/input/mousedown", methods=["POST"])
def mousedown():
    data = request.json or {}
    do_mousedown(data.get("button", "left"), data.get("x"), data.get("y"))
    return jsonify({"status": "ok"})


@app.route("/input/mousemove", methods=["POST"])
def mousemove():
    data = request.json or {}
    x, y = data.get("x"), data.get("y")
    if x is None or y is None:
        abort(400)
    do_mousemove(x, y)
    return jsonify({"status": "ok"})


@app.route("/input/mouseup", methods=["POST"])
def mouseup():
    data = request.json or {}
    do_mouseup(data.get("button", "left"))
    return jsonify({"status": "ok"})


@app.route("/input/doubleclick", methods=["POST"])
def doubleclick():
    data = request.json or {}
    do_doubleclick(data.get("button", "left"))
    return jsonify({"status": "ok"})


@app.route("/input/scroll", methods=["POST"])
def scroll():
    data = request.json or {}
    do_scroll(int(data.get("dx", 0)), int(data.get("dy", 0)))
    return jsonify({"status": "ok"})


@app.route("/input/text", methods=["POST"])
def type_text():
    data = request.json or {}
    do_text(data.get("text", ""))
    return jsonify({"status": "ok"})


@app.route("/input/key", methods=["POST"])
def key_press():
    data = request.json or {}
    combo = data.get("key", "")
    if not combo:
        abort(400)
    try:
        do_key(combo)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": "ok"})


@sock.route("/ws")
def ws_handler(ws):
    """One WebSocket connection: pushes binary frames while reading JSON
    input events off the same connection, replacing the HTTP polling /
    POST round trips with a single persistent connection.

    Streaming rate ("?rate=optimal|slow") is closed-loop, not a fixed
    interval: the sender never captures the next frame until the client
    has acked the previous one (frame_ack messages, handled below), so the
    rate self-adjusts to whatever the connection can actually sustain —
    capped only by how long a capture itself takes (~100-300ms). This also
    structurally prevents a slow connection from ever building up a
    backlog of stale-by-the-time-they-arrive frames, rather than just
    detecting that after the fact. "slow" adds a deliberate floor on top,
    for when conserving bandwidth matters more than freshness.

    Also answers {"type": "ping", "t": ...} with a pong echoing t back
    unchanged, so the client can measure round-trip latency using only its
    own clock (see the matching comment in HTML_PAGE's checkStale()) — this
    is a separate, purely diagnostic signal from frame_ack: it measures
    "is the pipe responsive right now" independent of capture cost, which
    is what the staleness overlay needs and frame_ack pacing doesn't
    directly tell you (frame_ack round trips include capture time too).

    "?resolution=" picks how large each captured frame is before it's
    hashed/encoded/sent — "full" or a fixed pixel cap (see RESOLUTION_TIERS),
    or "auto" (the default) to have frame_sender itself step between tiers
    based on how long each frame's actual round trip is taking (see
    AUTO_RESOLUTION_TIERS). Read once here at connect time for the fixed
    tiers (changing it client-side means reconnecting, same as a rate-mode
    change), but "auto" is re-evaluated by frame_sender on every frame.
    """
    rate_mode = request.args.get("rate", "optimal")
    resolution_mode = request.args.get("resolution", "auto")
    stop = threading.Event()
    ack_event = threading.Event()
    send_lock = threading.Lock()  # ws.send() isn't safe to call from two threads at once

    def frame_sender():
        last_hash = None
        auto_tier_idx = 0  # index into AUTO_RESOLUTION_TIERS; 0 = full res
        auto_fast_streak = 0
        while not stop.is_set():
            frame_start = time.time()
            sent_real_frame = False
            try:
                img = take_screenshot()
                if resolution_mode == "auto":
                    max_dim = AUTO_RESOLUTION_TIERS[auto_tier_idx]
                else:
                    max_dim = RESOLUTION_TIERS.get(resolution_mode)
                img = resize_for_stream(img, max_dim)
                h = frame_hash(img)
                if h == last_hash:
                    # Screen hasn't changed — skip encoding entirely and send
                    # a tiny marker instead of re-transferring an identical
                    # image. Still counts as this cycle's "frame" for pacing,
                    # but NOT for auto-resolution timing below: an unchanged
                    # marker is tiny regardless of resolution, so its round
                    # trip reflects latency, not the payload-size question
                    # auto-resolution is actually trying to answer.
                    with send_lock:
                        ws.send(json.dumps({"type": "unchanged"}))
                else:
                    data, _mimetype = encode_frame(img, prefer_speed=rate_mode == "optimal")
                    with send_lock:
                        ws.send(data)
                    last_hash = h
                    sent_real_frame = True
            except Exception:
                break
            ack_event.clear()
            ack_event.wait(timeout=MAX_FRAME_ACK_WAIT)

            if resolution_mode == "auto" and sent_real_frame:
                elapsed = time.time() - frame_start
                if elapsed > AUTO_STEP_DOWN_S and auto_tier_idx < len(AUTO_RESOLUTION_TIERS) - 1:
                    auto_tier_idx += 1
                    auto_fast_streak = 0
                elif elapsed < AUTO_STEP_UP_S:
                    auto_fast_streak += 1
                    if auto_fast_streak >= AUTO_STEP_UP_STREAK and auto_tier_idx > 0:
                        auto_tier_idx -= 1
                        auto_fast_streak = 0
                else:
                    auto_fast_streak = 0

            if rate_mode == "slow":
                stop.wait(WS_SLOW_INTERVAL)

    sender = threading.Thread(target=frame_sender, daemon=True)
    sender.start()
    try:
        while True:
            msg = ws.receive()
            if msg is None:
                break
            try:
                data = json.loads(msg)
                kind = data.get("type")
                if kind == "frame_ack":
                    ack_event.set()
                elif kind == "ping":
                    with send_lock:
                        ws.send(json.dumps({"type": "pong", "t": data.get("t")}))
                else:
                    # text messages used to be dispatched on their own
                    # background thread here specifically to keep do_text()'s
                    # per-character typing from blocking this loop's own
                    # ping/frame_ack handling (it was briefly showing a false
                    # "connection is slow"). That's back to inline/synchronous
                    # now — running it on a second thread meant it could
                    # genuinely race an overlapping input dispatch (e.g. an
                    # HTTP polling client's own concurrent request, which
                    # Flask's threaded=True already allows), and an
                    # interleaved keyboard-event sequence can leave a modifier
                    # key logically stuck down — this is suspected to be
                    # exactly what briefly triggered macOS's own Dictation
                    # shortcut and garbled a character send. input_lock now
                    # serializes the actual event-posting inside
                    # do_text/do_key/do_doubleclick regardless of dispatch
                    # thread, which is the correct fix for that; going back
                    # to synchronous dispatch here removes the specific
                    # thread that made the race possible in the first place,
                    # at the cost of reintroducing the original (purely
                    # cosmetic) false-staleness flash for a long message.
                    dispatch_input(data)
            except Exception:
                pass  # malformed/failed message — drop it, keep the connection alive
    finally:
        stop.set()
        ack_event.set()  # unstick the sender if it's mid-wait
        sender.join(timeout=1)


HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>APP_TITLE_PLACEHOLDER</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no, viewport-fit=cover">
    <meta name="theme-color" content="#111">
    <link rel="manifest" href="/manifest.json?token=TOKEN_PLACEHOLDER">
    <link rel="icon" type="image/png" href="/static/icon-192.png?token=TOKEN_PLACEHOLDER">
    <link rel="apple-touch-icon" href="/static/apple-touch-icon.png?token=TOKEN_PLACEHOLDER">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="APP_SHORT_TITLE_PLACEHOLDER">
    <style>
        * { box-sizing: border-box; }
        body {
            margin: 0; background: #111; color: #eee; font-family: -apple-system, sans-serif;
            display: flex; flex-direction: column; height: 100dvh;
            /* Needs to be a definite height, not min-height — flexbox only
               actually shrinks #screenCenterer below its content's natural
               size (forcing the image to shrink to fit) when the container
               has a hard height to shrink against; min-height just raises
               the floor and lets body grow past it instead, which is what
               was still leaving the image slightly too tall to fit. */
        }
        #topbar, #clickRow, #controls { flex-shrink: 0; }
        #screenCenterer {
            /* No flex-grow: this box only ever takes exactly the video's own
               natural (aspect-ratio-derived) height — it does NOT claim
               leftover vertical space itself (that would show up as empty
               letterboxing above/below the video). flex-shrink stays at its
               default (1) so it's still the thing that gives way first if
               the natural sizes of every row together don't fit in the
               viewport — same shrink-to-fit behavior as before, just without
               also hoarding surplus space. See #textRow below for where
               surplus space actually goes.

               aspect-ratio is set here too (not just on #screenWrap below) so
               this box's height is pinned from its own definite width alone,
               before the <img> has ever loaded a frame — with flex-grow: 0,
               an "auto" height would otherwise be derived from content
               (#screenWrap), which isn't reliably sized yet on first paint
               and was causing a layout shift (and a knock-on jump in
               #textRow's height, since it grows into whatever this box
               *doesn't* use) right up until the first frame arrived. */
            flex: 0 1 auto; min-height: 0; display: flex;
            align-items: center; justify-content: center; overflow: hidden; background: #000;
            aspect-ratio: SCREEN_ASPECT_PLACEHOLDER;
        }
        /* Same margin-block-end treatment as #clickRow/#textRow below, so the
           video-to-first-bar gap matches the gap between the bars
           themselves — applies in fullscreen too now, since the button rows
           stay visible there as well. */
        #screenCenterer { margin-block-end: 8px; }
        #topbar {
            position: relative; display: flex; align-items: center; gap: 8px; padding: 8px; background: #111; flex-wrap: wrap;
            /* Requires viewport-fit=cover on the viewport meta tag, or this
               always reads 0. Scoped to the actual top element (not body —
               that caused clipping, likely a viewport-height/scroll
               interaction on Android) so it only ever adds space, never
               changes how the page's overall height is computed. */
            padding-top: max(8px, env(safe-area-inset-top, 8px));
            /* Full width up to a real device's width (Pixel Fold unfolded,
               851px — the widest we have actual measurements for); wider
               than that (tablets, desktop) and it caps out and centers
               instead of stretching edge to edge. width:100% is needed
               explicitly — as a column-direction flex item, this doesn't
               stretch to fill on its own once margin:auto is involved. */
            width: 100%; max-width: 851px; margin-left: auto; margin-right: auto;
        }
        #topbar > button { flex: 1 1 auto; }
        #topbar span#status { font-size: 12px; padding: 2px 8px; border-radius: 10px; background: #444; }
        #topbar span#status.ok { background: #1e7e34; }
        #topbar span#status.bad { background: #a11; }
        #settingsPanel {
            display: none; position: absolute; top: 100%; right: 8px; margin-top: 4px;
            grid-template-columns: auto 1fr; align-items: center; column-gap: 12px; row-gap: 10px;
            padding: 12px; min-width: 220px;
            background: #181818; border: 1px solid #333; border-radius: 8px;
            box-shadow: 0 6px 16px rgba(0,0,0,0.5); z-index: 30;
        }
        #settingsPanel.open { display: grid; }
        /* display:contents takes the <label> itself out of layout so its
           text and <select> become grid items directly on #settingsPanel's
           own grid — that's what actually lines every row's label up in one
           column and every select up in another, rather than each label
           independently justify-content:space-between-ing its own two
           children with no relationship to the other rows' widths. */
        #settingsPanel label { display: contents; font-size: 13px; color: #ccc; }
        #settingsPanel #logoutBtn { grid-column: 1 / -1; }
        #screenWrap {
            position: relative; touch-action: none; overflow: hidden;
            /* This is the ONE element that shrinks to fit available space —
               #zoomLayer and #screen below just fill it at 100%/100%, so
               they always stay exactly the same size as #screenWrap. That
               matters: the pointer/pinch-zoom math (toMacCoords,
               updateCursorDot, startPinch/updatePinch) reads screenWrap's
               and the image's rects interchangeably, assuming they're
               identical — if this box instead centered a smaller
               letterboxed image inside a larger fixed-size wrap, all of
               that math would need a separate "where's the actual image
               within the wrap" offset. Keeping wrap == image avoids that
               entirely. */
            max-width: 100%; max-height: 100%; width: auto; height: auto;
            aspect-ratio: SCREEN_ASPECT_PLACEHOLDER; background: #000;
        }
        #zoomLayer { width: 100%; height: 100%; transform-origin: 0 0; will-change: transform; }
        #screen {
            width: 100%; height: 100%; display: block; touch-action: none; user-select: none; -webkit-user-select: none;
            /* iOS Safari's equivalent of Android's long-press "Save image"
               sheet — the contextmenu preventDefault below covers Android;
               this covers the same case on iOS, which doesn't fire
               contextmenu for it. */
            -webkit-touch-callout: none; -webkit-user-drag: none;
        }
        #cursorDot {
            position: absolute; top: 0; left: 0; width: 24px; height: 34px;
            pointer-events: none; opacity: 0; z-index: 10;
            filter: drop-shadow(0 1px 2px rgba(0,0,0,0.8));
        }
        #cursorDot path { fill: #fff; stroke: #000; stroke-width: 1.2; stroke-linejoin: round; }
        #cursorDot.right path { fill: #6cb2ff; }
        #cursorDot.down { transform: scale(0.9); }
        #staleOverlay {
            position: absolute; inset: 0; background: rgba(0,0,0,0.55);
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            gap: 6px; color: #fff; text-align: center; padding: 20px; z-index: 15;
            opacity: 0; pointer-events: none; transition: opacity 0.2s;
        }
        #staleOverlay.active { opacity: 1; pointer-events: auto; }
        #staleOverlay b { font-size: 15px; }
        #staleOverlay span { font-size: 13px; color: #ccc; }
        #bottomBar {
            /* No block padding — see #clickRow below for why. Its own
               bottom safe-area inset becomes margin instead of padding for
               the same reason (and margins don't collapse between flex
               siblings, so this is exactly equivalent to the old
               padding-bottom). */
            display: flex; align-items: stretch; gap: 6px; padding-inline: 8px; background: #111;
            margin-block-end: max(8px, env(safe-area-inset-bottom, 8px));
            /* Same width capping as #topbar — full width up to a real
               device's width, centered beyond that. Its buttons grow to
               fill it, same as the top bar's (below). */
            width: 100%; max-width: 851px; margin-left: auto; margin-right: auto;
        }
        #controls {
            display: flex; flex-wrap: wrap; gap: 6px; flex: 1; min-width: 0;
        }
        #controls > button { flex: 1 1 auto; }
        @media (min-width: 600px) {
            /* Wide viewports (tablets, unfolded foldables, desktop) get the
               whole row on one line instead of wrapping; overflow-x is a
               safety net in case it still doesn't quite fit. */
            #controls { flex-wrap: nowrap; overflow-x: auto; }
        }
        #scrollWheel {
            /* A vertical drag strip for touch scrolling, sitting right of
               #controls — mirrors what a real mouse wheel already does via
               the wheel listener on #screenWrap below. */
            flex: 0 0 40px; display: flex; flex-direction: column; align-items: center;
            justify-content: space-between; background: #333; color: #888;
            border: 1px solid #555; border-radius: 6px; padding: 8px 0;
            touch-action: none; user-select: none; -webkit-user-select: none; cursor: ns-resize;
        }
        #scrollWheel.active { background: #2a63c9; border-color: #2a63c9; color: #fff; }
        button, select { background: #333; color: #eee; border: 1px solid #555; border-radius: 6px; padding: 8px 12px; font-size: 14px; }
        button { user-select: none; -webkit-user-select: none; }
        button:active { background: #555; }
        button.active { background: #2a63c9; border-color: #2a63c9; }
        /* Marks buttons that support the long-press-for-Ctrl gesture (see
           the pointerdown/pointerup handling below) — a small corner dot
           rather than anything that'd change the button's own size, matching
           the same [data-key^="cmd+"] selection the JS uses so any future
           Cmd+ button picks this up automatically too. */
        #controls button[data-key^="cmd+"] { position: relative; }
        #controls button[data-key^="cmd+"]::after {
            content: ''; position: absolute; top: 4px; right: 4px;
            width: 5px; height: 5px; border-radius: 50%; background: #6cb2ff;
        }
        #arrowGroup {
            display: inline-grid;
            grid-template-areas: "pgup up pgdn" "left down right";
            grid-template-columns: repeat(3, 1fr);
            gap: 4px;
            /* Grows like the surrounding buttons, but doesn't shrink below
               its natural cluster size — unlike a plain text button, its
               grid track sizing doesn't have an obvious safe minimum. */
            flex: 1 0 auto;
        }
        #arrowGroup button { padding: 8px; min-width: 40px; }
        #arrowGroup button.small { font-size: 11px; padding: 8px 2px; }
        .arrow-pgup { grid-area: pgup; }
        .arrow-up { grid-area: up; }
        .arrow-pgdn { grid-area: pgdn; }
        .arrow-left { grid-area: left; }
        .arrow-down { grid-area: down; }
        .arrow-right { grid-area: right; }
        #clickRow {
            /* No padding at the block start/content sides — it sits flush
               against the video above it; margin-block-end is the only
               vertical space it adds, and only after itself. Halves the old
               "padding-bottom of this row + padding-top of the next" gap
               down to a single 8px, and keeps the amount of space between
               rows visually the same as the gap between buttons within a
               row. */
            display: flex; gap: 8px; padding-inline: 8px; margin-block-end: 8px; background: #111;
        }
        #clickRow button {
            flex: 1; padding: 16px; font-size: 15px; font-weight: 600;
            touch-action: none; user-select: none; -webkit-user-select: none;
        }
        #clickRow button.held { background: #2a63c9; border-color: #2a63c9; }
        #textRow {
            /* Same reasoning as #clickRow above — no block padding, just
               margin-block-end — plus this row's extra height goes entirely
               into #textInput/#sendText growing taller (align-items'
               default "stretch" fills whatever height this row ends up
               with). */
            display: flex; gap: 6px; padding-inline: 8px; margin-block-end: 8px; background: #111;
            /* The one row that grows: it soaks up whatever vertical space is
               left over once the video and every other (fixed-size) row have
               taken exactly what they need — see #screenCenterer above.
               flex-shrink: 0 keeps it from ever getting squeezed below its
               own natural size; on a deficit, #screenCenterer gives way
               first. max-height stops it from ballooning on narrow/tall
               viewports where the topbar and controls wrap to multiple lines
               and leave a lot of leftover space — comfortably roomy without
               turning into an oversized single-line input. */
            flex: 1 0 auto; max-height: 140px;
        }
        #textInput, #sendText { font-size: 16px; }
        /* Wraps just the textarea (not #sendText) so the clear button can be
           positioned inside its bounds, top-right — vertically centering it
           like a single-line <input type="search">'s native clear button
           would look wrong once this field grows tall (see #textRow above).
           flex:1 moves here from #textInput itself; default align-items on
           a flex container is stretch, so the single child (the textarea)
           still fills the wrapper's full height same as before. */
        #textInputWrap { position: relative; flex: 1; display: flex; }
        #textInput {
            flex: 1; padding: 12px; padding-right: 34px; border-radius: 6px; border: 1px solid #555;
            background: #222; color: #eee; font-family: inherit; resize: none;
        }
        #clearTextBtn {
            display: none; /* toggled via JS — only shown once there's text to clear, matching input type="search" */
            position: absolute; top: 6px; right: 6px; width: 22px; height: 22px;
            align-items: center; justify-content: center; padding: 0;
            border: none; border-radius: 50%; background: #444; color: #ccc; font-size: 15px; line-height: 1;
        }
        #clearTextBtn:active { background: #555; }
        #sendText { padding: 12px 20px; }
    </style>
</head>
<body>
    <div id="topbar">
        <b>APP_SHORT_TITLE_PLACEHOLDER</b>
        <span id="status">connecting...</span>
        <button id="zoomOutBtn">Zoom −</button>
        <button id="zoomInBtn">Zoom +</button>
        <button id="resetZoom">Reset Zoom</button>
        <button id="fullscreenBtn">Fullscreen</button>
        <button id="refreshBtn">Refresh</button>
        <button id="settingsBtn">⚙ Settings</button>
        <div id="settingsPanel">
            <label><span>Transport</span>
                <select id="transportMode">
                    <option value="poll">Polling</option>
                    <option value="ws" selected>WebSocket (optimal)</option>
                </select>
            </label>
            <label><span>Refresh rate</span>
                <select id="rateMode">
                    <option value="optimal" selected>Adapts to round-trip time (optimal)</option>
                    <option value="slow">Fixed interval</option>
                </select>
            </label>
            <label><span>Resolution</span>
                <select id="resolutionMode">
                    <option value="auto" selected>Adapts to connection speed (optimal)</option>
                    <option value="full">Full (native)</option>
                    <option value="2560">Up to 2560px</option>
                    <option value="1920">Up to 1920px</option>
                    <option value="1280">Up to 1280px</option>
                </select>
            </label>
            <label><span>Scroll direction</span>
                <select id="scrollDirection">
                    <option value="natural" selected>Natural</option>
                    <option value="traditional">Traditional</option>
                </select>
            </label>
            <button id="logoutBtn">Log Out</button>
        </div>
    </div>
    <div id="screenCenterer">
        <div id="screenWrap">
            <div id="zoomLayer">
                <img id="screen" src="/screenshot?token=TOKEN_PLACEHOLDER">
            </div>
            <div id="cursorDot">
                <svg width="24" height="34" viewBox="0 0 20 28">
                    <path d="M0,0 L0,20 L5,15.5 L8.5,23 L11.5,21.5 L8,14 L14,14 Z"/>
                </svg>
            </div>
            <div id="staleOverlay">
                <b>⚠ Connection is slow</b>
                <span id="staleAge"></span>
            </div>
        </div>
    </div>
    <div id="clickRow">
        <button id="leftClickBtn">Left Click</button>
        <button id="rightClickBtn">Right Click</button>
        <button id="doubleClickBtn">Double Click</button>
    </div>
    <div id="textRow">
        <div id="textInputWrap">
            <textarea id="textInput" placeholder="Type here, then Send"></textarea>
            <button id="clearTextBtn" type="button" aria-label="Clear text">&times;</button>
        </div>
        <button id="sendText">Send</button>
    </div>
    <div id="bottomBar">
        <div id="controls">
            <button data-key="enter">Enter</button>
            <button data-key="backspace">Backspace</button>
            <button data-key="tab">Tab</button>
            <button data-key="escape">Esc</button>
            <div id="arrowGroup">
                <button class="arrow-pgup small" data-key="pageup">PgUp</button>
                <button class="arrow-up" data-key="up">&uarr;</button>
                <button class="arrow-pgdn small" data-key="pagedown">PgDn</button>
                <button class="arrow-left" data-key="left">&larr;</button>
                <button class="arrow-down" data-key="down">&darr;</button>
                <button class="arrow-right" data-key="right">&rarr;</button>
            </div>
            <button data-key="cmd+c">Cmd+C</button>
            <button data-key="cmd+v">Cmd+V</button>
            <button data-key="cmd+z">Cmd+Z</button>
            <button data-key="cmd+tab">Cmd+Tab</button>
        </div>
        <div id="scrollWheel" aria-label="Scroll">
            <span aria-hidden="true">&uarr;</span>
            <span aria-hidden="true">&darr;</span>
        </div>
    </div>

    <script>
        const TOKEN = "TOKEN_PLACEHOLDER";
        // Reaching this page at all means the server just accepted this
        // token (the auth check runs before index() ever renders) — persist
        // it so a future bare visit to "/" (no ?token= in the URL) can be
        // auto-redirected by the login shell instead of asking again.
        localStorage.setItem('kvmToken', TOKEN);

        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('/sw.js?token=' + TOKEN).then((reg) => {
                // The browser's own update checks are otherwise infrequent
                // (roughly once per 24h, and only triggered by a fresh
                // navigation) — far too slow for a tool that's actively
                // being iterated on, especially for an installed PWA that
                // might sit open/backgrounded for days without a real
                // navigation ever happening. Force a check now, and again
                // whenever the app is reopened, so a server-side fix
                // actually reaches an already-installed PWA promptly
                // instead of silently waiting on the browser's own timer.
                reg.update().catch(() => {});
                document.addEventListener('visibilitychange', () => {
                    if (document.visibilityState === 'visible') reg.update().catch(() => {});
                });
            }).catch(() => {});
            // controllerchange fires both when a SW first claims this page
            // (right after install — not an update, don't reload) and when a
            // *new* SW version takes over from a previous one (a genuine
            // update). Only reload for the latter: skip the first occurrence.
            let controllerSeen = !!navigator.serviceWorker.controller;
            navigator.serviceWorker.addEventListener('controllerchange', () => {
                if (controllerSeen) {
                    location.reload();
                } else {
                    controllerSeen = true;
                }
            });
        }

        const img = document.getElementById('screen');
        const zoomLayer = document.getElementById('zoomLayer');
        const screenWrap = document.getElementById('screenWrap');
        const cursorDot = document.getElementById('cursorDot');
        const statusEl = document.getElementById('status');
        const staleOverlay = document.getElementById('staleOverlay');
        const staleAgeEl = document.getElementById('staleAge');
        // Left/Right Click buttons hold the actual mouse button down for as
        // long as they're pressed; moving a finger on the screen only ever
        // moves the pointer, never clicks. This is the whole fix for
        // accidental selection: there's no movement-distance heuristic
        // guessing "was that a tap or a drag" anymore, because movement
        // alone never triggers a click in the first place.
        let leftHeld = false;
        let rightHeld = false;

        // Frame staleness has two distinct failure modes, and arrival time
        // alone only catches one of them:
        //   1. Nothing has arrived in a while (dead connection, backgrounded
        //      tab) — caught by lastFrameTime below.
        //   2. Frames/pongs ARE still arriving on schedule, but each one took
        //      a long time in transit (a backed-up/bufferbloated pipe) — a
        //      frame that lands "now" may have been captured long ago. This
        //      needs round-trip time, not arrival time, to detect.
        // RTT could be measured by having the server stamp each frame with
        // its capture time, but that requires the client and server clocks
        // to agree, which we can't assume (especially once this travels over
        // the internet). Instead: measure RTT entirely with the client's own
        // clock via a small ping/pong exchange over the same transport (see
        // /ws's ping handling server-side) — zero clock-sync needed. Polling
        // gets this for free from the natural request/response timing of
        // each screenshot fetch.
        // Thresholds sit well above either transport's normal cadence (WS
        // pushes every ~350ms, polling every 1.5s, pings every 1s) so normal
        // jitter never triggers this, only a genuine stall or backlog.
        const STALE_GAP_MS = 3000;
        const STALE_RTT_MS = 1500;
        const PING_INTERVAL_MS = 1000;
        let lastFrameTime = Date.now();
        let lastRTT = 0;

        function markFreshFrame() {
            lastFrameTime = Date.now();
        }

        function markRTT(rttMs) {
            lastFrameTime = Date.now();
            lastRTT = rttMs;
        }

        function checkStale() {
            const age = Date.now() - lastFrameTime;
            const isStale = age > STALE_GAP_MS || lastRTT > STALE_RTT_MS;
            staleOverlay.classList.toggle('active', isStale);
            if (isStale) {
                staleAgeEl.textContent = age > STALE_GAP_MS
                    ? 'Last update ' + Math.floor(age / 1000) + 's ago'
                    : 'Round-trip ' + Math.round(lastRTT) + 'ms';
            }
        }
        setInterval(checkStale, 500);

        // The overlay only becomes click-through when .active (see CSS), but
        // stop the event here too so a click landing on it while stale never
        // bubbles up to screenWrap's own pointer handlers underneath.
        staleOverlay.addEventListener('pointerdown', (e) => { e.preventDefault(); e.stopPropagation(); });

        // 'idle' -> no pointers down. 'mouse' -> single pointer moving the
        // remote cursor. 'pinch' -> two pointers zooming/panning the view.
        let gestureMode = 'idle';
        let mousePointerId = null;
        const activePointers = new Map(); // pointerId -> {x, y} in client coords

        // Raw pointermove fires far faster (60-120/sec on a touchscreen) than
        // it's useful to relay over the network — each send is a real
        // synchronous OS call on the Mac side, so an unthrottled flood queues
        // up and the remote cursor lags further behind with every event.
        // Coalesce to the latest position and send at a bounded rate instead.
        const MOUSEMOVE_INTERVAL = 60;
        let pendingMove = null;
        let moveThrottleTimer = null;

        function sendMouseMoveThrottled(x, y) {
            pendingMove = { x, y };
            if (moveThrottleTimer) return;
            moveThrottleTimer = setTimeout(() => {
                moveThrottleTimer = null;
                if (pendingMove) {
                    const { x, y } = pendingMove;
                    pendingMove = null;
                    sendInput('mousemove', { x, y });
                }
            }, MOUSEMOVE_INTERVAL);
        }

        function flushMouseMove() {
            if (moveThrottleTimer) {
                clearTimeout(moveThrottleTimer);
                moveThrottleTimer = null;
            }
            if (pendingMove) {
                const { x, y } = pendingMove;
                pendingMove = null;
                return sendInput('mousemove', { x, y });
            }
            return Promise.resolve();
        }

        let scale = 1, panX = 0, panY = 0;
        let pinchStart = null; // { distance, midpoint, scale, pan, anchor }

        function apiHeaders() {
            return { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN };
        }

        function post(path, body) {
            return fetch(path, { method: 'POST', headers: apiHeaders(), body: JSON.stringify(body || {}) });
        }

        // --- Transport: "Polling" (HTTP GET /screenshot + POST /input/*, the
        // original design) vs "WebSocket" (one persistent connection pushing
        // binary frames and carrying input as JSON messages). Both stay fully
        // implemented; a dropdown picks which one is active. sendInput() is
        // the single call site the gesture/keyboard code below goes through,
        // so it doesn't need to know which transport is live.
        let transportMode = 'ws';
        // 'optimal': self-adjusting, as fast as the connection can sustain
        // (see /ws's frame_ack-gated pacing server-side, and the polling
        // loop below). 'slow': adds a deliberate floor on top, for when
        // conserving bandwidth matters more than freshness.
        let rateMode = 'optimal';
        // 'auto': the server itself adapts the streamed resolution to how
        // long frames are actually taking to round-trip (see
        // AUTO_RESOLUTION_TIERS server-side) — orthogonal to rateMode,
        // which picks the encoder, not the pixel dimensions. Fixed values
        // match RESOLUTION_TIERS' keys server-side.
        let resolutionMode = 'auto';
        const SLOW_INTERVAL_MS = 2000; // mirrors the server's WS_SLOW_INTERVAL default
        let ws = null;
        let wsFrameUrl = null; // current blob: URL backing the <img>, for revocation
        let wsReconnectTimer = null;
        let wsReconnectDelay = 1000; // ms — doubles on each failed attempt, capped below
        const WS_RECONNECT_MAX_DELAY_MS = 15000;

        function sendInput(type, body) {
            if (transportMode === 'ws' && ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify(Object.assign({ type }, body)));
                return Promise.resolve();
            }
            return post('/input/' + type, body);
        }

        // 'natural': dx/dy forwarded as-is (content follows the drag/wheel
        // direction, matching this app's original behavior). 'traditional':
        // both flipped, for anyone who prefers old-school wheel direction on
        // the remote Mac regardless of what their own device is set to —
        // the single choke point both scroll sources (the real wheel
        // listener and the virtual scroll wheel widget) go through.
        let scrollDirection = 'natural';
        function sendScroll(dx, dy) {
            if (scrollDirection === 'traditional') { dx = -dx; dy = -dy; }
            sendInput('scroll', { dx, dy });
        }

        let wsPingTimer = null;

        // Reconnects with backoff on any drop — a dead connection previously
        // just sat there forever (ws set to null, nothing ever retried),
        // which looked like the whole app had frozen since WS mode's
        // "Refresh" is a deliberate no-op (frames normally arrive on their
        // own) and input silently falls back to HTTP with no visible sign
        // anything was wrong. Resets to the minimum delay on an actual
        // reopen (visibilitychange below), since "I just switched back to
        // this tab/app" is a strong signal it's worth trying immediately
        // rather than waiting out whatever backoff a previous silent
        // background failure had built up.
        function scheduleWSReconnect() {
            if (wsReconnectTimer || transportMode !== 'ws') return;
            wsReconnectTimer = setTimeout(() => {
                wsReconnectTimer = null;
                if (transportMode === 'ws') connectWS();
            }, wsReconnectDelay);
            wsReconnectDelay = Math.min(wsReconnectDelay * 2, WS_RECONNECT_MAX_DELAY_MS);
        }

        function connectWS() {
            if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
            const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(proto + '//' + location.host + '/ws?token=' + TOKEN + '&rate=' + rateMode + '&resolution=' + resolutionMode);
            ws.binaryType = 'blob';
            ws.onopen = () => { wsReconnectDelay = 1000; };
            function sendFrameAck() {
                if (ws && ws.readyState === WebSocket.OPEN) {
                    ws.send(JSON.stringify({ type: 'frame_ack' }));
                }
            }
            ws.onmessage = (evt) => {
                if (evt.data instanceof Blob) {
                    // Ack immediately, before any decode/render work, so the
                    // round trip the server paces on reflects transport time,
                    // not client-side processing time.
                    sendFrameAck();
                    markFreshFrame();
                    const url = URL.createObjectURL(evt.data);
                    const previous = wsFrameUrl;
                    wsFrameUrl = url;
                    img.src = url;
                    if (previous) URL.revokeObjectURL(previous);
                    return;
                }
                try {
                    const msg = JSON.parse(evt.data);
                    if (msg.type === 'pong') {
                        markRTT(performance.now() - msg.t);
                    } else if (msg.type === 'unchanged') {
                        // Screen hasn't changed since the last frame — server
                        // sent this tiny marker instead of re-transferring an
                        // identical image. Still ack it (keeps the server's
                        // send-pace moving) and mark freshness (this IS
                        // current information, just "no change").
                        sendFrameAck();
                        markFreshFrame();
                    }
                } catch (e) { /* ignore */ }
            };
            ws.onclose = () => {
                ws = null;
                if (transportMode === 'ws') scheduleWSReconnect();
            };
            wsPingTimer = setInterval(() => {
                if (ws && ws.readyState === WebSocket.OPEN) {
                    ws.send(JSON.stringify({ type: 'ping', t: performance.now() }));
                }
            }, PING_INTERVAL_MS);
        }

        function disconnectWS() {
            // Explicit disconnect (switching to polling, etc.) — unlike an
            // unexpected drop, this should never trigger a reconnect.
            if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
            if (wsPingTimer) { clearInterval(wsPingTimer); wsPingTimer = null; }
            if (ws) { ws.onclose = null; ws.close(); ws = null; }
            if (wsFrameUrl) { URL.revokeObjectURL(wsFrameUrl); wsFrameUrl = null; }
        }

        function fetchScreenshotOnce() {
            return new Promise((resolve) => {
                const sentAt = performance.now();
                const next = new Image();
                next.onload = () => {
                    img.src = next.src;
                    markRTT(performance.now() - sentAt);
                    resolve(true);
                };
                next.onerror = () => resolve(false);
                // No cache-busting timestamp here on purpose: the server
                // sends Cache-Control: no-cache + ETag, so the browser
                // always revalidates with If-None-Match rather than ever
                // using a stale cached copy outright — an unchanged screen
                // costs a small 304 instead of a full image transfer +
                // decode. A unique URL every request would defeat that
                // entirely (never match anything to revalidate against).
                next.src = '/screenshot?token=' + TOKEN + '&rate=' + rateMode + '&resolution=' + resolutionMode;
            });
        }

        // One-shot nudge (e.g. "get a frame sooner after this click") in
        // polling mode — used as fire-and-forget, independent of the
        // self-pacing loop below. In WS mode it means something different:
        // a manual force-reconnect. Frames there already arrive on their
        // own ack-paced schedule (an HTTP fetch would just be redundant),
        // so there's nothing useful this could "nudge" — but a connection
        // can go silently zombied (readyState still OPEN, but not actually
        // delivering data — network paths through NAT/tunnels can do this
        // without ever sending a close frame) without the automatic
        // onclose-based reconnect ever having a reason to fire. This is the
        // manual escape hatch for exactly that: the same disconnect+
        // reconnect sequence already used for transport/rate-mode changes.
        function refreshScreen() {
            if (transportMode === 'ws') {
                disconnectWS();
                connectWS();
                return;
            }
            fetchScreenshotOnce();
        }

        // Polling's own self-pacing loop: fetch, wait for the response, then
        // schedule the next fetch — rather than firing on a fixed timer
        // regardless of how long the last request actually took. This is
        // polling's equivalent of /ws's frame_ack gating: a slow network
        // naturally stretches the interval instead of piling up requests
        // behind each other. 'slow' mode adds a floor on top, same as WS.
        let pollLoopActive = false;

        function pollLoopStep() {
            if (!pollLoopActive || transportMode !== 'poll') return;
            fetchScreenshotOnce().finally(() => {
                if (!pollLoopActive || transportMode !== 'poll') return;
                setTimeout(pollLoopStep, rateMode === 'slow' ? SLOW_INTERVAL_MS : 0);
            });
        }

        function startPollLoop() {
            if (pollLoopActive) return;
            pollLoopActive = true;
            pollLoopStep();
        }

        function stopPollLoop() {
            pollLoopActive = false;
        }

        // Offset the reported pointer position up-left of the actual touch so
        // the finger doesn't cover the cursor. Applied identically to the
        // visual cursor and the coordinates sent to the Mac, so the arrow tip
        // always shows exactly where the click will land.
        const CURSOR_OFFSET = { x: -22, y: -30 };
        function offsetPoint(clientX, clientY) {
            return { x: clientX + CURSOR_OFFSET.x, y: clientY + CURSOR_OFFSET.y };
        }

        function toMacCoords(clientX, clientY) {
            // img.getBoundingClientRect() already reflects the current zoom/pan
            // transform, so this stays correct at any zoom level.
            const rect = img.getBoundingClientRect();
            const scaleX = img.naturalWidth / rect.width;
            const scaleY = img.naturalHeight / rect.height;
            return {
                x: Math.round((clientX - rect.left) * scaleX),
                y: Math.round((clientY - rect.top) * scaleY),
            };
        }

        function updateCursorDot(offsetClientX, offsetClientY) {
            // cursorDot's SVG arrow has its tip at local (0,0), so positioning
            // top/left directly (no centering math) puts the tip exactly here.
            const rect = screenWrap.getBoundingClientRect();
            cursorDot.style.left = (offsetClientX - rect.left) + 'px';
            cursorDot.style.top = (offsetClientY - rect.top) + 'px';
            cursorDot.style.opacity = '1';
            updateCursorHeldVisual();
        }

        function updateCursorHeldVisual() {
            cursorDot.classList.toggle('right', rightHeld);
            cursorDot.classList.toggle('down', leftHeld || rightHeld);
        }

        function applyTransform() {
            zoomLayer.style.transform = `translate(${panX}px, ${panY}px) scale(${scale})`;
        }

        function clamp(v, lo, hi) { return Math.min(Math.max(v, lo), hi); }

        function clampPan() {
            const wrapRect = screenWrap.getBoundingClientRect();
            const minX = Math.min(0, wrapRect.width * (1 - scale));
            const minY = Math.min(0, wrapRect.height * (1 - scale));
            panX = clamp(panX, minX, 0);
            panY = clamp(panY, minY, 0);
        }

        function resetZoom() {
            scale = 1; panX = 0; panY = 0;
            applyTransform();
        }

        const ZOOM_STEP = 1.25;
        function zoomBy(factor) {
            // Same anchor-preserving math as pinch-zoom (see updatePinch),
            // just anchored to the viewport center instead of a pinch midpoint.
            const wrapRect = screenWrap.getBoundingClientRect();
            const center = { x: wrapRect.width / 2, y: wrapRect.height / 2 };
            const anchor = { x: (center.x - panX) / scale, y: (center.y - panY) / scale };
            const newScale = clamp(scale * factor, 1, 6);
            scale = newScale;
            panX = center.x - anchor.x * newScale;
            panY = center.y - anchor.y * newScale;
            clampPan();
            applyTransform();
        }

        function dist(a, b) { return Math.hypot(a.x - b.x, a.y - b.y); }
        function mid(a, b) { return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }; }

        function startPinch() {
            const pts = [...activePointers.values()];
            const wrapRect = screenWrap.getBoundingClientRect();
            const midClient = mid(pts[0], pts[1]);
            const midLocal = { x: midClient.x - wrapRect.left, y: midClient.y - wrapRect.top };
            pinchStart = {
                distance: dist(pts[0], pts[1]),
                scale, pan: { x: panX, y: panY },
                anchor: { x: (midLocal.x - panX) / scale, y: (midLocal.y - panY) / scale },
            };
        }

        function updatePinch() {
            const pts = [...activePointers.values()];
            const wrapRect = screenWrap.getBoundingClientRect();
            const midClient = mid(pts[0], pts[1]);
            const midLocal = { x: midClient.x - wrapRect.left, y: midClient.y - wrapRect.top };
            const newScale = clamp(pinchStart.scale * (dist(pts[0], pts[1]) / pinchStart.distance), 1, 6);
            scale = newScale;
            panX = midLocal.x - pinchStart.anchor.x * newScale;
            panY = midLocal.y - pinchStart.anchor.y * newScale;
            clampPan();
            applyTransform();
        }

        screenWrap.addEventListener('pointerdown', (e) => {
            e.preventDefault();
            try { screenWrap.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
            activePointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

            if (activePointers.size === 1) {
                gestureMode = 'mouse';
                mousePointerId = e.pointerId;
                const off = offsetPoint(e.clientX, e.clientY);
                updateCursorDot(off.x, off.y);
                const { x, y } = toMacCoords(off.x, off.y);
                sendInput('mousemove', { x, y }); // instant on first touch, not throttled
            } else if (activePointers.size === 2) {
                gestureMode = 'pinch';
                startPinch();
            }
        });

        screenWrap.addEventListener('pointermove', (e) => {
            if (!activePointers.has(e.pointerId)) return;
            activePointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

            if (gestureMode === 'mouse' && e.pointerId === mousePointerId) {
                const off = offsetPoint(e.clientX, e.clientY);
                updateCursorDot(off.x, off.y);
                const { x, y } = toMacCoords(off.x, off.y);
                sendMouseMoveThrottled(x, y);
            } else if (gestureMode === 'pinch' && activePointers.size === 2) {
                updatePinch();
            }
        });

        function endPointer(e) {
            const wasMouse = gestureMode === 'mouse' && e.pointerId === mousePointerId;
            activePointers.delete(e.pointerId);

            if (wasMouse) flushMouseMove();
            if (activePointers.size < 2) {
                pinchStart = null;
            }
            if (activePointers.size === 0) {
                gestureMode = 'idle';
                mousePointerId = null;
            }
        }
        screenWrap.addEventListener('pointerup', endPointer);
        screenWrap.addEventListener('pointercancel', endPointer);

        screenWrap.addEventListener('wheel', (e) => {
            e.preventDefault();
            // Two-finger pan on Android Chrome fires this as a synthetic
            // compatibility event on TOP of the real pointer events already
            // driving the local pinch/pan handling above — forwarding it too
            // sent an unintended scroll to the Mac on every touch-pan, which
            // (via Chrome Remote Desktop) misread as a workspace-switch
            // swipe on the far end. activePointers is already tracking any
            // in-progress touch gesture, so a wheel event firing while it's
            // non-empty is that synthetic echo, not a real trackpad/mouse
            // wheel — only forward when no touch is active.
            if (activePointers.size > 0) return;
            sendScroll(Math.round(e.deltaX), Math.round(e.deltaY));
        }, { passive: false });

        // Virtual scroll wheel: drag distance becomes 'scroll' input, same
        // as the real wheel listener above. dy is (previous Y - current Y)
        // so dragging a finger up yields positive dy, matching the sign
        // convention of a real trackpad's "natural scrolling" deltaY —
        // consistent with the desktop wheel listener's direct e.deltaY pass-through.
        const scrollWheel = document.getElementById('scrollWheel');
        let scrollWheelPointerId = null;
        let scrollWheelLastY = 0;
        scrollWheel.addEventListener('pointerdown', (e) => {
            e.preventDefault();
            scrollWheelPointerId = e.pointerId;
            scrollWheelLastY = e.clientY;
            // A drifting finger easily strays outside this narrow 40px
            // strip mid-drag; without capture, subsequent pointermove
            // events stop being routed here at all once that happens.
            // Same defensive try/catch as the other setPointerCapture call
            // sites in this file — it can throw on some devices/browsers,
            // and letting that abort the rest of this handler would also
            // skip the active-state class and the recentering below.
            try { scrollWheel.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
            scrollWheel.classList.add('active');
            // Unlike a click/drag on the video, this widget has no on-screen
            // position of its own — pyautogui.scroll() always acts on
            // whatever's currently under the Mac's real cursor, which (if
            // you haven't tapped the video yet) could be left anywhere from
            // a previous session. Recentering it here on every drag start
            // makes the widget reliably scroll "the middle of what I'm
            // looking at" instead of silently doing nothing useful.
            const rect = img.getBoundingClientRect();
            const cx = rect.left + rect.width / 2, cy = rect.top + rect.height / 2;
            sendInput('mousemove', toMacCoords(cx, cy));
            updateCursorDot(cx, cy);
        });
        scrollWheel.addEventListener('pointermove', (e) => {
            if (e.pointerId !== scrollWheelPointerId) return;
            const dy = Math.round(scrollWheelLastY - e.clientY);
            scrollWheelLastY = e.clientY;
            if (dy !== 0) sendScroll(0, dy);
        });
        function endScrollWheel(e) {
            if (e.pointerId !== scrollWheelPointerId) return;
            scrollWheelPointerId = null;
            scrollWheel.classList.remove('active');
        }
        scrollWheel.addEventListener('pointerup', endScrollWheel);
        scrollWheel.addEventListener('pointercancel', endScrollWheel);

        // A long-press on an <img> fires 'contextmenu' (Android's touch
        // equivalent of a right-click) independently of the touch-action/
        // pointer handling above — that's what triggers the native "Save
        // image" / "Copy image" sheet. Suppressing it here is the actual
        // fix; touch-action:none alone only stops default pan/zoom gestures.
        screenWrap.addEventListener('contextmenu', (e) => e.preventDefault());

        // Left/Right Click buttons: press-and-hold sends mousedown and keeps
        // the button down on the Mac side until released, exactly like a
        // real mouse button. Combined with screenWrap only ever moving the
        // pointer, holding one of these while dragging a finger on the
        // screen is what lets you select text or drag-scroll.
        function bindClickButton(btnId, button) {
            const btn = document.getElementById(btnId);
            function setHeld(held) {
                if (button === 'left') leftHeld = held; else rightHeld = held;
                btn.classList.toggle('held', held);
                updateCursorHeldVisual();
            }
            btn.addEventListener('pointerdown', (e) => {
                e.preventDefault();
                try { btn.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
                setHeld(true);
                sendInput('mousedown', { button });
            });
            function release() {
                if (!(button === 'left' ? leftHeld : rightHeld)) return;
                setHeld(false);
                sendInput('mouseup', { button }).then(() => setTimeout(refreshScreen, 200));
            }
            btn.addEventListener('pointerup', release);
            btn.addEventListener('pointercancel', release);
        }
        bindClickButton('leftClickBtn', 'left');
        bindClickButton('rightClickBtn', 'right');
        document.getElementById('doubleClickBtn').addEventListener('click', () => {
            sendInput('doubleclick', { button: 'left' }).then(() => setTimeout(refreshScreen, 200));
        });

        document.getElementById('resetZoom').addEventListener('click', resetZoom);

        document.getElementById('settingsBtn').addEventListener('click', () => {
            document.getElementById('settingsPanel').classList.toggle('open');
        });
        document.getElementById('logoutBtn').addEventListener('click', () => {
            localStorage.removeItem('kvmToken');
            location.href = '/';
        });
        document.getElementById('zoomInBtn').addEventListener('click', () => zoomBy(ZOOM_STEP));
        document.getElementById('zoomOutBtn').addEventListener('click', () => zoomBy(1 / ZOOM_STEP));

        document.getElementById('transportMode').addEventListener('change', (e) => {
            transportMode = e.target.value;
            // Grace period so switching transports doesn't itself flash the
            // stale overlay while the new connection/poll is still spinning up.
            lastFrameTime = Date.now();
            lastRTT = 0;
            if (transportMode === 'ws') {
                stopPollLoop();
                connectWS();
            } else {
                disconnectWS();
                startPollLoop();
            }
        });

        document.getElementById('rateMode').addEventListener('change', (e) => {
            rateMode = e.target.value;
            // WS reads its rate mode once at connect time (?rate=...), so a
            // running connection needs to be reopened to pick up the change.
            // Polling's loop just reads the shared variable on its next tick.
            if (transportMode === 'ws' && ws) {
                disconnectWS();
                connectWS();
            }
        });

        document.getElementById('scrollDirection').addEventListener('change', (e) => {
            scrollDirection = e.target.value;
        });

        document.getElementById('resolutionMode').addEventListener('change', (e) => {
            resolutionMode = e.target.value;
            // Same reasoning as rateMode above — WS only reads this at
            // connect time (frame_sender re-evaluates "auto" itself every
            // frame once connected, but a *fixed* tier change still needs a
            // fresh connection to take effect); polling just reads it fresh
            // on its next request.
            if (transportMode === 'ws' && ws) {
                disconnectWS();
                connectWS();
            }
        });

        const fullscreenBtn = document.getElementById('fullscreenBtn');
        async function toggleFullscreen() {
            // Purely the browser Fullscreen API now (hides browser chrome —
            // address bar etc.) — the page's own UI (button bars) stays
            // fully visible and functional throughout, it's just given more
            // room by the browser chrome going away. The button's label is
            // driven by the fullscreenchange listener below, not set here,
            // so it only ever says "Exit" when fullscreen genuinely
            // succeeded (e.g. iOS Safari has no Fullscreen API at all, and
            // the button correctly stays a no-op there rather than lying
            // about what happened).
            try {
                if (!document.fullscreenElement && document.documentElement.requestFullscreen) {
                    await document.documentElement.requestFullscreen();
                } else if (document.fullscreenElement) {
                    await document.exitFullscreen();
                }
            } catch (e) {
                // Fullscreen API unsupported/denied (e.g. iOS Safari) — nothing else to do.
            }
        }
        fullscreenBtn.addEventListener('click', toggleFullscreen);
        // Covers exiting via Esc/swipe-back too, not just this button — the
        // label always reflects the browser's actual fullscreen state.
        document.addEventListener('fullscreenchange', () => {
            fullscreenBtn.textContent = document.fullscreenElement ? 'Exit' : 'Fullscreen';
        });

        const LONG_PRESS_MS = 500;

        document.querySelectorAll('#controls button[data-key]').forEach(btn => {
            const key = btn.dataset.key;
            const ctrlVariant = key.startsWith('cmd+') ? 'ctrl+' + key.slice(4) : null;

            function sendKey(combo) {
                sendInput('key', { key: combo }).then(() => setTimeout(refreshScreen, 150));
            }

            if (!ctrlVariant) {
                btn.addEventListener('click', () => sendKey(key));
                return;
            }

            // Long-press sends the Ctrl equivalent instead of Cmd — a quick
            // way to reach e.g. Ctrl+C in a terminal without a Settings
            // trip, since which modifier you actually want depends on
            // whatever's focused on the Mac right now, not something worth
            // a persistent setting. preventDefault on pointerdown stops the
            // browser's own synthetic click from also firing afterward and
            // sending the plain Cmd version a second time.
            const label = btn.textContent;
            const ctrlLabel = label.replace('Cmd+', 'Ctrl+');
            let longPressTimer = null;
            let longPressFired = false;

            function cancelLongPress() {
                if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
                btn.classList.remove('active');
                btn.textContent = label;
            }

            btn.addEventListener('pointerdown', (e) => {
                e.preventDefault();
                longPressFired = false;
                longPressTimer = setTimeout(() => {
                    longPressFired = true;
                    btn.classList.add('active'); // held-down-style highlight, confirms the Ctrl variant is about to fire
                    btn.textContent = ctrlLabel;
                    sendKey(ctrlVariant);
                }, LONG_PRESS_MS);
            });
            btn.addEventListener('pointerup', () => {
                const wasLongPress = longPressFired;
                cancelLongPress();
                if (!wasLongPress) sendKey(key);
            });
            btn.addEventListener('pointercancel', cancelLongPress);
            btn.addEventListener('pointerleave', cancelLongPress);
        });

        const textInput = document.getElementById('textInput');
        const clearTextBtn = document.getElementById('clearTextBtn');
        // .value = '' (programmatic, e.g. after sending or the clear button
        // itself) doesn't fire a real 'input' event the way the user
        // actually typing does — so this needs to be called explicitly
        // everywhere the value changes, not just left to the listener below.
        function updateClearBtnVisibility() {
            clearTextBtn.style.display = textInput.value ? 'flex' : 'none';
        }
        textInput.addEventListener('input', updateClearBtnVisibility);
        clearTextBtn.addEventListener('click', () => {
            textInput.value = '';
            updateClearBtnVisibility();
            textInput.focus();
        });

        document.getElementById('sendText').addEventListener('click', () => {
            if (!textInput.value) return;
            sendInput('text', { text: textInput.value }).then(() => {
                textInput.value = '';
                updateClearBtnVisibility();
                setTimeout(refreshScreen, 150);
            });
        });

        document.getElementById('refreshBtn').addEventListener('click', refreshScreen);

        function pollHealth() {
            fetch('/health?token=' + TOKEN).then(r => {
                // A non-2xx here (in practice almost always a 502/504) means
                // the request reached the reverse proxy fine, but IT
                // couldn't reach the Mac — most likely the reverse tunnel
                // (or the Mac itself) is down. Checked before parsing JSON,
                // since the proxy's own error pages are HTML, not JSON, and
                // would otherwise look identical to "totally unreachable"
                // below once JSON parsing threw.
                if (!r.ok) {
                    statusEl.textContent = 'Mac unreachable';
                    statusEl.className = 'bad';
                    return null;
                }
                return r.json();
            }).then(d => {
                if (!d) return; // non-2xx case, already handled above
                if (d.screenshot_ok) {
                    statusEl.textContent = 'connected';
                    statusEl.className = 'ok';
                } else {
                    statusEl.textContent = d.warning || d.error || 'capture blocked';
                    statusEl.className = 'bad';
                }
            }).catch(() => {
                // fetch() itself rejected — no HTTP response at all, so this
                // device can't reach the server (no internet, DNS failure,
                // etc.), as distinct from the server being reachable but
                // unable to reach the Mac (handled above). On a plain LAN
                // setup with no reverse proxy in the picture at all, this is
                // just "can't reach the Mac," which is still correct.
                statusEl.textContent = 'no connection';
                statusEl.className = 'bad';
            });
        }

        pollHealth();
        setInterval(pollHealth, 5000);
        if (transportMode === 'ws') connectWS(); else startPollLoop();

        // Reopening the app (backgrounded PWA brought back to front) or
        // regaining network are both strong, specific signals that a dead
        // connection is worth retrying right now, rather than waiting out
        // whatever backoff delay scheduleWSReconnect had already built up —
        // this is the actual fix for "the PWA doesn't reconnect on its own,
        // only killing and restarting it works".
        function tryImmediateWSReconnect() {
            if (transportMode !== 'ws') return;
            if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
            wsReconnectDelay = 1000;
            connectWS();
        }
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') tryImmediateWSReconnect();
        });
        window.addEventListener('online', tryImmediateWSReconnect);
    </script>
</body>
</html>
"""


if __name__ == "__main__":
    ip = local_ip()
    print("=" * 60)
    print("Poor Person's KVM starting (LAN-only, no external tunnel)")
    print(f"Open on a device on the SAME WiFi network:")
    print(f"  http://{ip}:5959/?token={AUTH_TOKEN}")
    print("=" * 60)
    # threaded=True matters here: without it, Werkzeug's dev server handles
    # one request at a time, so a burst of input POSTs queues up behind the
    # periodic /screenshot polls (and vice versa), adding real, growing lag.
    app.run(host="0.0.0.0", port=5959, threaded=True)
