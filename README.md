# RemoteTouch

Remote touch control for Wayland/Qt devices over SSH. Runs a small local web server that shows a live screenshot of the remote device's screen — click to tap, drag to swipe.

## How it works

1. You open the web UI in your browser and enter an SSH target.
2. **Fetch Screenshot** grabs the remote screen via `weston-screenshooter` and displays it.
3. Clicking or dragging on the image sends a synthetic touch event to the device through `/dev/uinput`, then auto-refreshes the screenshot.

The touch-injection script is piped to the remote over SSH — no software needs to be installed on the device beyond the requirements below.

## Requirements

**Local machine:**
- Python 3
- SSH access to the target device

**Remote device:**
- SSH access
- `/dev/uinput` accessible (root or `input` group)
- `weston-screenshooter` on `PATH`
- Python 3 on `PATH`

## Usage

```bash
python3 remotetouch.py <ssh-host> [port]
```

Then open http://localhost:8080 in your browser.

- `<ssh-host>` — SSH host of the remote device (optional; can also be set in the browser)
- `[port]` — local HTTP port (default: `8080`)

If a `ssh-config` file exists next to the script, it's used as the SSH config (`-F`).

## Environment variables

| Variable      | Default              | Description                                       |
| ------------- | -------------------- | ------------------------------------------------- |
| `TARGET`      | —                    | SSH host (alternative to the positional arg)      |
| `PORT`        | `8080`               | Local HTTP port                                   |
| `IMAGE_PATH`  | `stream/latest.png`  | Local path to save screenshots                    |
| `SCREEN_W`    | `1920`               | Touch coordinate width                            |
| `SCREEN_H`    | `1080`               | Touch coordinate height                           |
| `SETTLE_TIME` | `1.0`                | Seconds to wait after a touch before screenshot   |

## Stopping

```bash
pkill -f remotetouch.py
```
