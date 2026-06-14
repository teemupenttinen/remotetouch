#!/usr/bin/env python3
"""RemoteTouch — remote touch control for Wayland/Qt devices over SSH.

Usage: python3 remotetouch.py <target> [port]

  target   SSH host of the remote device (required)
  port     local HTTP port (default: 8080, or $PORT env var)

Env vars:
  TARGET        SSH host (alternative to positional arg)
  PORT          local HTTP port
  IMAGE_PATH    local path to save screenshots (default: stream/latest.png)
  SCREEN_W/H    touch coordinate space (default: 1920 / 1080)
  SETTLE_TIME   seconds to wait after touch before screenshotting (default: 1.0)

Requirements on the remote device:
  - SSH access
  - /dev/uinput accessible (root or input group)
  - weston-screenshooter on PATH
  - Python 3 on PATH
"""

import http.server
import json
import os
import subprocess
import sys
import time
from urllib.parse import urlparse

DEFAULT_TARGET = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TARGET", "")
PORT           = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("PORT", "8080"))
IMAGE_PATH     = os.path.abspath(os.environ.get("IMAGE_PATH", "stream/latest.png"))
SCREEN_W       = int(os.environ.get("SCREEN_W", "1920"))
SCREEN_H       = int(os.environ.get("SCREEN_H", "1080"))
SETTLE_TIME    = float(os.environ.get("SETTLE_TIME", "1.0"))

# Remote touch script — piped to `python -` on the target device via SSH stdin.
# Uses /dev/uinput directly; no external packages needed.
_REMOTE_TOUCH_PY = """
import fcntl, os, struct, sys, time

SCREEN_W   = __SCREEN_W__
SCREEN_H   = __SCREEN_H__""".replace("__SCREEN_W__", str(SCREEN_W)).replace("__SCREEN_H__", str(SCREEN_H)) + """
DRAG_STEPS = 20
DRAG_DELAY = 0.01

UI_SET_EVBIT   = 0x40045564
UI_SET_KEYBIT  = 0x40045565
UI_SET_ABSBIT  = 0x40045567
UI_DEV_CREATE  = 0x5501
UI_DEV_DESTROY = 0x5502
UI_DEV_SETUP   = 0x405C5503
UI_ABS_SETUP   = 0x401C5504

EV_SYN     = 0x00
EV_KEY     = 0x01
EV_ABS     = 0x03
SYN_REPORT = 0x00
ABS_X      = 0x00
ABS_Y      = 0x01
BTN_TOUCH  = 0x14a

EVENT_FMT = "llHHi"

fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
fcntl.ioctl(fd, UI_SET_EVBIT,  EV_ABS)
fcntl.ioctl(fd, UI_SET_EVBIT,  EV_KEY)
fcntl.ioctl(fd, UI_SET_ABSBIT, ABS_X)
fcntl.ioctl(fd, UI_SET_ABSBIT, ABS_Y)
fcntl.ioctl(fd, UI_SET_KEYBIT, BTN_TOUCH)

name = b"keyboard-touch-emulator"
try:
    fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack("HHHH80sI", 0x03, 0, 0, 1, name, 0))
    fcntl.ioctl(fd, UI_ABS_SETUP, struct.pack("H2x6i", ABS_X, 0, 0, SCREEN_W, 0, 0, 0))
    fcntl.ioctl(fd, UI_ABS_SETUP, struct.pack("H2x6i", ABS_Y, 0, 0, SCREEN_H, 0, 0, 0))
except OSError:
    absmax = [0] * 64
    absmax[ABS_X] = SCREEN_W
    absmax[ABS_Y] = SCREEN_H
    os.write(fd, struct.pack("80sHHHHI", name, 0x03, 0, 0, 1, 0)
                + struct.pack("64i", *absmax)
                + b"\\x00" * (64 * 4 * 3))

fcntl.ioctl(fd, UI_DEV_CREATE)
time.sleep(0.1)

def emit(ev_type, code, value):
    t = time.time()
    os.write(fd, struct.pack(EVENT_FMT, int(t), int((t % 1) * 1e6), ev_type, code, value))

def sync():
    emit(EV_SYN, SYN_REPORT, 0)

if len(sys.argv) == 3:
    x, y = int(sys.argv[1]), int(sys.argv[2])
    emit(EV_ABS, ABS_X, x); emit(EV_ABS, ABS_Y, y)
    emit(EV_KEY, BTN_TOUCH, 1); sync()
    time.sleep(0.06)
    emit(EV_KEY, BTN_TOUCH, 0); sync()
else:
    x1, y1, x2, y2 = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
    emit(EV_ABS, ABS_X, x1); emit(EV_ABS, ABS_Y, y1)
    emit(EV_KEY, BTN_TOUCH, 1); sync()
    time.sleep(0.02)
    for i in range(1, DRAG_STEPS + 1):
        t = i / DRAG_STEPS
        emit(EV_ABS, ABS_X, int(x1 + (x2 - x1) * t))
        emit(EV_ABS, ABS_Y, int(y1 + (y2 - y1) * t))
        sync()
        time.sleep(DRAG_DELAY)
    emit(EV_KEY, BTN_TOUCH, 0); sync()

fcntl.ioctl(fd, UI_DEV_DESTROY)
os.close(fd)
"""

_SSH_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ssh-config")
_SSH_OPTS = [
    *(["-F", _SSH_CONFIG] if os.path.exists(_SSH_CONFIG) else []),
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=/tmp/ssh-touch-%r@%h:%p",
    "-o", "ControlPersist=60",
]


def run_touch(coord_args, target):
    """Send touch event then grab a screenshot. coord_args=[] for screenshot only."""
    if coord_args:
        remote_cmd = "python - " + " ".join(str(a) for a in coord_args)
        r = subprocess.run(
            ["ssh", *_SSH_OPTS, target, remote_cmd],
            input=_REMOTE_TOUCH_PY,
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return {"ok": False, "stderr": r.stderr.strip(), "exit_code": r.returncode}
        time.sleep(SETTLE_TIME)

    # Pipe shell script via stdin to avoid quoting issues entirely.
    # Auto-detect XDG_RUNTIME_DIR from the wayland socket location so it
    # works across devices that may have different paths.
    screenshot_sh = (
        "XDG_RUNTIME_DIR=$(dirname $(find /run /tmp -maxdepth 3 -name 'wayland-0' 2>/dev/null | head -1))\n"
        "export XDG_RUNTIME_DIR\n"
        "cd /tmp\n"
        "weston-screenshooter\n"
        "f=$(ls -1t wayland-screenshot*.png 2>/dev/null | head -1)\n"
        '[ -n "$f" ] && cat "$f" && rm -f "$f"\n'
    )
    r = subprocess.run(
        ["ssh", *_SSH_OPTS, target, "sh"],
        input=screenshot_sh.encode(),
        capture_output=True,
    )
    if not r.stdout:
        err = r.stderr.decode().strip()
        print(f"[screenshot] failed: {err or '(no output)'}")
        return {"ok": False, "stderr": err or "no screenshot produced", "exit_code": r.returncode}

    os.makedirs(os.path.dirname(IMAGE_PATH), exist_ok=True)
    with open(IMAGE_PATH, "wb") as f:
        f.write(r.stdout)
    return {"ok": True, "stderr": "", "exit_code": 0}


HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>RemoteTouch</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; background: #1e1e1e; color: #ccc; font-family: system-ui, sans-serif; overflow: hidden; }
body { display: flex; flex-direction: column; }
#image-area { flex: 1; position: relative; overflow: hidden; }
#screen { width: 100%; height: 100%; object-fit: contain; display: block; cursor: crosshair; user-select: none; -webkit-user-drag: none; }
#drag-canvas { position: absolute; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; display: none; }
#fetch-btn { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); padding: 8px 16px; background: #3d3d3d; color: #ccc; border: none; border-radius: 4px; font-size: 14px; cursor: pointer; }
#fetch-btn:hover { background: #454545; }
#fetch-btn:active { background: #555; }
#status-bar { height: 30px; background: #2d2d2d; border-top: 1px solid #333; display: flex; align-items: center; padding: 0 6px; gap: 6px; flex-shrink: 0; }
#status-bar label { color: #999; font-size: 12px; }
#target-input { background: #3d3d3d; color: #eee; border: 1px solid #555; border-radius: 3px; padding: 2px 6px; font-size: 12px; width: 140px; height: 22px; }
#target-input:focus { border-color: #5599ff; outline: none; }
.sep { width: 1px; height: 20px; background: #555; }
#status-text { font-size: 13px; color: #ccc; }
#refresh-btn { background: #3d3d3d; color: #ccc; border: none; border-radius: 3px; padding: 2px 8px; font-size: 12px; cursor: pointer; height: 22px; }
#refresh-btn:hover { background: #454545; }
#refresh-btn:active { background: #555; }
</style>
</head>
<body>
<div id="image-area">
  <img id="screen" draggable="false" alt="">
  <canvas id="drag-canvas"></canvas>
  <button id="fetch-btn">Fetch Screenshot</button>
</div>
<div id="status-bar">
  <label for="target-input">Target:</label>
  <input id="target-input" type="text" value="__DEFAULT_TARGET__" placeholder="ssh host">
  <div class="sep"></div>
  <button id="refresh-btn">↻ Refresh</button>
  <div class="sep"></div>
  <span id="status-text">Enter target host, then click "Fetch Screenshot"</span>
</div>
<script>
const img = document.getElementById('screen');
const canvas = document.getElementById('drag-canvas');
const ctx = canvas.getContext('2d');
const fetchBtn = document.getElementById('fetch-btn');
const statusEl = document.getElementById('status-text');
const targetInput = document.getElementById('target-input');

// --- Image ---

function loadImage() {
  img.src = '/image?' + Date.now();
}

img.onload = () => { fetchBtn.style.display = 'none'; };
img.onerror = () => { fetchBtn.style.display = ''; };

// Auto-refresh when the file changes on disk
const evtSource = new EventSource('/events');
evtSource.onmessage = () => loadImage();

// --- Coordinate mapping (object-fit: contain) ---

function mapToImage(ex, ey) {
  const iw = img.naturalWidth, ih = img.naturalHeight;
  if (!iw || !ih) return null;
  const rect = img.getBoundingClientRect();
  const vw = rect.width, vh = rect.height;
  const scale = Math.min(vw / iw, vh / ih);
  const drawW = iw * scale, drawH = ih * scale;
  const ox = (vw - drawW) / 2, oy = (vh - drawH) / 2;
  if (ex < ox || ex > ox + drawW || ey < oy || ey > oy + drawH) return null;
  return { x: Math.round((ex - ox) / scale), y: Math.round((ey - oy) / scale) };
}

// --- Drag tracking ---

let pressing = false, pressX = 0, pressY = 0, dragging = false;

function syncCanvasSize() {
  canvas.width = canvas.offsetWidth;
  canvas.height = canvas.offsetHeight;
}
window.addEventListener('resize', syncCanvasSize);
syncCanvasSize();

img.addEventListener('mousedown', e => {
  if (!img.naturalWidth) return;
  pressing = true; dragging = false;
  pressX = e.offsetX; pressY = e.offsetY;
  e.preventDefault();
});

window.addEventListener('mousemove', e => {
  if (!pressing) return;
  const rect = img.getBoundingClientRect();
  const ex = e.clientX - rect.left, ey = e.clientY - rect.top;
  const dx = ex - pressX, dy = ey - pressY;
  if (dx * dx + dy * dy > 25) {
    dragging = true;
    syncCanvasSize();
    canvas.style.display = 'block';
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = '#ff4444'; ctx.lineWidth = 3;
    ctx.beginPath(); ctx.moveTo(pressX, pressY); ctx.lineTo(ex, ey); ctx.stroke();
    ctx.fillStyle = '#ff4444';
    [[pressX, pressY], [ex, ey]].forEach(([px, py]) => {
      ctx.beginPath(); ctx.arc(px, py, 5, 0, 2 * Math.PI); ctx.fill();
    });
  }
});

window.addEventListener('mouseup', e => {
  if (!pressing) return;
  pressing = false;
  canvas.style.display = 'none';
  const rect = img.getBoundingClientRect();
  const upX = e.clientX - rect.left, upY = e.clientY - rect.top;
  const target = targetInput.value;
  if (dragging) {
    dragging = false;
    const start = mapToImage(pressX, pressY), end = mapToImage(upX, upY);
    if (start && end) post('/drag', { x1: start.x, y1: start.y, x2: end.x, y2: end.y, target });
  } else {
    const pt = mapToImage(upX, upY);
    if (pt) post('/tap', { x: pt.x, y: pt.y, target });
  }
});

// --- API ---

function setStatus(msg) { statusEl.textContent = msg; }

function post(endpoint, data) {
  setStatus('Sending...');
  fetch(endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) })
    .then(r => r.json())
    .then(r => {
      if (r.ok) {
        if (endpoint === '/tap') setStatus(`Tap (${data.x}, ${data.y}) sent`);
        else setStatus(`Drag (${data.x1},${data.y1})→(${data.x2},${data.y2}) sent`);
        loadImage();
      } else {
        setStatus(`Failed (exit ${r.exit_code}): ${r.stderr || 'unknown'}`);
      }
    })
    .catch(err => setStatus('Error: ' + err.message));
}

function fetchScreenshot() {
  setStatus('Fetching screenshot...');
  fetch('/screenshot', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target: targetInput.value }) })
    .then(r => r.json())
    .then(r => {
      if (r.ok) { setStatus('Click to tap, drag to swipe'); loadImage(); }
      else setStatus(`Failed: ${r.stderr || 'unknown'}`);
    })
    .catch(err => setStatus('Error: ' + err.message));
}

fetchBtn.addEventListener('click', fetchScreenshot);
document.getElementById('refresh-btn').addEventListener('click', fetchScreenshot);
</script>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", ""):
            self._serve_html()
        elif path == "/image":
            self._serve_image()
        elif path == "/events":
            self._serve_sse()
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_json()
        target = body.get("target", DEFAULT_TARGET)
        if path == "/tap":
            result = run_touch([body["x"], body["y"]], target)
        elif path == "/drag":
            result = run_touch([body["x1"], body["y1"], body["x2"], body["y2"]], target)
        elif path == "/screenshot":
            result = run_touch([], target)
        else:
            self.send_error(404)
            return
        self._send_json(result)

    def _serve_html(self):
        data = HTML.replace("__DEFAULT_TARGET__", DEFAULT_TARGET).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_image(self):
        try:
            with open(IMAGE_PATH, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404)

    def _serve_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_mtime = None
        try:
            while True:
                try:
                    mtime = os.path.getmtime(IMAGE_PATH)
                except OSError:
                    mtime = None
                if mtime != last_mtime:
                    last_mtime = mtime
                    self.wfile.write(b"data: update\n\n")
                    self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length))

    def _send_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")


if __name__ == "__main__":
    try:
        server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        print(f"Cannot bind to port {PORT}: {e}", file=sys.stderr)
        print("Kill any previous instance with:  pkill -f remotetouch.py", file=sys.stderr)
        sys.exit(1)
    print(f"RemoteTouch  ->  http://localhost:{PORT}")
    print(f"Target:  {DEFAULT_TARGET or '(none — enter in browser)'}")
    print(f"Image:   {IMAGE_PATH}")
    if not DEFAULT_TARGET:
        print("Tip: python3 remotetouch.py <ssh-host>")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
