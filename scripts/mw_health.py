#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


APP_DIR = Path("/opt/morse-whisperer-pi")
PYTHON = APP_DIR / "venv/bin/python"
DECODER = APP_DIR / "morse_whisperer_pi.py"
SPLASH = APP_DIR / "morse_whisperer_splash.py"
CONFIG = APP_DIR / "config.json"
SERVICE = "morse-whisperer.service"


class Health:
    def __init__(self) -> None:
        self.rows = []
        self.failed = 0
        self.warned = 0

    def ok(self, name: str, detail: str = "") -> None:
        self.rows.append(("OK", name, detail))

    def warn(self, name: str, detail: str = "") -> None:
        self.warned += 1
        self.rows.append(("WARN", name, detail))

    def fail(self, name: str, detail: str = "") -> None:
        self.failed += 1
        self.rows.append(("FAIL", name, detail))

    def run(self, name: str, cmd: list[str], timeout: float = 5.0):
        try:
            p = subprocess.run(
                cmd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
            return p
        except subprocess.TimeoutExpired as exc:
            self.fail(name, f"timeout after {timeout}s")
            return None
        except Exception as exc:
            self.fail(name, str(exc))
            return None

    def print_report(self) -> None:
        print("============================================================")
        print("The Morse Whisperer - Health Check")
        print("============================================================")

        for status, name, detail in self.rows:
            if status == "OK":
                prefix = "[ OK ]"
            elif status == "WARN":
                prefix = "[WARN]"
            else:
                prefix = "[FAIL]"

            if detail:
                print(f"{prefix} {name}: {detail}")
            else:
                print(f"{prefix} {name}")

        print("============================================================")

        if self.failed:
            print(f"RESULT: FAIL ({self.failed} failed, {self.warned} warning)")
        elif self.warned:
            print(f"RESULT: WARN ({self.warned} warning)")
        else:
            print("RESULT: OK")


def read_file(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except Exception:
        return ""


def check_files(h: Health) -> None:
    for path in [APP_DIR, PYTHON, DECODER, SPLASH]:
        if path.exists():
            h.ok("File exists", str(path))
        else:
            h.fail("Missing file", str(path))

    if CONFIG.exists():
        try:
            data = json.loads(CONFIG.read_text())
            h.ok("config.json parses", f"{len(data)} top-level keys")
        except Exception as exc:
            h.fail("config.json parse", str(exc))
    else:
        h.warn("config.json", "missing; decoder will use defaults")


def check_python_compile(h: Health) -> None:
    if not PYTHON.exists():
        h.fail("Python venv", f"missing {PYTHON}")
        return

    for script in [DECODER, SPLASH]:
        if not script.exists():
            continue

        p = h.run(
            f"compile {script.name}",
            [str(PYTHON), "-m", "py_compile", str(script)],
            timeout=10.0,
        )

        if p is None:
            continue

        if p.returncode == 0:
            h.ok(f"compile {script.name}")
        else:
            detail = (p.stderr or p.stdout).strip()
            h.fail(f"compile {script.name}", detail[:300])


def check_imports(h: Health) -> None:
    if not PYTHON.exists():
        return

    code = r'''
import sys
mods = ["numpy", "PIL"]
failed = []
for m in mods:
    try:
        __import__(m)
    except Exception as exc:
        failed.append(f"{m}: {exc}")
try:
    import sounddevice
except Exception as exc:
    failed.append(f"sounddevice: {exc}")
if failed:
    print("\n".join(failed))
    sys.exit(1)
print("imports ok")
'''

    p = h.run("Python imports", [str(PYTHON), "-c", code], timeout=10.0)

    if p is None:
        return

    if p.returncode == 0:
        h.ok("Python imports", p.stdout.strip())
    else:
        h.fail("Python imports", (p.stderr or p.stdout).strip()[:400])


def check_framebuffer(h: Health) -> None:
    fbs = sorted(Path("/dev").glob("fb*"))

    if fbs:
        h.ok("Framebuffers", " ".join(str(x) for x in fbs))
    else:
        h.fail("Framebuffers", "no /dev/fb* devices")
        return

    fb1 = Path("/dev/fb1")
    if not fb1.exists():
        h.fail("Display framebuffer", "/dev/fb1 missing")
        return

    if os.access(fb1, os.W_OK):
        h.ok("Display framebuffer", "/dev/fb1 writable")
    else:
        h.warn("Display framebuffer", "/dev/fb1 exists but is not writable by current user")

    sysfs = Path("/sys/class/graphics/fb1")
    details = []

    for name in ["name", "virtual_size", "bits_per_pixel", "stride", "blank"]:
        p = sysfs / name
        if p.exists():
            details.append(f"{name}={p.read_text().strip()}")

    if details:
        h.ok("fb1 sysfs", ", ".join(details))

    p = h.run("fbset fb1", ["fbset", "-fb", "/dev/fb1", "-s"], timeout=3.0)
    if p is not None:
        if p.returncode == 0:
            first = " ".join(line.strip() for line in p.stdout.splitlines()[:4])
            h.ok("fbset fb1", first[:220])
        else:
            h.warn("fbset fb1", (p.stderr or p.stdout).strip()[:220])


def check_input_devices(h: Health) -> None:
    inp = Path("/proc/bus/input/devices")
    text = read_file(inp)

    if not text:
        h.warn("Input devices", "could not read /proc/bus/input/devices")
        return

    if "ADS7846 Touchscreen" in text:
        h.warn("Touchscreen", "ADS7846 present, but production path keeps touch disabled")
    else:
        h.ok("Touchscreen", "not present / not required")

    if "C-Media Electronics Inc. USB Audio Device" in text:
        h.ok("USB audio HID", "C-Media event device present")
    else:
        h.warn("USB audio HID", "C-Media HID event not listed; not fatal")


def check_audio(h: Health, capture: bool) -> None:
    p = h.run("arecord list", ["arecord", "-l"], timeout=5.0)

    if p is None:
        return

    out = (p.stdout or "") + (p.stderr or "")

    if p.returncode != 0:
        h.fail("arecord -l", out.strip()[:400])
        return

    if "USB Audio" in out or "USB Audio Device" in out:
        h.ok("ALSA capture device", "USB Audio found")
    else:
        h.fail("ALSA capture device", "USB Audio capture device not found")

    if not capture:
        h.warn("Audio capture test", "skipped; run mw-health --capture to test recording")
        return

    tmp = Path("/tmp/mw-health-audio.raw")

    cmd = [
        "timeout",
        "1.2",
        "arecord",
        "-q",
        "-D",
        "plughw:2,0",
        "-r",
        "8000",
        "-f",
        "S16_LE",
        "-c",
        "1",
        "-t",
        "raw",
        str(tmp),
    ]

    p = h.run("audio capture", cmd, timeout=3.0)

    if p is None:
        return

    if tmp.exists() and tmp.stat().st_size > 1000:
        h.ok("audio capture", f"{tmp.stat().st_size} bytes captured via plughw:2,0")
    else:
        detail = ((p.stderr or "") + (p.stdout or "")).strip()
        h.fail("audio capture", detail[:400] or "no audio data captured")


def check_service(h: Health) -> None:
    p = h.run("systemd enabled", ["systemctl", "is-enabled", SERVICE], timeout=3.0)
    if p is not None:
        txt = (p.stdout or p.stderr).strip()
        if p.returncode == 0:
            h.ok("systemd enabled", txt)
        else:
            h.warn("systemd enabled", txt)

    p = h.run("systemd active", ["systemctl", "is-active", SERVICE], timeout=3.0)
    if p is not None:
        txt = (p.stdout or p.stderr).strip()
        if p.returncode == 0:
            h.ok("systemd active", txt)
        else:
            h.warn("systemd active", txt)

    log = Path("/var/log/morse-whisperer/service.log")
    if log.exists():
        h.ok("service log", f"{log} size={log.stat().st_size}")
    else:
        h.warn("service log", f"{log} missing")


def check_boot_config(h: Health) -> None:
    found = []

    for cfg in [Path("/boot/config.txt"), Path("/boot/firmware/config.txt")]:
        if not cfg.exists():
            continue

        text = cfg.read_text(errors="replace")
        active = []

        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "dtoverlay=pitft28-resistive" in stripped:
                active.append(stripped)
            if "dtoverlay=tft9341" in stripped:
                active.append(stripped)
            if "dtoverlay=ads7846" in stripped:
                active.append(stripped)

        if active:
            found.append(f"{cfg}: {' | '.join(active)}")

    if not found:
        h.warn("LCD boot overlay", "no active LCD dtoverlay found")
        return

    detail = " || ".join(found)

    if "pitft28-resistive" in detail and "dtoverlay=ads7846" not in detail and "dtoverlay=tft9341" not in detail:
        h.ok("LCD boot overlay", detail)
    else:
        h.warn("LCD boot overlay", detail)


def main() -> int:
    parser = argparse.ArgumentParser(description="The Morse Whisperer appliance health check")
    parser.add_argument("--capture", action="store_true", help="Run a short arecord capture test")
    args = parser.parse_args()

    h = Health()

    check_files(h)
    check_python_compile(h)
    check_imports(h)
    check_framebuffer(h)
    check_input_devices(h)
    check_audio(h, capture=args.capture)
    check_service(h)
    check_boot_config(h)

    h.print_report()

    return 2 if h.failed else 1 if h.warned else 0


if __name__ == "__main__":
    raise SystemExit(main())
