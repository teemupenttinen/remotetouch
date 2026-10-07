#!/usr/bin/env python3
"""RemoteTouch — remote touch control for Wayland/Qt devices over SSH.

Usage: python3 remotetouch.py [target] [port]

  target   SSH host of the remote device (or $TARGET; can also be set in the browser)
  port     local HTTP port (default: 8080, or $PORT)

Env vars:
  TARGET        SSH host (alternative to positional arg)
  PORT          local HTTP port
  JPEG_QUALITY  JPEG quality 1-100 for device-side encoding (default: 80)
  FRAME_WIDTH   downscale frames to this width on the device, 0 = native (default: 0)
  BURST         seconds after a touch at which frames are captured (default: 0.15,0.5,1.2)
  IMAGE_PATH    if set, every received frame is also written to this file

How it works:
  One SSH session per target runs a small agent (piped over at start, nothing is
  installed on the device). The agent keeps a /dev/uinput touch device open, grabs
  frames straight from Weston through the weston_capture_v1 protocol, JPEG-encodes
  them with GStreamer or ffmpeg and streams them back over the same session. The
  browser shows them as an MJPEG stream and sends taps/drags as normalized coordinates.

Requirements on the remote device:
  - SSH access, /dev/uinput accessible (root or input group), Python 3 on PATH
  - Weston >= 13 for fast capture (falls back to weston-screenshooter otherwise)
  - gst-launch-1.0 or ffmpeg for JPEG encoding (falls back to PNG via weston-screenshooter)
"""

import base64
import http.server
import json
import os
import queue
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlparse

DEFAULT_TARGET = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TARGET", "")
PORT           = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("PORT", "8080"))
JPEG_QUALITY   = int(os.environ.get("JPEG_QUALITY", "80"))
FRAME_WIDTH    = int(os.environ.get("FRAME_WIDTH", "0"))
BURST          = os.environ.get("BURST", "0.15,0.5,1.2")
IMAGE_PATH     = os.environ.get("IMAGE_PATH", "")


# ============================================================================
# Device-side agent. Sent over SSH and executed with python3 -c on the target.
#
# Protocol (over the SSH session's stdin/stdout):
#   -> tap X Y | drag X1 Y1 X2 Y2      coordinates are 0..1 fractions of the frame
#   -> shot | live 0|1 | cfg width=N quality=N | ping
#   <- ready W H MODE                  once at startup
#   <- ok MSG | err MSG                one reply per command, in order
#   <- log MSG                         diagnostics, forwarded to the local console
#   <- frame N W H jpeg|png\n<N bytes> pushed whenever a new frame is available
# ============================================================================
AGENT = r'''
import fcntl, glob, os, select, shutil, socket, struct, subprocess, sys, tempfile, threading, time

QUALITY = int(sys.argv[1]) if len(sys.argv) > 1 else 80
WIDTH   = int(sys.argv[2]) if len(sys.argv) > 2 else 0
BURST   = [float(x) for x in sys.argv[3].split(",") if x] if len(sys.argv) > 3 else [0.15, 0.5, 1.2]
OUT = sys.stdout.buffer

def say(line):
    OUT.write(line.encode() + b"\n"); OUT.flush()

def log(msg):
    say("log " + str(msg).replace("\n", " "))

def have(cmd):
    return shutil.which(cmd) is not None

def runtime_dir():
    d = os.environ.get("XDG_RUNTIME_DIR")
    if d and os.path.exists(d + "/wayland-0"):
        return d
    for pat in ("/run/*/wayland-0", "/run/*/*/wayland-0", "/run/wayland-0", "/tmp/*/wayland-0", "/tmp/wayland-0"):
        for p in glob.glob(pat):
            return os.path.dirname(p)
    return None

# ---------- Wayland wire protocol: just enough for weston_capture_v1 ----------

class Wayland:
    def __init__(self, path):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); self.s.connect(path)
        self.buf = b""; self.next_id = 2; self.handlers = {}; self.ifaces = {1: "wl_display"}

    def new_id(self, iface):
        i = self.next_id; self.next_id += 1; self.ifaces[i] = iface; return i

    def send(self, obj, opcode, payload=b"", fds=()):
        msg = struct.pack("<II", obj, ((8 + len(payload)) << 16) | opcode) + payload
        if fds:
            self.s.sendmsg([msg], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i" * len(fds), *fds))])
        else:
            self.s.sendall(msg)

    @staticmethod
    def string(t):
        b = t.encode() + b"\0"; return struct.pack("<I", len(b)) + b + b"\0" * (-len(b) % 4)

    def recv(self):
        d = self.s.recv(1 << 16)
        if not d:
            raise EOFError("compositor closed the connection")
        self.buf += d
        while len(self.buf) >= 8:
            obj, so = struct.unpack_from("<II", self.buf); size, op = so >> 16, so & 0xFFFF
            if len(self.buf) < size:
                break
            p = self.buf[8:size]; self.buf = self.buf[size:]
            if obj == 1:                      # wl_display: error / delete_id
                if op == 0:
                    oid, code, ln = struct.unpack_from("<III", p)
                    raise RuntimeError("wl_display error on %s: %s" % (self.ifaces.get(oid), p[12:12 + ln - 1].decode()))
                continue
            h = self.handlers.get(obj)
            if h:
                h(op, p)

    def sync(self):
        cb = self.new_id("wl_callback"); done = []
        self.handlers[cb] = lambda op, p: done.append(1)
        self.send(1, 0, struct.pack("<I", cb))
        while not done:
            self.recv()
        del self.handlers[cb]

    def close(self):
        self.s.close()

# DRM fourcc -> (ffmpeg/gst pixel format, wl_shm format code)
FOURCC = {0x34325241: ("bgr0", 0), 0x34325258: ("bgr0", 1),                       # AR24, XR24
          0x34324241: ("rgb0", 0x34324241), 0x34324258: ("rgb0", 0x34324258)}     # AB24, XB24

class Capture:
    """Grabs raw output frames through weston_capture_v1 into a reusable shm buffer."""
    SOURCES = (1, 2, 0)   # framebuffer, full_framebuffer, writeback

    def __init__(self, path):
        wl = self.wl = Wayland(path)
        self.globals = {}
        self.reg = wl.new_id("wl_registry"); wl.handlers[self.reg] = self._on_registry
        wl.send(1, 1, struct.pack("<I", self.reg)); wl.sync()
        for need in ("wl_shm", "wl_output", "weston_capture_v1"):
            if need not in self.globals:
                raise RuntimeError("compositor does not offer " + need)
        self.shm, self.out, self.cap = self._bind("wl_shm"), self._bind("wl_output"), self._bind("weston_capture_v1")
        wl.handlers[self.shm] = wl.handlers[self.out] = self._ignore
        wl.sync()
        self.st = None
        for src in self.SOURCES:
            sid, st = self._source(src)
            if st["size"] and st["fmt"] in FOURCC and not st["failed"]:
                self.src, self.st = sid, st
                break
            wl.send(sid, 0)
        if not self.st:
            raise RuntimeError("no usable capture source")
        self.fd = None; self.buf = None; self.nbytes = 0

    @staticmethod
    def _ignore(op, p):
        pass

    def _on_registry(self, op, p):
        if op == 0:
            name, ln = struct.unpack_from("<II", p); iface = p[8:8 + ln - 1].decode()
            ver, = struct.unpack_from("<I", p, 8 + ((ln + 3) & ~3))
            self.globals.setdefault(iface, (name, ver))

    def _bind(self, iface):
        name, ver = self.globals[iface]; nid = self.wl.new_id(iface)
        self.wl.send(self.reg, 0, struct.pack("<I", name) + Wayland.string(iface) + struct.pack("<II", 1, nid))
        return nid

    def _source(self, src):
        sid = self.wl.new_id("weston_capture_source_v1")
        st = {"fmt": None, "size": None, "done": False, "retry": False, "failed": None}
        def h(op, p):
            if op == 0:   st["fmt"], = struct.unpack_from("<I", p)
            elif op == 1: st["size"] = struct.unpack_from("<ii", p)
            elif op == 2: st["done"] = True
            elif op == 3: st["retry"] = True
            elif op == 4:
                ln, = struct.unpack_from("<I", p); st["failed"] = p[4:4 + ln - 1].decode() if ln else "failed"
        self.wl.handlers[sid] = h
        self.wl.send(self.cap, 1, struct.pack("<III", self.out, src, sid)); self.wl.sync()
        return sid, st

    @property
    def size(self):
        return tuple(self.st["size"])

    @property
    def pix(self):
        return FOURCC[self.st["fmt"]][0]

    def _make_buffer(self):
        self._drop_buffer()
        w, h = self.size; self.nbytes = w * h * 4
        if hasattr(os, "memfd_create"):
            self.fd = os.memfd_create("remotetouch", 0)
        else:
            self.fd, p = tempfile.mkstemp(dir="/dev/shm"); os.unlink(p)
        os.ftruncate(self.fd, self.nbytes)
        pool = self.wl.new_id("wl_shm_pool")
        self.wl.send(self.shm, 0, struct.pack("<Ii", pool, self.nbytes), fds=[self.fd])
        self.buf = self.wl.new_id("wl_buffer"); self.wl.handlers[self.buf] = self._ignore
        self.wl.send(pool, 0, struct.pack("<IiiiiI", self.buf, 0, w, h, w * 4, FOURCC[self.st["fmt"]][1]))
        self.wl.send(pool, 1)   # pool no longer needed, the buffer keeps its own reference

    def _drop_buffer(self):
        if self.buf:
            self.wl.send(self.buf, 0); self.buf = None
        if self.fd is not None:
            os.close(self.fd); self.fd = None

    def grab(self):
        st = self.st
        for _ in range(4):
            if not self.buf:
                self._make_buffer()
            st["done"] = st["retry"] = False; st["failed"] = None
            self.wl.send(self.src, 1, struct.pack("<I", self.buf))
            while not (st["done"] or st["retry"] or st["failed"]):
                self.wl.recv()
            if st["done"]:
                w, h = self.size
                return os.pread(self.fd, self.nbytes, 0), w, h, self.pix
            if st["failed"]:
                raise RuntimeError("capture failed: " + st["failed"])
            self._drop_buffer()   # output size/format changed; rebuild the buffer
        raise RuntimeError("capture: too many retries")

    def close(self):
        try:
            self._drop_buffer(); self.wl.close()
        except Exception:
            pass

def screenshooter(rt):
    """Fallback: weston-screenshooter PNG. Returns (png_bytes, width, height)."""
    d = tempfile.mkdtemp(prefix="remotetouch-")
    try:
        r = subprocess.run(["weston-screenshooter"], cwd=d, env=dict(os.environ, XDG_RUNTIME_DIR=rt or ""),
                           capture_output=True, timeout=30)
        files = glob.glob(d + "/*.png")
        if not files:
            raise RuntimeError("weston-screenshooter produced no file: " + r.stderr.decode(errors="replace").strip()[-200:])
        with open(files[0], "rb") as f:
            data = f.read()
        w, h = struct.unpack(">II", data[16:24])
        return data, w, h
    finally:
        shutil.rmtree(d, ignore_errors=True)

class Encoder:
    """Long-lived JPEG encoder process (GStreamer or ffmpeg): raw frames in, JPEGs out."""
    def __init__(self, size, pix, quality, width):
        self.key = (size, pix, quality, width)
        w, h = size
        ow = width if 0 < width < w else 0; oh = (ow * h // w) & ~1
        if have("gst-launch-1.0"):
            cmd = ["gst-launch-1.0", "-q", "fdsrc", "fd=0", "!", "rawvideoparse", "format=" + {"bgr0": "bgrx", "rgb0": "rgbx"}[pix],
                   "width=%d" % w, "height=%d" % h, "!", "videoconvert", "!"]
            if ow:
                cmd += ["videoscale", "!", "video/x-raw,width=%d,height=%d" % (ow, oh), "!"]
            cmd += ["jpegenc", "quality=%d" % quality, "!", "fdsink", "fd=1"]
        else:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", pix, "-s", "%dx%d" % (w, h),
                   "-i", "-", "-fps_mode", "passthrough", "-threads", "1"]
            if ow:
                cmd += ["-vf", "scale=%d:%d" % (ow, oh)]
            cmd += ["-c:v", "mjpeg", "-q:v", str(max(2, min(31, round((100 - quality) / 4)))),
                    "-f", "image2pipe", "-flush_packets", "1", "-"]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def alive(self):
        return self.proc.poll() is None

    def encode(self, raw, timeout=20):
        t = threading.Thread(target=self._feed, args=(raw,), daemon=True); t.start()
        out = bytearray(); fd = self.proc.stdout.fileno()
        while not out.endswith(b"\xff\xd9"):
            r, _, _ = select.select([fd], [], [], timeout)
            if not r:
                self.stop(); raise RuntimeError("encoder timeout")
            chunk = os.read(fd, 1 << 18)
            if not chunk:
                self.stop(); raise RuntimeError("encoder exited")
            out += chunk
        t.join()
        return bytes(out)

    def _feed(self, raw):
        try:
            self.proc.stdin.write(raw); self.proc.stdin.flush()
        except OSError:
            pass

    def stop(self):
        try:
            self.proc.kill()
        except OSError:
            pass

# ---------- Touch injection via /dev/uinput ----------

UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_ABSBIT = 0x40045564, 0x40045565, 0x40045567
UI_DEV_CREATE, UI_DEV_DESTROY, UI_DEV_SETUP, UI_ABS_SETUP = 0x5501, 0x5502, 0x405C5503, 0x401C5504
EV_SYN, EV_KEY, EV_ABS, ABS_X, ABS_Y, BTN_TOUCH = 0x00, 0x01, 0x03, 0x00, 0x01, 0x14a

class Touch:
    def __init__(self, w, h):
        fd = self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        for ev in (EV_ABS, EV_KEY):
            fcntl.ioctl(fd, UI_SET_EVBIT, ev)
        for a in (ABS_X, ABS_Y):
            fcntl.ioctl(fd, UI_SET_ABSBIT, a)
        fcntl.ioctl(fd, UI_SET_KEYBIT, BTN_TOUCH)
        name = b"keyboard-touch-emulator"
        try:
            fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack("HHHH80sI", 0x03, 0, 0, 1, name, 0))
            fcntl.ioctl(fd, UI_ABS_SETUP, struct.pack("H2x6i", ABS_X, 0, 0, w, 0, 0, 0))
            fcntl.ioctl(fd, UI_ABS_SETUP, struct.pack("H2x6i", ABS_Y, 0, 0, h, 0, 0, 0))
        except OSError:   # old uinput ABI
            absmax = [0] * 64; absmax[ABS_X] = w; absmax[ABS_Y] = h
            os.write(fd, struct.pack("80sHHHHI", name, 0x03, 0, 0, 1, 0) + struct.pack("64i", *absmax) + b"\0" * (64 * 4 * 3))
        fcntl.ioctl(fd, UI_DEV_CREATE)
        self.created = time.time()

    def _emit(self, ev_type, code, value):
        t = time.time()
        os.write(self.fd, struct.pack("llHHi", int(t), int((t % 1) * 1e6), ev_type, code, value))

    def _sync(self):
        self._emit(EV_SYN, 0, 0)

    def _settle(self):
        # Only matters right after creation: give the compositor time to pick the device up.
        dt = 0.5 - (time.time() - self.created)
        if dt > 0:
            time.sleep(dt)

    def tap(self, x, y, hold=0.06):
        self._settle()
        self._emit(EV_ABS, ABS_X, x); self._emit(EV_ABS, ABS_Y, y); self._emit(EV_KEY, BTN_TOUCH, 1); self._sync()
        time.sleep(hold)
        self._emit(EV_KEY, BTN_TOUCH, 0); self._sync()

    def drag(self, x1, y1, x2, y2, steps=20, delay=0.01):
        self._settle()
        self._emit(EV_ABS, ABS_X, x1); self._emit(EV_ABS, ABS_Y, y1); self._emit(EV_KEY, BTN_TOUCH, 1); self._sync()
        time.sleep(0.02)
        for i in range(1, steps + 1):
            t = i / steps
            self._emit(EV_ABS, ABS_X, int(x1 + (x2 - x1) * t)); self._emit(EV_ABS, ABS_Y, int(y1 + (y2 - y1) * t)); self._sync()
            time.sleep(delay)
        self._emit(EV_KEY, BTN_TOUCH, 0); self._sync()

    def close(self):
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY); os.close(self.fd)
        except OSError:
            pass

# ---------- Agent main loop ----------

class Agent:
    def __init__(self):
        self.rt = runtime_dir()
        self.cap = self.enc = self.touch = None
        self.quality, self.width = QUALITY, WIDTH
        self.pending = []; self.live = False; self.live_due = 0.0; self.last_raw = None
        self.mode = "screenshooter"
        first = None
        if not self.rt:
            log("no wayland socket found under /run or /tmp")
        elif have("gst-launch-1.0") or have("ffmpeg"):
            self._open_capture()
        else:
            log("neither gst-launch-1.0 nor ffmpeg found; using weston-screenshooter (PNG)")
        if self.cap:
            w, h = self.cap.size
            self.encoder()   # start warming up now
        else:
            w = h = 0
            try:
                first, w, h = screenshooter(self.rt)
            except Exception as e:
                log("screenshot failed: %s" % e)
        if w and h:
            try:
                self.touch = Touch(w, h)
            except Exception as e:
                log("touch injection unavailable: %s" % e)
        self.w, self.h = w, h
        say("ready %d %d %s" % (w, h, self.mode))
        if first:
            self.emit(first, w, h, "png")

    def _open_capture(self):
        try:
            self.cap = Capture(self.rt + "/wayland-0")
            self.mode = "capture+" + ("gst" if have("gst-launch-1.0") else "ffmpeg")
        except Exception as e:
            self.cap = None; self.mode = "screenshooter"
            log("weston_capture_v1 unavailable (%s); using weston-screenshooter" % e)

    def encoder(self):
        key = (self.cap.size, self.cap.pix, self.quality, self.width)
        if self.enc is None or self.enc.key != key or not self.enc.alive():
            if self.enc:
                self.enc.stop()
            self.enc = Encoder(*key)
        return self.enc

    def emit(self, data, w, h, kind):
        OUT.write(b"frame %d %d %d %s\n" % (len(data), w, h, kind.encode())); OUT.write(data); OUT.flush()

    def frame(self, force=False):
        """Capture, encode and push one frame. Returns True if a frame was sent."""
        try:
            if self.cap:
                try:
                    raw, w, h, pix = self.cap.grab()
                except (EOFError, OSError) as e:
                    log("capture connection lost (%s); reconnecting" % e)
                    self.cap.close(); self._open_capture()
                    if not self.cap:
                        return False
                    raw, w, h, pix = self.cap.grab()
                if not force and raw == self.last_raw:
                    return False
                self.last_raw = raw
                data, kind = self.encoder().encode(raw), "jpeg"
            else:
                data, w, h = screenshooter(self.rt); kind = "png"
        except Exception as e:
            log("frame failed: %s" % e)
            if self.enc:
                self.enc.stop(); self.enc = None
            return False
        self.emit(data, w, h, kind)
        return True

    def px(self, v, n):
        return int(round(min(1.0, max(0.0, float(v))) * n))

    def after_touch(self, msg):
        due = [time.time() + b for b in BURST]
        if due:
            time.sleep(max(0.0, due[0] - time.time())); self.frame()
        self.pending = due[1:]
        say("ok " + msg)

    def handle(self, line):
        parts = line.split()
        if not parts:
            return
        cmd, a = parts[0], parts[1:]
        try:
            if cmd in ("tap", "drag"):
                if not self.touch:
                    return say("err touch device unavailable (is /dev/uinput accessible?)")
                pts = [(self.px(a[i], self.w), self.px(a[i + 1], self.h)) for i in range(0, len(a), 2)]
                if cmd == "tap":
                    self.touch.tap(*pts[0]); msg = "Tap (%d, %d)" % pts[0]
                else:
                    self.touch.drag(*pts[0], *pts[1]); msg = "Drag (%d, %d) to (%d, %d)" % (pts[0] + pts[1])
                self.after_touch(msg)
            elif cmd == "shot":
                say("ok Screen updated" if self.frame(force=True) else "err screenshot failed")
            elif cmd == "live":
                self.live = a[0] == "1"; self.live_due = 0.0
                say("ok Live %s" % ("on" if self.live else "off"))
            elif cmd == "cfg":
                for kv in a:
                    k, v = kv.split("=")
                    if k == "width":
                        self.width = int(v)
                    elif k == "quality":
                        self.quality = max(1, min(100, int(v)))
                say("ok Frame settings updated")
            elif cmd == "ping":
                say("ok pong")
            else:
                say("err unknown command: " + cmd)
        except Exception as e:
            say("err %s: %s" % (cmd, e))

    def run(self):
        buf = b""
        while True:
            now = time.time()
            if self.pending:
                timeout = max(0.0, min(self.pending) - now)
            elif self.live:
                timeout = max(0.0, self.live_due - now)
            else:
                timeout = None
            r, _, _ = select.select([0], [], [], timeout)
            if r:
                chunk = os.read(0, 1 << 16)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self.handle(line.decode(errors="replace"))
                continue
            now = time.time()
            if self.pending and min(self.pending) <= now:
                self.pending = [d for d in self.pending if d > now]
                self.frame()
            elif self.live:
                sent = self.frame()
                self.live_due = time.time() + (0.02 if sent else 0.15)

    def close(self):
        for o in (self.touch, self.enc, self.cap):
            if o:
                try:
                    o.close() if o is not self.enc else o.stop()
                except Exception:
                    pass

agent = None
try:
    agent = Agent(); agent.run()
except BrokenPipeError:
    pass
except Exception as e:
    try:
        log("agent crashed: %r" % e)
    except Exception:
        pass
finally:
    if agent:
        agent.close()
'''

_SSH_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ssh-config")
_SSH = [
    "ssh", "-T",
    *(["-F", _SSH_CONFIG] if os.path.exists(_SSH_CONFIG) else []),
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=15",
    "-o", "ServerAliveInterval=10",
    "-o", "ServerAliveCountMax=3",
]


def _remote_command():
    """Shell command that runs the agent with python3 -c, leaving stdin free for commands."""
    b64 = base64.b64encode(AGENT.encode()).decode()
    py = f"import base64;exec(compile(base64.b64decode('{b64}'),'remotetouch-agent','exec'))"
    return f'P=$(command -v python3 || command -v python); exec "$P" -c "{py}" {JPEG_QUALITY} {FRAME_WIDTH} {BURST}'


class Agent:
    """One SSH session + device agent per target. Frames are kept in memory."""

    def __init__(self, target):
        self.target = target
        self.proc = None
        self.lock = threading.Lock()          # serializes commands (and reconnects)
        self.cond = threading.Condition()     # signals new frames to stream clients
        self.frame = None; self.mime = "image/jpeg"; self.seq = 0
        self.size = (0, 0); self.mode = ""
        self.ready = threading.Event(); self.ready_ok = False
        self.replies = queue.Queue(); self.error = ""

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _start(self):
        self.ready.clear(); self.ready_ok = False; self.error = ""; self.replies = queue.Queue()
        print(f"[{self.target}] connecting...")
        self.proc = subprocess.Popen([*_SSH, self.target, _remote_command()],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._stderr, args=(self.proc,), daemon=True).start()
        self.ready.wait(45)
        if not self.ready_ok:
            self.stop()
            raise RuntimeError(self.error or "no response from device")
        print(f"[{self.target}] ready: {self.size[0]}x{self.size[1]} ({self.mode})")

    def connect(self):
        with self.lock:
            if not self.alive():
                self._start()
            return self.size, self.mode

    def command(self, line, timeout=30):
        with self.lock:
            if not self.alive():
                self._start()
            while not self.replies.empty():      # drop stale replies from a dead session
                self.replies.get_nowait()
            try:
                self.proc.stdin.write((line + "\n").encode()); self.proc.stdin.flush()
            except OSError as e:
                raise RuntimeError(f"connection lost: {e}")
            try:
                ok, msg = self.replies.get(timeout=timeout)
            except queue.Empty:
                raise RuntimeError("device did not respond")
            if not ok:
                raise RuntimeError(msg)
            return msg

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _reader(self, proc):
        f = proc.stdout
        try:
            while True:
                line = f.readline()
                if not line:
                    break
                kind, _, rest = line.decode(errors="replace").rstrip("\n").partition(" ")
                if kind == "frame":
                    n, w, h, fmt = rest.split()
                    data = f.read(int(n))
                    if len(data) < int(n):
                        break
                    self._push(data, "image/" + fmt, (int(w), int(h)))
                elif kind == "ready":
                    w, h, mode = rest.split(" ", 2)
                    self.size, self.mode = (int(w), int(h)), mode
                    self.ready_ok = True; self.ready.set()
                elif kind in ("ok", "err"):
                    self.replies.put((kind == "ok", rest))
                elif kind == "log":
                    print(f"[{self.target}] {rest}"); self.error = rest
        finally:
            self.replies.put((False, "connection to device closed"))
            self.ready.set()
            print(f"[{self.target}] disconnected")

    def _stderr(self, proc):
        for line in proc.stderr:
            text = line.decode(errors="replace").strip()
            if text:
                print(f"[{self.target}] ssh: {text}"); self.error = text

    def _push(self, data, mime, size):
        with self.cond:
            self.frame, self.mime, self.size = data, mime, size
            self.seq += 1
            self.cond.notify_all()
        if IMAGE_PATH:
            tmp = IMAGE_PATH + ".tmp"
            os.makedirs(os.path.dirname(os.path.abspath(IMAGE_PATH)), exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, IMAGE_PATH)


_agents = {}
_agents_lock = threading.Lock()


def get_agent(target):
    with _agents_lock:
        if target not in _agents:
            _agents[target] = Agent(target)
        return _agents[target]


def _norm(v):
    v = float(v)
    if not 0.0 <= v <= 1.0:
        raise ValueError("coordinate out of range")
    return f"{v:.5f}"


HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>RemoteTouch</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; background: #1e1e1e; color: #ccc; font-family: system-ui, sans-serif; overflow: hidden; }
body { display: flex; flex-direction: column; }
#image-area { flex: 1; position: relative; overflow: hidden; touch-action: none; cursor: crosshair; }
#screen { width: 100%; height: 100%; object-fit: contain; display: block; user-select: none; -webkit-user-drag: none; pointer-events: none; }
#overlay { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
#connect-btn { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); padding: 8px 16px; font-size: 14px; }
#status-bar { height: 30px; background: #2d2d2d; border-top: 1px solid #333; display: flex; align-items: center; padding: 0 6px; gap: 6px; flex-shrink: 0; }
label { color: #999; font-size: 12px; }
input, select, button { background: #3d3d3d; color: #eee; border: 1px solid #555; border-radius: 3px; padding: 2px 6px; font-size: 12px; height: 22px; }
button { border: none; cursor: pointer; }
button:hover { background: #454545; }
button:active { background: #555; }
button.on { background: #2a7a4b; color: #fff; }
#target { width: 140px; }
#target:focus { border-color: #5599ff; outline: none; }
.sep { width: 1px; height: 20px; background: #555; }
#status { font-size: 13px; color: #ccc; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
</style>
</head>
<body>
<div id="image-area">
  <img id="screen" alt="">
  <canvas id="overlay"></canvas>
  <button id="connect-btn">Connect</button>
</div>
<div id="status-bar">
  <label for="target">Target:</label>
  <input id="target" type="text" placeholder="ssh host">
  <button id="connect2">Connect</button>
  <div class="sep"></div>
  <button id="refresh" title="Take a screenshot now">&#8635; Refresh</button>
  <button id="live" title="Continuous capture">&#9679; Live</button>
  <select id="width" title="Frame size"><option value="0">Full</option><option value="960">Half</option></select>
  <div class="sep"></div>
  <span id="status">Enter target host, then Connect</span>
</div>
<script>
const $ = id => document.getElementById(id);
const area = $('image-area'), img = $('screen'), canvas = $('overlay'), ctx = canvas.getContext('2d');
const targetInput = $('target'), statusEl = $('status'), liveBtn = $('live'), widthSel = $('width');
let target = '', live = false, press = null, dragging = false;

targetInput.value = new URLSearchParams(location.search).get('target') || __DEFAULT_TARGET__;

function setStatus(m) { statusEl.textContent = m; }

function syncUrl(t) {
  const u = new URL(location.href);
  if (t) u.searchParams.set('target', t); else u.searchParams.delete('target');
  history.replaceState(history.state, '', u);
}

function api(path, data) {
  return fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target, ...data }) })
    .then(r => r.json())
    .catch(e => ({ ok: false, msg: e.message }));
}

async function send(path, data) {
  if (!target) return setStatus('Not connected');
  setStatus('Sending...');
  const r = await api(path, data);
  clearMarker();
  setStatus(r.ok ? r.msg : 'Failed: ' + r.msg);
}

// --- Connection ---

async function connect() {
  target = targetInput.value.trim();
  if (!target) return setStatus('Enter a target host first');
  syncUrl(target);
  setStatus('Connecting to ' + target + '...');
  const r = await api('/connect', {});
  if (!r.ok) return setStatus('Connect failed: ' + r.msg);
  $('connect-btn').hidden = true;
  img.src = '/stream?target=' + encodeURIComponent(target) + '&t=' + Date.now();
  setStatus('Connected to ' + target + ' · ' + r.width + '×' + r.height + ' · ' + r.mode);
  if (widthSel.value !== '0') await api('/cfg', { width: +widthSel.value });
  if (live) await api('/live', { on: true });
  api('/shot', {});
}

$('connect-btn').onclick = $('connect2').onclick = connect;
targetInput.addEventListener('keydown', e => { if (e.key === 'Enter') connect(); });
$('refresh').onclick = () => send('/shot', {});
liveBtn.onclick = () => { live = !live; liveBtn.classList.toggle('on', live); send('/live', { on: live }); };
widthSel.onchange = () => send('/cfg', { width: +widthSel.value });

// --- Overlay markers ---

function syncCanvas() { canvas.width = area.clientWidth; canvas.height = area.clientHeight; }
window.addEventListener('resize', syncCanvas);
syncCanvas();

function clearMarker() { ctx.clearRect(0, 0, canvas.width, canvas.height); }
function dot(x, y, r) { ctx.beginPath(); ctx.arc(x, y, r, 0, 2 * Math.PI); ctx.fill(); }
function drawTap(x, y) { clearMarker(); ctx.fillStyle = 'rgba(255, 68, 68, 0.9)'; dot(x, y, 7); }
function drawDrag(a, b) {
  clearMarker();
  ctx.strokeStyle = ctx.fillStyle = '#ff4444'; ctx.lineWidth = 3;
  ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
  dot(a[0], a[1], 5); dot(b[0], b[1], 5);
}

// --- Coordinate mapping (object-fit: contain) -> 0..1 fractions of the frame ---

function mapToImage(ex, ey) {
  const iw = img.naturalWidth, ih = img.naturalHeight;
  if (!iw || !ih) return null;
  const vw = canvas.width, vh = canvas.height;
  const scale = Math.min(vw / iw, vh / ih);
  const dw = iw * scale, dh = ih * scale, ox = (vw - dw) / 2, oy = (vh - dh) / 2;
  if (ex < ox || ex > ox + dw || ey < oy || ey > oy + dh) return null;
  return { x: (ex - ox) / dw, y: (ey - oy) / dh };
}

// --- Pointer input: click = tap, drag = swipe ---

function pos(e) { const r = area.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }

area.addEventListener('pointerdown', e => {
  if (e.button !== 0 || !img.naturalWidth) return;
  press = pos(e); dragging = false;
  area.setPointerCapture(e.pointerId);
  e.preventDefault();
});

area.addEventListener('pointermove', e => {
  if (!press) return;
  const p = pos(e);
  if (!dragging && (p[0] - press[0]) ** 2 + (p[1] - press[1]) ** 2 < 25) return;
  dragging = true;
  drawDrag(press, p);
});

area.addEventListener('pointerup', e => {
  if (!press) return;
  const start = press, end = pos(e);
  press = null;
  if (dragging) {
    dragging = false;
    const a = mapToImage(...start), b = mapToImage(...end);
    if (a && b) send('/drag', { x1: a.x, y1: a.y, x2: b.x, y2: b.y }); else clearMarker();
  } else {
    const p = mapToImage(...end);
    if (p) { drawTap(...end); send('/tap', { x: p.x, y: p.y }); }
  }
});

area.addEventListener('pointercancel', () => { press = null; dragging = false; clearMarker(); });

if (targetInput.value) connect();
</script>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        target = parse_qs(u.query).get("target", [DEFAULT_TARGET])[0]
        if u.path in ("/", ""):
            self._serve_html()
        elif u.path == "/stream":
            self._serve_stream(target)
        elif u.path == "/frame":
            self._serve_frame(target)
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_json()
            target = (body.get("target") or DEFAULT_TARGET).strip()
            if not target:
                raise ValueError("no target host given")
            agent = get_agent(target)
            if path == "/connect":
                (w, h), mode = agent.connect()
                result = {"ok": True, "width": w, "height": h, "mode": mode}
            elif path == "/tap":
                result = {"ok": True, "msg": agent.command(f"tap {_norm(body['x'])} {_norm(body['y'])}")}
            elif path == "/drag":
                coords = " ".join(_norm(body[k]) for k in ("x1", "y1", "x2", "y2"))
                result = {"ok": True, "msg": agent.command(f"drag {coords}")}
            elif path == "/shot":
                result = {"ok": True, "msg": agent.command("shot")}
            elif path == "/live":
                result = {"ok": True, "msg": agent.command(f"live {1 if body.get('on') else 0}")}
            elif path == "/cfg":
                opts = " ".join(f"{k}={int(body[k])}" for k in ("width", "quality") if k in body)
                result = {"ok": True, "msg": agent.command(f"cfg {opts}")}
            else:
                self.send_error(404)
                return
        except Exception as e:
            result = {"ok": False, "msg": str(e)}
        self._send_json(result)

    def _serve_html(self):
        data = HTML.replace("__DEFAULT_TARGET__", json.dumps(DEFAULT_TARGET)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_frame(self, target):
        agent = get_agent(target)
        with agent.cond:
            frame, mime = agent.frame, agent.mime
        if frame is None:
            self.send_error(404, "no frame yet")
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(frame)))
        self.end_headers()
        self.wfile.write(frame)

    def _serve_stream(self, target):
        """MJPEG stream: every new frame is pushed as a multipart part. Idle clients get
        the current frame re-sent every 30 s as a keepalive."""
        agent = get_agent(target)
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        seq = -1
        try:
            while True:
                with agent.cond:
                    if agent.seq == seq:
                        agent.cond.wait(30)
                    frame, mime, seq = agent.frame, agent.mime, agent.seq
                if frame is None:
                    continue
                self.wfile.write(b"--frame\r\nContent-Type: %s\r\nContent-Length: %d\r\n\r\n" % (mime.encode(), len(frame)))
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def _send_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if "/stream" not in self.path:
            print(f"[{self.address_string()}] {fmt % args}")


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)   # logs show up promptly when piped
    try:
        server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        print(f"Cannot bind to port {PORT}: {e}", file=sys.stderr)
        print("Kill any previous instance with:  pkill -f remotetouch.py", file=sys.stderr)
        sys.exit(1)
    print(f"RemoteTouch  ->  http://localhost:{PORT}")
    print(f"Target:  {DEFAULT_TARGET or '(none — enter in browser)'}")
    if IMAGE_PATH:
        print(f"Saving frames to: {IMAGE_PATH}")
    if not DEFAULT_TARGET:
        print("Tip: python3 remotetouch.py <ssh-host>")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for a in list(_agents.values()):
            a.stop()
