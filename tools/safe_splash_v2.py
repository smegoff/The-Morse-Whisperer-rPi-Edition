#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


APP_DIR = Path("/opt/morse-whisperer-pi")
CONFIG_PATH = APP_DIR / "config.json"


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.25)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def font(size: int, bold: bool = False):
    candidates = []
    if bold:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
        ]
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
    ]
    for f in candidates:
        if Path(f).exists():
            try:
                return ImageFont.truetype(f, size)
            except Exception:
                pass
    return ImageFont.load_default()


def measure(draw, text: str, f) -> tuple[int, int]:
    try:
        b = draw.textbbox((0, 0), text, font=f)
        return b[2] - b[0], b[3] - b[1]
    except Exception:
        return len(text) * 8, 14


def find_fb(cfg: dict) -> str | None:
    preferred = cfg.get("framebuffer_candidates", ["/dev/fb1", "/dev/fb0"])
    for candidate in preferred:
        if os.path.exists(candidate) and os.access(candidate, os.W_OK):
            return candidate
    return None


def write_fb(img: Image.Image, fb_path: str) -> None:
    # This TFT framebuffer is RGB565 little-endian.
    rgb = img.convert("RGB")
    data = bytearray()

    # Pillow 14 deprecates getdata(), but this is fine on current Pi builds.
    for r, g, b in rgb.getdata():
        v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        data.append(v & 0xFF)
        data.append((v >> 8) & 0xFF)

    with open(fb_path, "wb", buffering=0) as f:
        f.write(data)


def draw_frame(progress: float, cfg: dict, phase: str = "INITIALISING") -> Image.Image:
    w, h = 320, 240

    bg = (3, 7, 13)
    panel = (8, 16, 28)
    panel2 = (12, 26, 46)
    line = (54, 126, 210)
    grid = (12, 34, 58)
    text = (238, 248, 255)
    muted = (150, 170, 190)
    blue = (85, 190, 255)
    cyan = (90, 255, 245)
    green = (80, 255, 170)
    amber = (255, 215, 90)

    f_title1 = font(22, True)
    f_title2 = font(28, True)
    f_mid = font(13, True)
    f_small = font(11)
    f_tiny = font(10)

    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle((7, 7, w - 7, h - 7), radius=15, fill=panel, outline=line)
    draw.rounded_rectangle((11, 11, w - 11, h - 11), radius=12, outline=(18, 52, 88))

    # Subtle oscilloscope grid.
    for gx in range(24, w - 22, 24):
        draw.line((gx, 84, gx, 146), fill=grid)
    for gy in range(90, 146, 14):
        draw.line((22, gy, w - 22, gy), fill=grid)

    # Title.
    t1 = "THE MORSE"
    t2 = "WHISPERER"
    tw, _ = measure(draw, t1, f_title1)
    draw.text(((w - tw) // 2, 20), t1, font=f_title1, fill=text)
    tw, _ = measure(draw, t2, f_title2)
    draw.text(((w - tw) // 2, 44), t2, font=f_title2, fill=blue)

    # Central emblem: radio fox / signal badge.
    cx, cy = 160, 114
    draw.rounded_rectangle((104, 78, 216, 148), radius=14, fill=panel2, outline=blue)

    # Signal waves.
    draw.arc((cx - 70, cy - 28, cx - 32, cy + 30), 105, 255, fill=cyan, width=2)
    draw.arc((cx + 32, cy - 28, cx + 70, cy + 30), -75, 75, fill=cyan, width=2)

    # Fox/radio face.
    draw.polygon([(cx - 36, cy - 7), (cx - 15, cy - 36), (cx - 8, cy - 3)], fill=(205, 218, 236), outline=(70, 130, 190))
    draw.polygon([(cx + 36, cy - 7), (cx + 15, cy - 36), (cx + 8, cy - 3)], fill=(205, 218, 236), outline=(70, 130, 190))
    draw.polygon(
        [(cx - 34, cy - 6), (cx, cy - 24), (cx + 34, cy - 6), (cx + 22, cy + 22), (cx, cy + 35), (cx - 22, cy + 22)],
        fill=(180, 200, 224),
        outline=(70, 130, 190),
    )
    draw.polygon([(cx - 14, cy + 5), (cx, cy + 31), (cx + 14, cy + 5)], fill=(238, 245, 250))
    draw.ellipse((cx - 18, cy - 3, cx - 10, cy + 5), fill=(5, 14, 25))
    draw.ellipse((cx + 10, cy - 3, cx + 18, cy + 5), fill=(5, 14, 25))
    draw.ellipse((cx - 4, cy + 8, cx + 4, cy + 14), fill=(5, 14, 25))

    subtitle = "CW DECODER APPLIANCE"
    sw, _ = measure(draw, subtitle, f_mid)
    draw.text(((w - sw) // 2, 156), subtitle, font=f_mid, fill=muted)

    tags = "AUTO TONE  ·  LIVE COPY  ·  WEB UI"
    tw, _ = measure(draw, tags, f_tiny)
    draw.text(((w - tw) // 2, 174), tags, font=f_tiny, fill=blue)

    url = f"http://{local_ip()}:{int(cfg.get('web_port', 8080))}"
    uw, _ = measure(draw, url, f_small)
    draw.text(((w - uw) // 2, 188), url, font=f_small, fill=green)

    # Progress bar. Stop at 96% for normal frames; final handoff frame is separate.
    progress = max(0.0, min(1.0, progress))
    bx, by, bw, bh = 28, 210, w - 56, 10
    draw.rounded_rectangle((bx, by, bx + bw, by + bh), radius=5, fill=(3, 10, 18), outline=(55, 85, 120))
    fill_w = int(bw * progress)
    if fill_w > 0:
        draw.rounded_rectangle((bx, by, bx + fill_w, by + bh), radius=5, fill=blue)

    phase = phase.upper()
    pw, _ = measure(draw, phase, f_tiny)
    draw.text(((w - pw) // 2, 224), phase, font=f_tiny, fill=amber)

    return img


def draw_handoff(cfg: dict) -> Image.Image:
    w, h = 320, 240
    bg = (3, 7, 13)
    panel = (8, 16, 28)
    line = (54, 126, 210)
    text = (238, 248, 255)
    muted = (150, 170, 190)
    green = (80, 255, 170)

    f_big = font(23, True)
    f_mid = font(14, True)
    f_small = font(11)

    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle((8, 8, w - 8, h - 8), radius=15, fill=panel, outline=line)

    msg = "STARTING LIVE DECODER"
    mw, _ = measure(draw, msg, f_big)
    draw.text(((w - mw) // 2, 78), msg, font=f_big, fill=text)

    sub = "TFT handoff to main display"
    sw, _ = measure(draw, sub, f_mid)
    draw.text(((w - sw) // 2, 118), sub, font=f_mid, fill=muted)

    url = f"http://{local_ip()}:{int(cfg.get('web_port', 8080))}"
    uw, _ = measure(draw, url, f_small)
    draw.text(((w - uw) // 2, 156), url, font=f_small, fill=green)

    return img


def main() -> int:
    cfg = load_config()

    # Manual script still respects this for boot mode, but --manual-test bypasses it.
    manual_test = "--manual-test" in sys.argv

    if not manual_test and not bool(cfg.get("systemd_splash_enabled", False)):
        return 0

    seconds = float(cfg.get("safe_splash_seconds", 3.2) or 3.2)
    seconds = max(0.8, min(seconds, 4.5))

    fb = find_fb(cfg)
    if not fb:
        print("safe_splash_v2: no writable framebuffer found", file=sys.stderr)
        return 0

    start = time.monotonic()
    deadline = start + seconds

    # Draw limited frames. Never sit at a full loading bar forever.
    frames = max(8, int(seconds * 8))

    for i in range(frames):
        now = time.monotonic()
        if now >= deadline:
            break

        progress = min(0.96, (i + 1) / max(frames, 1) * 0.96)
        write_fb(draw_frame(progress, cfg, "INITIALISING DECODER"), fb)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.10, remaining))

    # Handoff frame makes it obvious the splash has finished.
    try:
        write_fb(draw_handoff(cfg), fb)
        time.sleep(0.25)
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"safe_splash_v2 failed: {exc}", file=sys.stderr)
        raise SystemExit(0)
