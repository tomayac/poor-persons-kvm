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
import hmac
import io
import json
import os
import secrets
import socket
import threading

import pyautogui
from flask import Flask, Response, abort, jsonify, request, send_file
from flask_sock import Sock

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.01

app = Flask(__name__)
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
# checks, and the /ws frame sender can all land at once. Input handling stays
# fully concurrent (that's what threaded=True is actually for); only the
# capture itself is serialized.
screenshot_lock = threading.Lock()


def take_screenshot():
    with screenshot_lock:
        return pag(pyautogui.screenshot)


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
    token = request.args.get("token") or request.headers.get("X-Auth-Token")
    if not token or not hmac.compare_digest(token, AUTH_TOKEN):
        abort(401)


@app.errorhandler(401)
def unauthorized(_e):
    return jsonify({"error": "unauthorized"}), 401


@app.route("/")
def index():
    return Response(HTML_PAGE.replace("TOKEN_PLACEHOLDER", AUTH_TOKEN), mimetype="text/html")


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
        "name": "Poor Person's KVM",
        "short_name": "KVM",
        "description": "Self-hosted remote view + control for your own Mac",
        "start_url": f"/?token={AUTH_TOKEN}",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#1a1a1a",
        "theme_color": "#1a1a1a",
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
const CACHE_NAME = 'kvm-shell-v1';
const SHELL_PATHS = ['/', '/manifest.json', '/sw.js'];

function isShellRequest(url) {
    const path = new URL(url).pathname;
    return SHELL_PATHS.includes(path) || path.startsWith('/static/');
}

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('fetch', (e) => {
    if (e.request.method === 'GET' && isShellRequest(e.request.url)) {
        e.respondWith(
            fetch(e.request)
                .then((response) => {
                    const copy = response.clone();
                    // waitUntil keeps the worker alive for this write — without
                    // it the browser can terminate the worker right after the
                    // response above resolves, before the cache is ever updated.
                    e.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.put(e.request, copy)));
                    return response;
                })
                .catch(() => caches.match(e.request))
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


def capture_frame_bytes(prefer_speed=False):
    """Capture one screenshot and encode it, shared by /screenshot and /ws.

    AVIF compresses much smaller than JPEG (good for bandwidth), but on this
    machine encoding it costs ~500ms+ versus JPEG's ~15ms — measured
    directly, not assumed — which single-handedly dominates per-frame
    latency once capture (~1-2s) is added. "optimal" mode's whole point is
    speed, so it uses JPEG; "slow" mode already isn't optimizing for
    latency, so it keeps AVIF for the smaller payload.
    """
    img = take_screenshot()
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
        data, mimetype = capture_frame_bytes(prefer_speed=request.args.get("rate") == "optimal")
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return send_file(io.BytesIO(data), mimetype=mimetype)


def resolve_button(name):
    return name if name in ("left", "right", "middle") else "left"


# Shared input handlers — called from both the HTTP /input/* routes (for
# "Polling" transport) and the /ws message loop (for "WebSocket" transport),
# so the two transports can't drift in behavior.

def do_mousedown(button, x=None, y=None):
    # x/y are optional: the Left/Right Click buttons press wherever the
    # cursor already is (positioned by prior mousemove calls) rather than
    # moving it — pyautogui.mouseDown() presses at the current OS position.
    if x is not None and y is not None:
        pag(pyautogui.moveTo, x, y)
    pag(pyautogui.mouseDown, button=resolve_button(button))


def do_mousemove(x, y):
    pag(pyautogui.moveTo, x, y)


def do_mouseup(button):
    pag(pyautogui.mouseUp, button=resolve_button(button))


def do_scroll(dx, dy):
    if dy:
        pag(pyautogui.scroll, -dy)
    if dx:
        pag(pyautogui.hscroll, dx)


def do_text(text):
    if text:
        pag(pyautogui.typewrite, text, interval=0.01)


def do_key(combo):
    keys = [KEY_ALIASES.get(k.lower(), k.lower()) for k in combo.split("+")]
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
    """
    rate_mode = request.args.get("rate", "optimal")
    stop = threading.Event()
    ack_event = threading.Event()
    send_lock = threading.Lock()  # ws.send() isn't safe to call from two threads at once

    def frame_sender():
        while not stop.is_set():
            try:
                data, _mimetype = capture_frame_bytes(prefer_speed=rate_mode == "optimal")
                with send_lock:
                    ws.send(data)
            except Exception:
                break
            ack_event.clear()
            ack_event.wait(timeout=MAX_FRAME_ACK_WAIT)
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
    <title>Poor Person's KVM</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <meta name="theme-color" content="#1a1a1a">
    <link rel="manifest" href="/manifest.json?token=TOKEN_PLACEHOLDER">
    <link rel="icon" type="image/png" href="/static/icon-192.png?token=TOKEN_PLACEHOLDER">
    <link rel="apple-touch-icon" href="/static/apple-touch-icon.png?token=TOKEN_PLACEHOLDER">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="KVM">
    <style>
        * { box-sizing: border-box; }
        body { margin: 0; background: #1a1a1a; color: #eee; font-family: -apple-system, sans-serif; }
        #topbar { display: flex; align-items: center; gap: 8px; padding: 8px; background: #111; flex-wrap: wrap; }
        #topbar span#status { font-size: 12px; padding: 2px 8px; border-radius: 10px; background: #444; }
        #topbar span#status.ok { background: #1e7e34; }
        #topbar span#status.bad { background: #a11; }
        #settingsPanel {
            display: none; gap: 16px; padding: 10px 8px; background: #181818;
            border-top: 1px solid #333; flex-wrap: wrap; align-items: center;
        }
        #settingsPanel.open { display: flex; }
        #settingsPanel label { display: flex; align-items: center; gap: 6px; font-size: 13px; color: #ccc; }
        #screenWrap { position: relative; touch-action: none; overflow: hidden; }
        #zoomLayer { transform-origin: 0 0; will-change: transform; }
        #screen { width: 100%; display: block; touch-action: none; user-select: none; -webkit-user-select: none; }
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
        body.compact #topbar, body.compact #textRow, body.compact #controls,
        body.compact #clickRow, body.compact #settingsPanel { display: none; }
        #controls { display: flex; flex-wrap: wrap; gap: 6px; padding: 8px; background: #111; }
        @media (min-width: 600px) {
            /* Wide viewports (tablets, unfolded foldables, desktop) get the
               whole row on one line instead of wrapping; overflow-x is a
               safety net in case it still doesn't quite fit. */
            #controls { flex-wrap: nowrap; overflow-x: auto; }
        }
        button, select { background: #333; color: #eee; border: 1px solid #555; border-radius: 6px; padding: 8px 12px; font-size: 14px; }
        button:active { background: #555; }
        button.active { background: #2a63c9; border-color: #2a63c9; }
        #arrowGroup {
            display: inline-grid;
            grid-template-areas: ".    up    ." "left down right";
            grid-template-columns: repeat(3, 1fr);
            gap: 4px;
            flex-shrink: 0;
        }
        #arrowGroup button { padding: 8px; min-width: 40px; }
        .arrow-up { grid-area: up; }
        .arrow-left { grid-area: left; }
        .arrow-down { grid-area: down; }
        .arrow-right { grid-area: right; }
        #clickRow { display: flex; gap: 8px; padding: 8px; background: #111; }
        #clickRow button {
            flex: 1; padding: 16px; font-size: 15px; font-weight: 600;
            touch-action: none; user-select: none; -webkit-user-select: none;
        }
        #clickRow button.held { background: #2a63c9; border-color: #2a63c9; }
        #textRow { display: flex; gap: 6px; padding: 8px; background: #111; }
        #textInput { flex: 1; padding: 8px; border-radius: 6px; border: 1px solid #555; background: #222; color: #eee; font-size: 14px; }
    </style>
</head>
<body>
    <div id="topbar">
        <b>Poor Person's KVM</b>
        <span id="status">connecting...</span>
        <button id="zoomOutBtn">Zoom −</button>
        <button id="zoomInBtn">Zoom +</button>
        <button id="resetZoom">Reset Zoom</button>
        <button id="fullscreenBtn">Fullscreen</button>
        <button id="settingsBtn">⚙ Settings</button>
    </div>
    <div id="settingsPanel">
        <label>Transport
            <select id="transportMode">
                <option value="poll">Polling</option>
                <option value="ws" selected>WebSocket</option>
            </select>
        </label>
        <label>Refresh rate
            <select id="rateMode">
                <option value="optimal" selected>Optimal (fastest)</option>
                <option value="slow">Slow</option>
            </select>
        </label>
    </div>
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
    <div id="clickRow">
        <button id="leftClickBtn">Left Click</button>
        <button id="rightClickBtn">Right Click</button>
    </div>
    <div id="textRow">
        <input id="textInput" type="text" placeholder="Type here, then Send">
        <button id="sendText">Send</button>
    </div>
    <div id="controls">
        <button data-key="enter">Enter</button>
        <button data-key="backspace">Backspace</button>
        <button data-key="tab">Tab</button>
        <button data-key="escape">Esc</button>
        <div id="arrowGroup">
            <button class="arrow-up" data-key="up">&uarr;</button>
            <button class="arrow-left" data-key="left">&larr;</button>
            <button class="arrow-down" data-key="down">&darr;</button>
            <button class="arrow-right" data-key="right">&rarr;</button>
        </div>
        <button data-key="cmd+c">Cmd+C</button>
        <button data-key="cmd+v">Cmd+V</button>
        <button data-key="cmd+z">Cmd+Z</button>
        <button data-key="cmd+tab">Cmd+Tab</button>
        <button id="refreshBtn">Refresh</button>
    </div>

    <script>
        const TOKEN = "TOKEN_PLACEHOLDER";

        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('/sw.js?token=' + TOKEN).catch(() => {});
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
        const SLOW_INTERVAL_MS = 2000; // mirrors the server's WS_SLOW_INTERVAL default
        let ws = null;
        let wsFrameUrl = null; // current blob: URL backing the <img>, for revocation

        function sendInput(type, body) {
            if (transportMode === 'ws' && ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify(Object.assign({ type }, body)));
                return Promise.resolve();
            }
            return post('/input/' + type, body);
        }

        let wsPingTimer = null;

        function connectWS() {
            const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(proto + '//' + location.host + '/ws?token=' + TOKEN + '&rate=' + rateMode);
            ws.binaryType = 'blob';
            ws.onmessage = (evt) => {
                if (evt.data instanceof Blob) {
                    // Ack immediately, before any decode/render work, so the
                    // round trip the server paces on reflects transport time,
                    // not client-side processing time.
                    if (ws && ws.readyState === WebSocket.OPEN) {
                        ws.send(JSON.stringify({ type: 'frame_ack' }));
                    }
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
                    if (msg.type === 'pong') markRTT(performance.now() - msg.t);
                } catch (e) { /* ignore */ }
            };
            ws.onclose = () => { if (transportMode === 'ws') ws = null; };
            wsPingTimer = setInterval(() => {
                if (ws && ws.readyState === WebSocket.OPEN) {
                    ws.send(JSON.stringify({ type: 'ping', t: performance.now() }));
                }
            }, PING_INTERVAL_MS);
        }

        function disconnectWS() {
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
                next.src = '/screenshot?token=' + TOKEN + '&rate=' + rateMode + '&t=' + Date.now();
            });
        }

        // One-shot nudge (e.g. "get a frame sooner after this click") — used
        // as fire-and-forget, independent of the self-pacing loop below.
        function refreshScreen() {
            // No-op in WebSocket mode — frames already arrive on their own
            // ack-paced schedule, so this HTTP fetch would just be redundant.
            if (transportMode !== 'poll') return;
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
            sendInput('scroll', { dx: Math.round(e.deltaX), dy: Math.round(e.deltaY) });
        }, { passive: false });

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

        document.getElementById('resetZoom').addEventListener('click', resetZoom);
        document.getElementById('settingsBtn').addEventListener('click', () => {
            document.getElementById('settingsPanel').classList.toggle('open');
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

        const fullscreenBtn = document.getElementById('fullscreenBtn');
        async function toggleFullscreen() {
            const goingCompact = !document.body.classList.contains('compact');
            document.body.classList.toggle('compact', goingCompact);
            fullscreenBtn.textContent = goingCompact ? 'Exit' : 'Fullscreen';
            try {
                if (goingCompact && document.documentElement.requestFullscreen) {
                    await document.documentElement.requestFullscreen();
                } else if (!goingCompact && document.fullscreenElement) {
                    await document.exitFullscreen();
                }
            } catch (e) {
                // Fullscreen API unsupported/denied (e.g. iOS Safari) — the
                // compact CSS class above still reclaims the screen space.
            }
        }
        fullscreenBtn.addEventListener('click', toggleFullscreen);
        document.addEventListener('fullscreenchange', () => {
            if (!document.fullscreenElement && document.body.classList.contains('compact')) {
                document.body.classList.remove('compact');
                fullscreenBtn.textContent = 'Fullscreen';
            }
        });

        document.querySelectorAll('#controls button[data-key]').forEach(btn => {
            btn.addEventListener('click', () => {
                sendInput('key', { key: btn.dataset.key }).then(() => setTimeout(refreshScreen, 150));
            });
        });

        document.getElementById('sendText').addEventListener('click', () => {
            const input = document.getElementById('textInput');
            if (!input.value) return;
            sendInput('text', { text: input.value }).then(() => {
                input.value = '';
                setTimeout(refreshScreen, 150);
            });
        });

        document.getElementById('refreshBtn').addEventListener('click', refreshScreen);

        function pollHealth() {
            fetch('/health?token=' + TOKEN).then(r => r.json()).then(d => {
                if (d.screenshot_ok) {
                    statusEl.textContent = 'connected';
                    statusEl.className = 'ok';
                } else {
                    statusEl.textContent = d.warning || d.error || 'capture blocked';
                    statusEl.className = 'bad';
                }
            }).catch(() => {
                statusEl.textContent = 'disconnected';
                statusEl.className = 'bad';
            });
        }

        pollHealth();
        setInterval(pollHealth, 5000);
        if (transportMode === 'ws') connectWS(); else startPollLoop();
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
