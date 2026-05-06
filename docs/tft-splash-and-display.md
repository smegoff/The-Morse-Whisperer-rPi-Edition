# TFT Splash and Display Notes

The Morse Whisperer uses a framebuffer TFT display on the Raspberry Pi 4.

Current known framebuffer:

```text
/dev/fb1
fb_ili9340
320x240
16-bit RGB565
```

## Display Stack

The main decoder application owns the live TFT display.

The splash screen is separate and runs before the main app starts. This separation is intentional.

## Why Splash Is Separate

Earlier splash implementations inside the main display loop caused the framebuffer to become stuck on the splash screen.

The current design avoids that by using a standalone splash script:

```text
/opt/morse-whisperer-pi/tools/safe_splash_v2.py
```

systemd runs it before the main decoder service:

```text
ExecStartPre=-/usr/bin/timeout 6s /opt/morse-whisperer-pi/venv/bin/python /opt/morse-whisperer-pi/tools/safe_splash_v2.py
```

Important details:

- `timeout 6s` prevents boot blocking.
- the leading `-` means systemd ignores splash failure.
- the script exits before the main app starts.
- the splash shows a handoff frame instead of stopping on a full progress bar.

## Config

```json
{
  "splash_enabled": false,
  "systemd_splash_enabled": true,
  "safe_splash_seconds": 3.2
}
```

`"splash_enabled": false` means the old in-app splash path is disabled.

`"systemd_splash_enabled": true` enables the safe standalone splash.

## Emergency Disable

If the splash ever misbehaves:

```bash
sudo python3 - <<'PY'
import json
from pathlib import Path

p = Path("/opt/morse-whisperer-pi/config.json")
cfg = json.loads(p.read_text())

cfg["splash_enabled"] = False
cfg["systemd_splash_enabled"] = False

p.write_text(json.dumps(cfg, indent=2) + "\n")
PY

sudo sed -i '\|safe_splash_v2.py|d;\|safe_splash.py|d;\|splash_screen.py|d' \
  /etc/systemd/system/morse-whisperer.service

sudo systemctl daemon-reload
sudo systemctl restart morse-whisperer
```

## Framebuffer Test

To manually prove the TFT framebuffer works:

```bash
sudo systemctl stop morse-whisperer

sudo /opt/morse-whisperer-pi/venv/bin/python - <<'PY'
from PIL import Image, ImageDraw
import time

fb = "/dev/fb1"
w, h = 320, 240

img = Image.new("RGB", (w, h), (0, 0, 45))
draw = ImageDraw.Draw(img)

draw.rectangle((0, 0, w - 1, h - 1), outline=(0, 220, 255), width=5)
draw.text((28, 42), "TFT TEST", fill=(255, 255, 255))
draw.text((28, 88), "Framebuffer: /dev/fb1", fill=(120, 220, 255))
draw.text((28, 118), "If you see this, LCD is OK.", fill=(120, 255, 160))

data = bytearray()
for r, g, b in img.convert("RGB").getdata():
    v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    data.append(v & 0xFF)
    data.append((v >> 8) & 0xFF)

with open(fb, "wb", buffering=0) as f:
    f.write(data)

time.sleep(5)
PY

sudo systemctl start morse-whisperer
```
