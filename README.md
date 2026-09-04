# RemoteTouch

Remote touch control for Wayland/Qt devices over SSH. Runs a small local web server that shows a live view of the remote device's screen — click to tap, drag to swipe.

## How it works

1. You open the web UI in your browser and connect to an SSH target.
2. One SSH session is opened per target. A small Python agent is piped over it and runs for as long as the session lives — nothing is installed on the device.
3. The agent keeps a touch device open on `/dev/uinput`, grabs frames directly from Weston through its `weston_capture_v1` protocol (no PNG round trip), JPEG-encodes them with GStreamer or ffmpeg and streams them back over the same session.
4. The browser shows the frames as an MJPEG stream. Clicking or dragging sends a touch event; the agent then captures a burst of frames (default 0.15 s, 0.5 s and 1.2 s after the touch) so you see the UI react without waiting for a fixed settle time.
5. **Live** switches to continuous capture (about 2–4 fps at 1080p on a typical device). **Half** halves the frame size for less bandwidth.

Unchanged frames are never re-sent. If the SSH session drops, it is reopened on the next action.

### Fallbacks

| Missing on the device                    | Behaviour                                              |
| ---------------------------------------- | ------------------------------------------------------ |
| `weston_capture_v1` (Weston < 13)        | `weston-screenshooter` PNG is used instead (slower)    |
| `gst-launch-1.0`                         | ffmpeg is used for JPEG encoding (slower)              |
| both `gst-launch-1.0` and ffmpeg         | `weston-screenshooter` PNG is used instead             |
| `/dev/uinput` access                     | Screen view works, taps are refused with an error      |

## Requirements

**Local machine:**
- Python 3
- SSH access to the target device (key-based; the tool runs ssh in batch mode and never prompts)

**Remote device:**
- SSH access
- `/dev/uinput` accessible (root or `input` group)
- Python 3 on `PATH`
- Weston 13 or newer for fast capture, otherwise `weston-screenshooter` on `PATH`
- `gst-launch-1.0` or `ffmpeg` on `PATH` for JPEG encoding (optional, see fallbacks)

## Usage

```bash
python3 remotetouch.py <ssh-host> [port]
```

Then open http://localhost:8080 in your browser.

- `<ssh-host>` — SSH host of the remote device (optional; can also be entered in the browser)
- `[port]` — local HTTP port (default: `8080`)

If a `ssh-config` file exists next to the script, it's used as the SSH config (`-F`).

## Environment variables

| Variable       | Default          | Description                                                        |
| -------------- | ---------------- | ------------------------------------------------------------------ |
| `TARGET`       | —                | SSH host (alternative to the positional arg)                       |
| `PORT`         | `8080`           | Local HTTP port                                                    |
| `JPEG_QUALITY` | `80`             | JPEG quality (1–100) used by the device-side encoder               |
| `FRAME_WIDTH`  | `0`              | Downscale frames to this width on the device; `0` = native size    |
| `BURST`        | `0.15,0.5,1.2`   | Seconds after a touch at which frames are captured                 |
| `IMAGE_PATH`   | —                | If set, every received frame is also written to this file          |

## HTTP endpoints

All POST bodies are JSON with a `target` field. Coordinates are fractions (0–1) of the frame.

| Endpoint          | Description                                          |
| ----------------- | ---------------------------------------------------- |
| `GET /`           | Web UI                                               |
| `GET /stream`     | MJPEG stream (`?target=host`)                        |
| `GET /frame`      | Latest frame as a single image (`?target=host`)      |
| `POST /connect`   | Open the SSH session; returns frame size and mode    |
| `POST /tap`       | `{x, y}`                                             |
| `POST /drag`      | `{x1, y1, x2, y2}`                                   |
| `POST /shot`      | Capture a frame now                                  |
| `POST /live`      | `{on: true|false}`                                   |
| `POST /cfg`       | `{width, quality}`                                   |

## Stopping

Ctrl-C, or:

```bash
pkill -f remotetouch.py
```
