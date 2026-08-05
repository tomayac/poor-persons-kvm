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
import os
import secrets
import socket

import pyautogui
from flask import Flask, Response, abort, jsonify, request, send_file

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.01

app = Flask(__name__)

# Set KVM_TOKEN in the environment to pin a fixed token across restarts
# (handy during development); otherwise a fresh random one is generated
# on every launch.
AUTH_TOKEN = os.environ.get("KVM_TOKEN") or secrets.token_urlsafe(24)

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


@app.route("/health")
def health():
    try:
        img = pag(pyautogui.screenshot)
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


@app.route("/screenshot")
def screenshot():
    try:
        img = pag(pyautogui.screenshot)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    buf = io.BytesIO()
    try:
        img.save(buf, "AVIF", quality=55)
        mimetype = "image/avif"
    except Exception:
        # Fall back to JPEG if AVIF isn't available in this Pillow build.
        # JPEG has no alpha channel, but macOS screenshots come back as RGBA
        # — drop the alpha or Pillow's JPEG encoder raises KeyError('RGBA').
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=70)
        mimetype = "image/jpeg"
    buf.seek(0)
    return send_file(buf, mimetype=mimetype)


def resolve_button(name):
    return name if name in ("left", "right", "middle") else "left"


@app.route("/input/mousedown", methods=["POST"])
def mousedown():
    data = request.json or {}
    x, y = data.get("x"), data.get("y")
    button = resolve_button(data.get("button", "left"))
    if x is None or y is None:
        abort(400)
    pag(pyautogui.moveTo, x, y)
    pag(pyautogui.mouseDown, button=button)
    return jsonify({"status": "ok"})


@app.route("/input/mousemove", methods=["POST"])
def mousemove():
    data = request.json or {}
    x, y = data.get("x"), data.get("y")
    if x is None or y is None:
        abort(400)
    pag(pyautogui.moveTo, x, y)
    return jsonify({"status": "ok"})


@app.route("/input/mouseup", methods=["POST"])
def mouseup():
    data = request.json or {}
    button = resolve_button(data.get("button", "left"))
    pag(pyautogui.mouseUp, button=button)
    return jsonify({"status": "ok"})


@app.route("/input/scroll", methods=["POST"])
def scroll():
    data = request.json or {}
    dx = int(data.get("dx", 0))
    dy = int(data.get("dy", 0))
    if dy:
        pag(pyautogui.scroll, -dy)
    if dx:
        pag(pyautogui.hscroll, dx)
    return jsonify({"status": "ok"})


@app.route("/input/text", methods=["POST"])
def type_text():
    data = request.json or {}
    text = data.get("text", "")
    if text:
        pag(pyautogui.typewrite, text, interval=0.01)
    return jsonify({"status": "ok"})


@app.route("/input/key", methods=["POST"])
def key_press():
    data = request.json or {}
    combo = data.get("key", "")
    if not combo:
        abort(400)
    keys = [KEY_ALIASES.get(k.lower(), k.lower()) for k in combo.split("+")]
    try:
        if len(keys) > 1:
            pag(pyautogui.hotkey, *keys)
        else:
            pag(pyautogui.press, keys[0])
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": "ok"})


HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Poor Person's KVM</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <style>
        * { box-sizing: border-box; }
        body { margin: 0; background: #1a1a1a; color: #eee; font-family: -apple-system, sans-serif; }
        #topbar { display: flex; align-items: center; gap: 8px; padding: 8px; background: #111; flex-wrap: wrap; }
        #topbar span#status { font-size: 12px; padding: 2px 8px; border-radius: 10px; background: #444; }
        #topbar span#status.ok { background: #1e7e34; }
        #topbar span#status.bad { background: #a11; }
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
        body.compact #topbar {
            position: fixed; top: 6px; right: 6px; left: auto; background: rgba(0,0,0,0.55);
            padding: 4px 6px; border-radius: 8px; z-index: 20;
        }
        body.compact #topbar b, body.compact #topbar #status,
        body.compact #topbar #rightClickToggle, body.compact #topbar #resetZoom { display: none; }
        body.compact #textRow, body.compact #controls { display: none; }
        #controls { display: flex; flex-wrap: wrap; gap: 6px; padding: 8px; background: #111; }
        button { background: #333; color: #eee; border: 1px solid #555; border-radius: 6px; padding: 8px 12px; font-size: 14px; }
        button:active { background: #555; }
        button.active { background: #2a63c9; border-color: #2a63c9; }
        #textRow { display: flex; gap: 6px; padding: 8px; background: #111; }
        #textInput { flex: 1; padding: 8px; border-radius: 6px; border: 1px solid #555; background: #222; color: #eee; font-size: 14px; }
    </style>
</head>
<body>
    <div id="topbar">
        <b>Poor Person's KVM</b>
        <span id="status">connecting...</span>
        <button id="rightClickToggle">Right-click mode</button>
        <button id="resetZoom">Reset Zoom</button>
        <button id="fullscreenBtn">Fullscreen</button>
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
        <button data-key="up">&uarr;</button>
        <button data-key="down">&darr;</button>
        <button data-key="left">&larr;</button>
        <button data-key="right">&rarr;</button>
        <button data-key="cmd+c">Cmd+C</button>
        <button data-key="cmd+v">Cmd+V</button>
        <button data-key="cmd+z">Cmd+Z</button>
        <button data-key="cmd+tab">Cmd+Tab</button>
        <button id="refreshBtn">Refresh</button>
    </div>

    <script>
        const TOKEN = "TOKEN_PLACEHOLDER";
        const img = document.getElementById('screen');
        const zoomLayer = document.getElementById('zoomLayer');
        const screenWrap = document.getElementById('screenWrap');
        const cursorDot = document.getElementById('cursorDot');
        const statusEl = document.getElementById('status');
        let rightClickMode = false;

        // 'idle' -> no pointers down. 'mouse' -> single pointer driving the
        // remote mouse. 'pinch' -> two pointers zooming/panning the view.
        let gestureMode = 'idle';
        let mousePointerId = null;
        const activePointers = new Map(); // pointerId -> {x, y} in client coords

        let scale = 1, panX = 0, panY = 0;
        let pinchStart = null; // { distance, midpoint, scale, pan, anchor }

        function apiHeaders() {
            return { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN };
        }

        function post(path, body) {
            return fetch(path, { method: 'POST', headers: apiHeaders(), body: JSON.stringify(body || {}) });
        }

        function refreshScreen() {
            const next = new Image();
            next.onload = () => { img.src = next.src; };
            next.src = '/screenshot?token=' + TOKEN + '&t=' + Date.now();
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
            cursorDot.classList.toggle('right', rightClickMode);
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
                const { x, y } = toMacCoords(off.x, off.y);
                updateCursorDot(off.x, off.y);
                cursorDot.classList.add('down');
                post('/input/mousedown', { x, y, button: rightClickMode ? 'right' : 'left' });
            } else if (activePointers.size === 2) {
                if (gestureMode === 'mouse') {
                    // Cancel the in-progress click/drag before switching to pinch.
                    post('/input/mouseup', { button: rightClickMode ? 'right' : 'left' });
                }
                gestureMode = 'pinch';
                startPinch();
            }
        });

        screenWrap.addEventListener('pointermove', (e) => {
            if (!activePointers.has(e.pointerId)) return;
            activePointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

            if (gestureMode === 'mouse' && e.pointerId === mousePointerId) {
                const off = offsetPoint(e.clientX, e.clientY);
                const { x, y } = toMacCoords(off.x, off.y);
                updateCursorDot(off.x, off.y);
                post('/input/mousemove', { x, y });
            } else if (gestureMode === 'pinch' && activePointers.size === 2) {
                updatePinch();
            }
        });

        function endPointer(e) {
            const wasMouse = gestureMode === 'mouse' && e.pointerId === mousePointerId;
            activePointers.delete(e.pointerId);

            if (wasMouse) {
                cursorDot.classList.remove('down');
                post('/input/mouseup', { button: rightClickMode ? 'right' : 'left' })
                    .then(() => setTimeout(refreshScreen, 200));
            }
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
            post('/input/scroll', { dx: Math.round(e.deltaX), dy: Math.round(e.deltaY) });
        }, { passive: false });

        document.getElementById('rightClickToggle').addEventListener('click', (e) => {
            rightClickMode = !rightClickMode;
            e.target.classList.toggle('active', rightClickMode);
        });

        document.getElementById('resetZoom').addEventListener('click', resetZoom);

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
                post('/input/key', { key: btn.dataset.key }).then(() => setTimeout(refreshScreen, 150));
            });
        });

        document.getElementById('sendText').addEventListener('click', () => {
            const input = document.getElementById('textInput');
            if (!input.value) return;
            post('/input/text', { text: input.value }).then(() => {
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
        setInterval(refreshScreen, 1500);
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
    app.run(host="0.0.0.0", port=5959)
