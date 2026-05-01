#!/usr/bin/env python3
"""
The Morse Whisperer - standalone splash launcher

Purpose:
- Own the pre-decoder splash screen.
- Keep the decoder code clean.
- Show project branding, horse mascot, and START DECODER button.
- Start the real decoder only when requested.

Start methods:
- Press Enter on keyboard/SSH console
- Press physical MODE button on GPIO23, active-low

GPIO safety:
- This launcher only uses GPIO23.
- It deliberately does not use GPIO22 or GPIO27.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import select
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

try:
    import RPi.GPIO as GPIO
except Exception as exc:
    GPIO = None
    GPIO_IMPORT_ERROR = exc
else:
    GPIO_IMPORT_ERROR = None

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
except Exception as exc:
    print(f"[SPLASH] fatal: Pillow/numpy unavailable: {exc}", file=sys.stderr)
    raise


PROJECT_NAME = "The Morse Whisperer"
PROJECT_TAG = "Beep -> Words"
START_TEXT = "START DECODER"

DEFAULT_CMD = [
    "/opt/morse-whisperer-pi/venv/bin/python",
    "/opt/morse-whisperer-pi/morse_whisperer_pi.py",
]

SAFE_START_GPIO = 23


def detect_fb_path(candidates) -> Path:
    for cand in candidates:
        p = Path(cand)
        if p.exists() and os.access(str(p), os.W_OK):
            return p
    raise RuntimeError("No writable framebuffer found")


def fb_name(fb_path: Path) -> str:
    return fb_path.name


def sys_fb_path(fb_path: Path, name: str) -> Path:
    return Path("/sys/class/graphics") / fb_name(fb_path) / name


def detect_size(fb_path: Path) -> Tuple[int, int]:
    vs = sys_fb_path(fb_path, "virtual_size")
    if vs.exists():
        txt = vs.read_text().strip()
        if "," in txt:
            w, h = txt.split(",", 1)
            return int(w), int(h)

    try:
        out = subprocess.check_output(["fbset", "-fb", str(fb_path), "-s"], text=True)
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("geometry"):
                parts = line.split()
                return int(parts[1]), int(parts[2])
    except Exception:
        pass

    return 320, 240


def detect_bpp(fb_path: Path) -> int:
    bpp = sys_fb_path(fb_path, "bits_per_pixel")
    if bpp.exists():
        return int(bpp.read_text().strip())
    return 16


def load_font(size: int, bold: bool = False):
    if bold:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        ]

    for fp in candidates:
        try:
            return ImageFont.truetype(fp, int(size))
        except Exception:
            pass

    return ImageFont.load_default()


def text_wh(draw: ImageDraw.ImageDraw, text: str, font) -> Tuple[int, int]:
    try:
        bb = draw.multiline_textbbox((0, 0), text, font=font, spacing=2, stroke_width=1)
        return max(0, bb[2] - bb[0]), max(0, bb[3] - bb[1])
    except Exception:
        try:
            return draw.textsize(text, font=font)
        except Exception:
            return len(str(text)) * 8, 16


def rgb565_bytes(img: Image.Image) -> bytes:
    arr = np.asarray(img.convert("RGB"), dtype=np.uint16)
    r = (arr[:, :, 0] >> 3) & 0x1F
    g = (arr[:, :, 1] >> 2) & 0x3F
    b = (arr[:, :, 2] >> 3) & 0x1F
    rgb565 = (r << 11) | (g << 5) | b
    return rgb565.astype("<u2").tobytes()


def image_to_fb_bytes(img: Image.Image, bpp: int) -> bytes:
    if bpp == 16:
        return rgb565_bytes(img)
    if bpp == 24:
        return img.convert("RGB").tobytes()
    if bpp == 32:
        return img.convert("RGBA").tobytes("raw", "BGRA")
    raise RuntimeError(f"Unsupported framebuffer bpp: {bpp}")


def draw_horse(draw: ImageDraw.ImageDraw, box):
    x0, y0, x1, y1 = box
    pw = max(1, x1 - x0)
    ph = max(1, y1 - y0)

    def X(v): return int(x0 + v * pw)
    def Y(v): return int(y0 + v * ph)

    # Neck
    draw.polygon(
        [(X(0.24), Y(0.74)), (X(0.48), Y(0.50)), (X(0.66), Y(0.60)), (X(0.45), Y(0.88))],
        fill=(155, 111, 63),
    )

    # Head and muzzle
    draw.ellipse((X(0.30), Y(0.20), X(0.78), Y(0.58)), fill=(170, 122, 72), outline=(90, 60, 35))
    draw.ellipse((X(0.60), Y(0.34), X(0.95), Y(0.60)), fill=(198, 150, 100), outline=(90, 60, 35))

    # Ears
    draw.polygon([(X(0.38), Y(0.23)), (X(0.46), Y(0.03)), (X(0.56), Y(0.23))],
                 fill=(150, 105, 58), outline=(90, 60, 35))
    draw.polygon([(X(0.52), Y(0.21)), (X(0.65), Y(0.02)), (X(0.72), Y(0.25))],
                 fill=(150, 105, 58), outline=(90, 60, 35))

    # Mane
    draw.polygon(
        [
            (X(0.31), Y(0.24)),
            (X(0.20), Y(0.36)),
            (X(0.21), Y(0.52)),
            (X(0.30), Y(0.63)),
            (X(0.42), Y(0.52)),
            (X(0.47), Y(0.36)),
            (X(0.43), Y(0.24)),
        ],
        fill=(70, 42, 24),
    )

    # Face details
    draw.ellipse((X(0.55), Y(0.30), X(0.61), Y(0.36)), fill=(0, 0, 0))
    draw.ellipse((X(0.84), Y(0.47), X(0.90), Y(0.52)), fill=(70, 42, 24))
    draw.arc((X(0.70), Y(0.45), X(0.95), Y(0.62)), start=18, end=140, fill=(90, 60, 35), width=2)



# MW_SPLASH_BUTTON_RECT_V1
def splash_button_rect(width: int, height: int):
    margin = max(8, int(min(width, height) * 0.04))
    bx0 = margin + 12
    by1 = height - margin - 12
    by0 = by1 - 38
    bx1 = width - margin - 12
    return bx0, by0, bx1, by1


def draw_splash(width: int, height: int, pulse: float = 0.0) -> Image.Image:
    img = Image.new("RGB", (width, height), (8, 12, 20))
    draw = ImageDraw.Draw(img)

    margin = max(8, int(min(width, height) * 0.04))
    gap = max(6, int(width * 0.025))

    outer = (margin, margin, width - margin - 1, height - margin - 1)

    # Background frame
    draw.rectangle((0, 0, width, height), fill=(8, 12, 20))
    draw.rounded_rectangle(
        outer,
        radius=max(10, margin + 4),
        outline=(70, 120, 170),
        width=2,
    )

    # Mascot panel
    panel_w = min(max(92, int(width * 0.34)), 116)
    panel_x1 = width - margin - 8
    panel_x0 = panel_x1 - panel_w
    panel_y0 = margin + 14
    panel_y1 = height - margin - 58

    draw.rounded_rectangle(
        (panel_x0, panel_y0, panel_x1, panel_y1),
        radius=16,
        fill=(18, 24, 36),
        outline=(95, 165, 220),
        width=2,
    )
    draw_horse(draw, (panel_x0 + 4, panel_y0 + 6, panel_x1 - 4, panel_y1 - 6))

    # Text
    tx = margin + 12
    text_right = panel_x0 - gap
    text_w = max(120, text_right - tx)

    title_text = "MORSE\nWHISPERER"
    title_size = max(20, int(height * 0.145))

    while title_size > 16:
        font = load_font(title_size, bold=True)
        tw, th = text_wh(draw, title_text, font)
        if tw <= text_w and th <= int(height * 0.44):
            title_font = font
            break
        title_size -= 1
    else:
        title_font = load_font(16, bold=True)

    tag_font = load_font(max(12, int(height * 0.055)), bold=True)
    small_font = load_font(max(10, int(height * 0.044)), bold=False)
    button_font = load_font(max(13, int(height * 0.062)), bold=True)

    draw.text(
        (tx, margin + 22),
        title_text,
        font=title_font,
        fill=(245, 248, 252),
        spacing=2,
        stroke_width=1,
        stroke_fill=(0, 0, 0),
    )

    draw.text((tx, height - margin - 92), PROJECT_TAG, font=tag_font, fill=(255, 220, 100))
    draw.text((tx, height - margin - 66), "Press MODE or Enter", font=small_font, fill=(180, 215, 245))

    # Start button
    bx0, by0, bx1, by1 = splash_button_rect(width, height)

    glow = int(30 + 25 * pulse)
    fill = (20, 56 + glow, 82 + glow)
    outline = (90, 220, 255)

    draw.rounded_rectangle(
        (bx0, by0, bx1, by1),
        radius=13,
        fill=fill,
        outline=outline,
        width=2,
    )

    tw, th = text_wh(draw, START_TEXT, button_font)
    draw.text(
        (bx0 + ((bx1 - bx0) - tw) // 2, by0 + ((by1 - by0) - th) // 2 - 1),
        START_TEXT,
        font=button_font,
        fill=(255, 255, 255),
        stroke_width=1,
        stroke_fill=(0, 0, 0),
    )

    return img


def write_fb(fb_path: Path, img: Image.Image, bpp: int) -> None:
    """
    Write one full frame to the Linux framebuffer.

    Do this the same way the decoder renderer does it: unbuffered write to the
    framebuffer device. Path.write_bytes() can behave oddly with character
    devices on some fbtft overlays.
    """
    data = image_to_fb_bytes(img, bpp)
    with open(str(fb_path), "wb", buffering=0) as fb:
        fb.write(data)


def setup_gpio_button() -> bool:
    if GPIO is None:
        print(f"[SPLASH] GPIO unavailable; keyboard Enter only: {GPIO_IMPORT_ERROR}", flush=True)
        return False

    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(SAFE_START_GPIO, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        print(f"[SPLASH] start button enabled: GPIO{SAFE_START_GPIO} active-low", flush=True)
        return True
    except Exception as exc:
        print(f"[SPLASH] GPIO setup failed; keyboard Enter only: {exc}", flush=True)
        return False


def gpio_pressed() -> bool:
    if GPIO is None:
        return False
    try:
        return GPIO.input(SAFE_START_GPIO) == 0
    except Exception:
        return False



# MW_SPLASH_TOUCH_V1
class SplashTouch:
    EV_KEY = 0x01
    EV_ABS = 0x03

    ABS_X = 0x00
    ABS_Y = 0x01
    ABS_PRESSURE = 0x18
    BTN_TOUCH = 0x14A

    @staticmethod
    def _eviocgabs(abs_code: int) -> int:
        # Linux EVIOCGABS(abs): _IOR('E', 0x40 + abs, struct input_absinfo)
        # input_absinfo is 6 ints = 24 bytes.
        return (2 << 30) | (24 << 16) | (0x45 << 8) | (0x40 + abs_code)

    @staticmethod
    def auto_event_path() -> str:
        devices = Path("/proc/bus/input/devices")
        if not devices.exists():
            return ""

        text = devices.read_text(errors="replace")
        blocks = [b for b in text.split("\n\n") if b.strip()]
        candidates = []
        available = []

        for block in blocks:
            name = ""
            handlers = ""

            for line in block.splitlines():
                if line.startswith("N: Name="):
                    name = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("H: Handlers="):
                    handlers = line.split("=", 1)[1].strip()

            if "event" not in handlers:
                continue

            available.append(f"{name} [{handlers}]")
            lname = name.lower()

            # Avoid USB audio HID/kbd control surfaces.
            if "audio" in lname or "keyboard" in lname or "c-media" in lname:
                continue

            score = 0
            for token in ("touchscreen", "touch", "ads7846", "stmpe", "xpt2046", "tft", "ili9341"):
                if token in lname:
                    score += 10

            # Prefer devices that are not mouse/keyboard-like.
            if "mouse" in lname:
                score -= 4

            for h in handlers.split():
                if h.startswith("event"):
                    event_path = f"/dev/input/{h}"
                    if score > 0:
                        candidates.append((score, name, event_path))

        if candidates:
            candidates.sort(reverse=True)
            score, name, event_path = candidates[0]
            print(f"[SPLASH] touch auto-selected {event_path}: {name}", flush=True)
            return event_path

        print("[SPLASH] no touchscreen input event auto-detected", flush=True)
        if available:
            print("[SPLASH] input events: " + " | ".join(available), flush=True)

        return ""

    def __init__(
        self,
        event_path: str,
        width: int,
        height: int,
        button_rect,
        invert_x: bool = False,
        invert_y: bool = False,
        swap_xy: bool = False,
        debug: bool = False,
    ):
        self.event_path = event_path
        self.width = int(width)
        self.height = int(height)
        self.button_rect = button_rect
        self.invert_x = bool(invert_x)
        self.invert_y = bool(invert_y)
        self.swap_xy = bool(swap_xy)
        self.debug = bool(debug)

        self.fd = None
        self.x = None
        self.y = None
        self.touching = False
        self.down_started = 0.0
        self.last_release = 0.0

        self.x_min = 0
        self.x_max = 4095
        self.y_min = 0
        self.y_max = 4095

        # 64-bit input_event: long sec, long usec, ushort type, ushort code, int value.
        self.event_fmt = "llHHi"
        self.event_size = struct.calcsize(self.event_fmt)

    def open(self) -> bool:
        if not self.event_path:
            return False

        path = Path(self.event_path)
        if not path.exists():
            print(f"[SPLASH] touch event not found: {self.event_path}", flush=True)
            return False

        try:
            self.fd = path.open("rb", buffering=0)
            os.set_blocking(self.fd.fileno(), False)
            self._read_abs_info()
            print(
                f"[SPLASH] touch enabled {self.event_path} "
                f"x={self.x_min}..{self.x_max} y={self.y_min}..{self.y_max} "
                f"invert_x={self.invert_x} invert_y={self.invert_y} swap_xy={self.swap_xy}",
                flush=True,
            )
            return True
        except PermissionError:
            print(
                f"[SPLASH] permission denied on {self.event_path}; "
                f"try: sudo usermod -aG input decoder",
                flush=True,
            )
        except Exception as exc:
            print(f"[SPLASH] touch open failed: {exc}", flush=True)

        return False

    def close(self) -> None:
        try:
            if self.fd is not None:
                self.fd.close()
        except Exception:
            pass
        self.fd = None

    def _read_abs_info(self) -> None:
        if self.fd is None:
            return

        try:
            for code, attr_min, attr_max in [
                (self.ABS_X, "x_min", "x_max"),
                (self.ABS_Y, "y_min", "y_max"),
            ]:
                buf = bytearray(24)
                fcntl.ioctl(self.fd.fileno(), self._eviocgabs(code), buf, True)
                _value, minimum, maximum, _fuzz, _flat, _res = struct.unpack("iiiiii", buf)
                setattr(self, attr_min, int(minimum))
                setattr(self, attr_max, int(maximum))
        except Exception as exc:
            print(f"[SPLASH] ABS range read failed; using defaults: {exc}", flush=True)

    def _screen_xy(self):
        if self.x is None or self.y is None:
            return None

        xr = max(1, self.x_max - self.x_min)
        yr = max(1, self.y_max - self.y_min)

        nx = (float(self.x) - float(self.x_min)) / float(xr)
        ny = (float(self.y) - float(self.y_min)) / float(yr)

        nx = max(0.0, min(1.0, nx))
        ny = max(0.0, min(1.0, ny))

        if self.invert_x:
            nx = 1.0 - nx
        if self.invert_y:
            ny = 1.0 - ny

        if self.swap_xy:
            nx, ny = ny, nx

        sx = int(nx * max(1, self.width - 1))
        sy = int(ny * max(1, self.height - 1))

        return sx, sy, nx, ny

    def _release_on_button(self) -> bool:
        pos = self._screen_xy()
        if pos is None:
            return False

        sx, sy, nx, ny = pos
        x0, y0, x1, y1 = self.button_rect

        hit = x0 <= sx <= x1 and y0 <= sy <= y1

        if self.debug:
            print(
                f"[SPLASH] touch release raw=({self.x},{self.y}) "
                f"norm=({nx:.2f},{ny:.2f}) screen=({sx},{sy}) "
                f"button=({x0},{y0},{x1},{y1}) hit={hit}",
                flush=True,
            )

        return hit

    def poll_start(self) -> bool:
        if self.fd is None:
            return False

        try:
            while True:
                readable, _, _ = select.select([self.fd], [], [], 0)
                if not readable:
                    return False

                data = self.fd.read(self.event_size)
                if not data or len(data) != self.event_size:
                    return False

                _sec, _usec, etype, code, value = struct.unpack(self.event_fmt, data)

                if etype == self.EV_ABS:
                    if code == self.ABS_X:
                        self.x = int(value)
                    elif code == self.ABS_Y:
                        self.y = int(value)
                    elif code == self.ABS_PRESSURE:
                        # Some controllers only expose pressure, some only BTN_TOUCH.
                        if int(value) > 0 and not self.touching:
                            self.touching = True
                            self.down_started = time.monotonic()

                elif etype == self.EV_KEY and code == self.BTN_TOUCH:
                    if int(value):
                        self.touching = True
                        self.down_started = time.monotonic()
                    else:
                        if self.touching:
                            self.touching = False
                            now = time.monotonic()
                            if now - self.last_release < 0.20:
                                continue
                            self.last_release = now
                            if self._release_on_button():
                                return True

        except BlockingIOError:
            return False
        except Exception as exc:
            print(f"[SPLASH] touch read failed: {exc}", flush=True)
            return False


def enter_pressed() -> bool:
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return False
        readable, _, _ = select.select([sys.stdin], [], [], 0)
        if readable:
            sys.stdin.readline()
            return True
    except Exception:
        return False
    return False


def start_decoder(cmd):
    print("[SPLASH] starting decoder: " + " ".join(cmd), flush=True)
    os.execv(cmd[0], cmd)


def main() -> int:
    parser = argparse.ArgumentParser(description="The Morse Whisperer splash launcher")
    parser.add_argument("--fb", default="", help="Framebuffer path, e.g. /dev/fb1")
    parser.add_argument("--timeout", type=float, default=0.0, help="Auto-start after N seconds; 0 disables")
    parser.add_argument("--no-gpio", action="store_true", help="Do not listen for GPIO start button")
    parser.add_argument("--touch-event", default="auto", help="Touch input event path, e.g. /dev/input/event1, or auto")
    parser.add_argument("--touch", action="store_true", help="Enable touchscreen start button")
    parser.add_argument("--no-touch", action="store_true", help="Disable touchscreen start button; kept for compatibility")
    parser.add_argument("--touch-debug", action="store_true", help="Print touch coordinate debug")
    parser.add_argument("--invert-touch-x", action="store_true", help="Invert touch X axis")
    parser.add_argument("--invert-touch-y", action="store_true", help="Invert touch Y axis")
    parser.add_argument("--swap-touch-xy", action="store_true", help="Swap touch X/Y axes")
    parser.add_argument("--decoder-arg", action="append", default=[], help="Extra argument passed to decoder")
    args = parser.parse_args()

    candidates = [args.fb] if args.fb else ["/dev/fb1", "/dev/fb0"]
    fb_path = detect_fb_path([c for c in candidates if c])
    width, height = detect_size(fb_path)
    bpp = detect_bpp(fb_path)

    print(f"[SPLASH] using {fb_path} {width}x{height} {bpp}bpp", flush=True)

    gpio_enabled = False if args.no_gpio else setup_gpio_button()

    touch = None
    # Touch is deliberately opt-in. The ADS7846 layer works intermittently on
    # this LCD-show/kernel combination, so the production splash should rely on
    # GPIO23/Enter unless --touch is explicitly requested.
    if bool(getattr(args, "touch", False)) and not args.no_touch:
        touch_event = SplashTouch.auto_event_path() if args.touch_event == "auto" else str(args.touch_event)
        if touch_event:
            touch = SplashTouch(
                event_path=touch_event,
                width=width,
                height=height,
                button_rect=splash_button_rect(width, height),
                invert_x=bool(args.invert_touch_x),
                invert_y=bool(args.invert_touch_y),
                swap_xy=bool(args.swap_touch_xy),
                debug=bool(args.touch_debug),
            )
            if not touch.open():
                touch = None

    start_time = time.monotonic()
    last_draw = 0.0

    try:
        while True:
            now = time.monotonic()

            if now - last_draw >= 0.10:
                pulse = 0.5 + 0.5 * __import__("math").sin(now * 3.0)
                img = draw_splash(width, height, pulse)
                write_fb(fb_path, img, bpp)
                last_draw = now

            if enter_pressed():
                break

            if touch is not None and touch.poll_start():
                print("[SPLASH] touchscreen START DECODER pressed", flush=True)
                break

            if gpio_enabled and gpio_pressed():
                # debounce and wait for release
                time.sleep(0.08)
                if gpio_pressed():
                    while gpio_pressed():
                        time.sleep(0.03)
                    break

            if args.timeout > 0 and (now - start_time) >= args.timeout:
                print(f"[SPLASH] timeout {args.timeout:.1f}s reached; auto-starting", flush=True)
                break

            time.sleep(0.03)

    except KeyboardInterrupt:
        print("\n[SPLASH] interrupted; exiting", flush=True)
        return 130

    finally:
        if touch is not None:
            touch.close()
        if GPIO is not None:
            try:
                GPIO.cleanup(SAFE_START_GPIO)
            except Exception:
                pass

    # Quick visual acknowledgement.
    try:
        img = draw_splash(width, height, 1.0)
        d = ImageDraw.Draw(img)
        font = load_font(max(14, int(height * 0.065)), bold=True)
        msg = "Starting decoder..."
        bb = d.textbbox((0, 0), msg, font=font)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        d.rounded_rectangle(
            (18, height // 2 - 24, width - 18, height // 2 + 28),
            radius=12,
            fill=(8, 16, 30),
            outline=(255, 220, 90),
            width=2,
        )
        d.text(((width - tw) // 2, height // 2 - th // 2), msg, font=font, fill=(255, 255, 255))
        write_fb(fb_path, img, bpp)
    except Exception:
        pass

    cmd = DEFAULT_CMD + list(args.decoder_arg)
    start_decoder(cmd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
