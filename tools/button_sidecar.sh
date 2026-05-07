#!/usr/bin/env bash
set -Eeuo pipefail

APP_SERVICE="morse-whisperer"
API_BASE="http://127.0.0.1:8080"
PYTHON="/opt/morse-whisperer-pi/venv/bin/python"

exec "$PYTHON" - <<'PY'
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error

try:
    import RPi.GPIO as GPIO
except Exception as exc:
    print(f"[mw-buttons] RPi.GPIO import failed: {exc}", flush=True)
    raise SystemExit(1)

API_BASE = "http://127.0.0.1:8080"
APP_SERVICE = "morse-whisperer"
ACTIVE_FILE = "/run/morse-whisperer-button.json"

# Physical TFT buttons, left to right.
# Active-low with internal pull-ups.
BUTTONS = {
    1: {"gpio": 23, "name": "PAGE"},
    2: {"gpio": 22, "name": "SCAN"},
    3: {"gpio": 27, "name": "RESET"},
    4: {"gpio": 18, "name": "CLEAR"},
}

POLL_SEC = 0.025
DEBOUNCE_SEC = 0.06
LONG_PRESS_SEC = 1.20

running = True


def log(msg: str) -> None:
    print(f"[mw-buttons] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def set_active_button(button: int, label: str, hold_sec: float = 1.6) -> None:
    """
    Publish a tiny transient state file for the TFT footer.

    The display loop reads this file and highlights the matching physical
    button. /run is volatile, so it resets cleanly at boot.
    """
    try:
        payload = {
            "button": int(button),
            "label": str(label),
            "until": time.time() + float(hold_sec),
            "ts": time.time(),
        }
        tmp = ACTIVE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, ACTIVE_FILE)
    except Exception as exc:
        log(f"active button state write failed: {exc}")


def api_post(path: str, timeout: float = 1.2) -> bool:
    url = API_BASE + path
    try:
        req = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        log(f"POST {path} OK")
        return True
    except Exception as exc:
        log(f"POST {path} failed: {exc}")
        return False


def run_cmd(cmd, timeout: float = 8.0) -> bool:
    try:
        subprocess.run(cmd, check=False, timeout=timeout)
        return True
    except Exception as exc:
        log(f"command failed {cmd}: {exc}")
        return False


def restart_decoder() -> None:
    log("restarting morse-whisperer service")
    run_cmd(["systemctl", "restart", APP_SERVICE], timeout=12.0)


def full_reset_and_restart() -> None:
    log("full reset requested")
    api_post("/api/reset", timeout=1.0)
    time.sleep(0.4)
    restart_decoder()


def manual_scan() -> None:
    # Try a few likely endpoint names. Only one needs to exist.
    # This keeps the sidecar compatible with slightly different web.py builds.
    for path in (
        "/api/tone/scan",
        "/api/scan",
        "/api/tone_scan",
        "/api/control/scan",
    ):
        if api_post(path, timeout=1.0):
            return
    log("no scan API endpoint responded; leaving decoder unchanged")


def short_press(button: int) -> None:
    if button == 1:
        log("Button 1 short: TFT next page")
        api_post("/api/tft/next")

    elif button == 2:
        log("Button 2 short: manual tone scan")
        manual_scan()

    elif button == 3:
        log("Button 3 short: reset decoder/copy")
        api_post("/api/reset")

    elif button == 4:
        log("Button 4 short: clear/reset decoder/copy")
        api_post("/api/reset")


def long_press(button: int) -> None:
    if button == 1:
        log("Button 1 hold: TFT freeze/unfreeze")
        api_post("/api/tft/freeze")

    elif button == 2:
        log("Button 2 hold: restart decoder")
        restart_decoder()

    elif button == 3:
        log("Button 3 hold: full reset + restart")
        full_reset_and_restart()

    elif button == 4:
        log("Button 4 hold: full reset + restart")
        full_reset_and_restart()


def stop(sig, frame) -> None:
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)

GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)

for num, info in BUTTONS.items():
    pin = int(info["gpio"])
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    log(f"Button {num} / GPIO{pin}: {info['name']} initial={GPIO.input(pin)}")

state = {}
for num, info in BUTTONS.items():
    pin = int(info["gpio"])
    val = GPIO.input(pin)
    state[num] = {
        "pin": pin,
        "last_raw": val,
        "stable": val,
        "changed_at": time.monotonic(),
        "pressed_at": None,
        "long_fired": False,
    }

log("four-button sidecar running")

try:
    while running:
        now = time.monotonic()

        for num, st in state.items():
            pin = st["pin"]
            raw = GPIO.input(pin)

            if raw != st["last_raw"]:
                st["last_raw"] = raw
                st["changed_at"] = now

            # Debounced stable transition.
            if raw != st["stable"] and (now - st["changed_at"]) >= DEBOUNCE_SEC:
                st["stable"] = raw

                if raw == 0:
                    st["pressed_at"] = now
                    st["long_fired"] = False
                    log(f"Button {num} GPIO{pin} pressed")
                    try:
                        set_active_button(num, BUTTONS[num]["name"], 1.8)
                    except Exception:
                        pass
                else:
                    pressed_at = st.get("pressed_at")
                    held = (now - pressed_at) if pressed_at else 0.0
                    log(f"Button {num} GPIO{pin} released after {held:.2f}s")

                    if pressed_at and not st["long_fired"]:
                        try:
                            set_active_button(num, BUTTONS[num]["name"], 1.5)
                        except Exception:
                            pass
                        short_press(num)

                    st["pressed_at"] = None
                    st["long_fired"] = False

            # Long press while still held.
            if st["stable"] == 0 and st.get("pressed_at") and not st["long_fired"]:
                held = now - st["pressed_at"]
                if held >= LONG_PRESS_SEC:
                    st["long_fired"] = True
                    try:
                        set_active_button(num, BUTTONS[num]["name"] + " HOLD", 2.2)
                    except Exception:
                        pass
                    long_press(num)

        time.sleep(POLL_SEC)

finally:
    log("cleaning up GPIO")
    try:
        GPIO.cleanup()
    except Exception:
        pass
PY
