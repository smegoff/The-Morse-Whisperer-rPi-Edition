# 00 - Overview

The Morse Whisperer is a Raspberry Pi CW decoder appliance. It listens to audio from a USB sound card, detects a CW tone using a Goertzel detector, decodes Morse into live copy, and displays the result on both a small LCD and a web dashboard.

The current build is designed for a headless shack appliance, not a desktop application. The Pi boots a systemd service, waits for the framebuffer LCD, shows a splash screen, and starts the decoder when the operator presses the MODE button or hits Enter.

## Runtime architecture

```text
systemd service
    ↓
start_morse_whisperer.sh
    ↓
morse_whisperer_splash.py
    ↓
morse_whisperer_pi.py
        ├── audio capture thread
        ├── decoder / DSP loop
        ├── background tone scanner
        ├── LCD display thread
        ├── physical button thread
        ├── console status thread
        └── web dashboard / QSO export server
```

## Decode model

The decoder uses:

- 8 kHz sample rate
- 64-sample DSP blocks
- exact-frequency Goertzel tone detection
- automatic tone acquisition and lock
- adaptive dot/dash timing
- adaptive gap classification
- squelch based on detector activity and SNR threshold

## Display model

The system separates three views of decoded content:

| View | Purpose |
|---|---|
| Operator Copy | Human-friendly cleaned copy for the operator |
| Raw Decode | Literal decoder stream, useful for tuning and honesty |
| QSO Export | Current-session fields and downloadable records |

RAW is deliberately not perfect. It may include startup artefacts while tone and timing settle. Operator Copy applies light formatting to make common ham/CW traffic readable.

## Current milestone

This documentation describes the working Phase 4.x appliance state:

- splash boot flow
- LCD decoder UI
- web dashboard
- persistent runtime squelch setting
- QSO save and ADIF export foundation
- health and backup tooling
