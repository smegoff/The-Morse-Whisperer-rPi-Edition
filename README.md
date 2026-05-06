# The Morse Whisperer

**The Morse Whisperer** is a Raspberry Pi 4 based CW / Morse decoder appliance designed for live radio audio, USB audio capture, TFT display output, and a browser-based dashboard.

The goal is simple:

> Turn real CW audio into readable copy with useful diagnostics, without needing a full desktop or SDR stack.

This project is built for practical ham radio use, testing, club demonstrations, and competition-style live decoding.

---

## Current Platform

This rebuild currently targets:

- Raspberry Pi 4
- Raspberry Pi OS / Debian-based install
- Jaycar XC9022 / GoodTFT 2.8" LCD
- USB audio capture device / USB microphone
- 8000 Hz mono audio
- Python-based decoder service
- Flask web dashboard
- framebuffer TFT display
- systemd services

---

## Current Features

- Live CW decoding from USB audio
- Automatic tone detection and lock
- Manual tone scan button
- Supported CW tone range:
  - 400 Hz
  - 500 Hz
  - 550 Hz
  - 600 Hz
  - 650 Hz
  - 700 Hz
  - 750 Hz
  - 800 Hz
  - 850 Hz
  - 900 Hz
  - 950 Hz
  - 1000 Hz
- Rolling live decode window
- RAW and cleaned COPY output
- Web dashboard
- TFT display output
- Safe splash screen with handoff
- Four physical TFT buttons
- Button press highlight on TFT
- Diagnostic WAV recording tools
- Offline WAV decode tooling
- Tone truth analysis
- Audio level diagnostics
- systemd service deployment

---

## Web Dashboard

Default web UI:

```text
http://<pi-ip-address>:8080
