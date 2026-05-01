# 08 - Troubleshooting

## Service status

```bash
sudo systemctl status morse-whisperer.service --no-pager
sudo journalctl -u morse-whisperer.service -n 200 --no-pager
sudo tail -f /var/log/morse-whisperer/service.log
```

## Display is white or blank

First check framebuffer existence:

```bash
ls -l /dev/fb*
fbset -fb /dev/fb1 -s
```

Raw black frame test:

```bash
sudo dd if=/dev/zero of=/dev/fb1 bs=153600 count=1 status=none
```

For a 320x240 16-bit RGB565 framebuffer, `153600` bytes is one full frame.

If the display works after writing a black frame, the LCD driver is alive and the issue is likely application rendering or process state.

## `/dev/fb1` missing

Check boot overlay and dmesg:

```bash
grep -nEi 'dtoverlay=(pitft28|tft9341|ads7846)|dtparam=spi' /boot/config.txt /boot/firmware/config.txt 2>/dev/null

dmesg | grep -Ei 'ads7846|xpt2046|touch|stmpe|spi0|ili934|fbtft|fb1|pitft' | tail -180
```

The known-good health output reported:

```text
dtoverlay=pitft28-resistive,rotate=90,speed=32000000,fps=20
```

## Audio not detected

List devices:

```bash
arecord -l
/opt/morse-whisperer-pi/venv/bin/python /opt/morse-whisperer-pi/morse_whisperer_pi.py --list-audio
```

The working bundle reported:

```text
card 2: Device [USB Audio Device], device 0: USB Audio [USB Audio]
```

The running process used:

```text
arecord -q -D plughw:2,0 -r 8000 -f S16_LE -c 1 -t raw --period-size 64 --buffer-size 512
```

## SQL opens with no audio

Raise squelch in the web UI. The value persists to `config.json`.

API example:

```bash
curl -s -X POST http://127.0.0.1:8080/api/control/squelch \
  -H 'Content-Type: application/json' \
  -d '{"squelch_snr":2.8}' | python3 -m json.tool
```

## RAW starts with junk

RAW is literal. Startup artefacts can happen while tone and timing acquisition settle. Operator Copy may be cleaner than RAW.

Example acceptable behaviour:

```text
RAW: TEN CQ CQ DE ZL1SXG CALLING CQ CQ CQ
COPY: CQ CQ CQ DE ZL1SXG CALLING CQ CQ CQ
```

Use RAW for decoder tuning and Operator Copy for readable operation.

## Web page not loading

Check service and port:

```bash
sudo systemctl status morse-whisperer.service --no-pager
curl -s http://127.0.0.1:8080/api/snapshot | python3 -m json.tool
```

If using UFW:

```bash
sudo ufw allow 8080/tcp
```
