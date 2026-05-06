# The Morse Whisperer rPi Edition

**The Morse Whisperer rPi Edition** is a Raspberry Pi 4 based CW / Morse decoder appliance designed for live radio audio, USB audio capture, TFT display output, physical controls, and a browser-based dashboard.

The goal is simple:

> Turn real CW audio into readable copy with useful diagnostics, without needing a full desktop or SDR stack.

This project is built for practical amateur radio use, club demonstrations, testing, and competition-style live decoding.

---

## Current Platform

This rebuild currently targets:

- Raspberry Pi 4
- Raspberry Pi OS / Debian-based install
- Jaycar XC9022 / GoodTFT 2.8" LCD
- USB audio capture device / USB microphone
- 8000 Hz mono audio
- Python decoder service
- Flask web dashboard
- framebuffer TFT display
- systemd services

---

## Current Features

- Live CW decoding from USB audio
- Automatic tone detection and lock
- Manual tone scan button and API
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
```

The dashboard shows:

- Live COPY
- Literal RAW
- detected / locked tone
- WPM estimate
- confidence / signal quality
- audio level status
- queue / buffer state
- selected audio device
- reset and control actions

---

## TFT Display

The TFT display shows:

- current stable copy
- tone lock status
- WPM
- signal quality / confidence
- audio level
- web URL
- button legend
- button press feedback

Current confirmed button layout on the XC9022 / GoodTFT 2.8" display:

```text
Button 1 = GPIO23 = PAGE / hold FREEZE
Button 2 = GPIO22 = SCAN / hold RESTART
Button 3 = GPIO27 = RESET / hold FULL RESET
Button 4 = GPIO18 = CLEAR / hold FULL RESET
```

Buttons are active-low and require pull-ups.

---

## Important GPIO Notes

Earlier assumptions said GPIO22 and GPIO27 were unsafe because they may be used by some LCD configurations.

On this tested Raspberry Pi 4 + XC9022 / GoodTFT 2.8" build, all four buttons were confirmed working with `RPi.GPIO` pull-ups:

```text
GPIO23 = Button 1
GPIO22 = Button 2
GPIO27 = Button 3
GPIO18 = Button 4
```

Do not assume this applies to every GoodTFT clone or every LCD-show overlay. Test your exact hardware before enabling all four buttons.

---

## Services

Main decoder service:

```bash
sudo systemctl status morse-whisperer
sudo systemctl restart morse-whisperer
```

Button sidecar service:

```bash
sudo systemctl status morse-whisperer-buttons
sudo systemctl restart morse-whisperer-buttons
```

Watch button logs:

```bash
sudo journalctl -u morse-whisperer-buttons -f --no-pager
```

Watch decoder logs:

```bash
sudo journalctl -u morse-whisperer -f --no-pager
```

---

## Manual Tone Scan

Manual tone scan endpoint:

```bash
curl -s -X POST http://127.0.0.1:8080/api/tone/scan | python3 -m json.tool
```

Button 2 calls this endpoint.

This clears the current tone lock/session and causes the decoder to reacquire from current live audio.

---

## Offline WAV Testing

Record a test WAV:

```bash
arecord -D plughw:X,Y -r 8000 -f S16_LE -c 1 -d 45 /tmp/mw-test.wav
```

Tone truth:

```bash
/opt/morse-whisperer-pi/venv/bin/python /opt/morse-whisperer-pi/tools/tone_truth.py /tmp/mw-test.wav
```

Offline decode:

```bash
/opt/morse-whisperer-pi/venv/bin/python /opt/morse-whisperer-pi/tools/decode_wav.py /tmp/mw-test.wav --tone auto
```

---

## Known Good Test Result

A clean 700 Hz CW recording produced strong tone lock and good copy.

Example tone truth:

```text
Winner: 700 Hz
Winner ratio: 41+
Audio level: GOOD
```

Example decoded output:

```text
RAW:
VVV VVVZL2RO ZL1SXG GOOD TOHEAR YOU73

COPY:
ZL2RO ZL1SXG GOOD TO HEAR YOU 73
```

---

## Design Philosophy

This project favours simple, robust DSP over clever magic.

Current decoder approach:

- USB audio capture at 8000 Hz mono
- Goertzel / tone correlation
- rolling tone scan
- adaptive tone lock
- envelope / mark-space extraction
- hysteresis and squelch
- adaptive dot/dash timing
- honest RAW output
- lightly cleaned COPY output

COPY cleanup must not hard-code entire expected phrases. RAW remains the truth.

---

## Current Status

The current Raspberry Pi 4 build is considered competition-demo stable:

- live decoder works
- web UI works
- TFT works
- four buttons work
- splash v2 works
- manual tone scan works
- reset / clear functions work
- button press feedback works

Further work should focus on:

- more real-radio testing
- better human-fist CW handling
- improved noisy signal rejection
- better confidence scoring
- cleaner packaged installer
- documentation and release packaging
