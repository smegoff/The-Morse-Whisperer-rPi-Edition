# 10 - Known-Good Baseline

This document records the known-good state from the uploaded documentation bundle.

## Health check summary

```text
The Morse Whisperer - Health Check
RESULT: WARN (1 warning)
```

The warning was:

```text
Audio capture test: skipped; run mw-health --capture to test recording
```

All other checks were OK.

## Key config values

```json
{
  "sample_rate": 8000,
  "block_n": 64,
  "target_tone_hz": 700.0,
  "squelch_snr": 2.5,
  "decode_delay_ms": 1500,
  "tone_scan_history_sec": 3.0,
  "prelock_replay_ms": 340
}
```

Display:

```json
{
  "enabled": true,
  "framebuffer_candidates": ["/dev/fb1", "/dev/fb0"],
  "fps": 8,
  "font_size": 16
}
```

## Framebuffer

```text
/dev/fb0
/dev/fb1
fb1: fb_ili9340
320x240
16 bpp
```

## Audio

```text
card 2: Device [USB Audio Device], device 0: USB Audio [USB Audio]
```

## Service

```text
morse-whisperer.service enabled and active
Main PID: python /opt/morse-whisperer-pi/morse_whisperer_pi.py
Child: arecord -q -D plughw:2,0 -r 8000 -f S16_LE -c 1 -t raw --period-size 64 --buffer-size 512
```

## Web interface

```text
http://<pi-ip>:8080/
```

## Baseline operator behaviour

- boots to splash
- MODE/Enter starts decoder
- LCD shows live decode
- web dashboard works
- squelch slider works and persists
- QSO save/ADIF export present
- RAW remains literal
- Operator Copy is human-readable
