# The Morse Whisperer

The Morse Whisperer is a Raspberry Pi based CW / Morse decoder appliance. It uses a USB audio input, a small SPI LCD, physical button controls, and a local web interface for live decode monitoring and QSO export.

The project started as a Raspberry Pi port of an ESP32 Morse decoder, but the Pi version now behaves more like a small shack appliance: it boots to a splash screen, starts the decoder on demand, displays copy on the LCD, exposes a browser dashboard, and can save current decode sessions as QSO records with ADIF export.

## Current status

This repository snapshot documents the working Raspberry Pi appliance build.

Working features:

- Raspberry Pi 4 runtime target
- USB audio capture through ALSA / `arecord`
- 8 kHz sample rate with 64-sample DSP blocks
- Goertzel tone detection
- automatic tone scan / tone lock
- adaptive dot, dash, and gap timing
- LCD framebuffer rendering on `/dev/fb1`
- standalone splash launcher
- physical button operation
- long RESET return-to-splash action
- local web dashboard on port `8080`
- runtime squelch slider, persisted to `config.json`
- QSO draft workflow
- TXT, Markdown, JSON, CSV, and ADIF export endpoints
- appliance health check command
- appliance backup command

Known limitations:

- RAW decode is literal and may include startup artefacts while tone and timing acquisition settle.
- Operator Copy applies light ham/CW readability formatting.
- QRZ upload is not implemented yet; the web interface is shaped for future QRZ/ADIF workflow.
- Touchscreen input is parked. The production path uses physical GPIO buttons and web controls.

## Hardware used in this build

- Raspberry Pi 4
- GoodTFT / Jaycar XC9022 style 2.8 inch SPI LCD
- USB audio input device
- C-Media USB audio capture device tested: `VID_0D8C PID_0014`
- Logitech-style USB audio device also preferred in config: `VID_046D PID_081D`

The health check from the working appliance reports:

```text
Framebuffers: /dev/fb0 /dev/fb1
Display framebuffer: /dev/fb1 writable
fb1: fb_ili9340, 320x240, 16 bpp
Touchscreen: not present / not required
ALSA capture device: USB Audio found
Systemd service: enabled and active
LCD boot overlay: dtoverlay=pitft28-resistive,rotate=90,speed=32000000,fps=20
```

## Quick start

The production install path is documented in [`docs/02-installation.md`](docs/02-installation.md).

Once installed, enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable morse-whisperer.service
sudo systemctl restart morse-whisperer.service
```

The appliance boots to the splash launcher. Press the physical MODE button, or press Enter on an attached console/SSH session, to start the decoder.

Open the web interface from another device on the LAN:

```text
http://<pi-ip>:8080/
```

## Useful commands

```bash
mw-health
mw-backup --name before-change
sudo systemctl status morse-whisperer.service --no-pager
sudo tail -f /var/log/morse-whisperer/service.log
```

## Documentation

- [Overview](docs/00-overview.md)
- [Hardware](docs/01-hardware.md)
- [Installation](docs/02-installation.md)
- [Boot and service](docs/03-boot-and-service.md)
- [Decoder operation](docs/04-decoder-operation.md)
- [Web interface](docs/05-web-interface.md)
- [QSO logging and ADIF](docs/06-qso-logging-and-adif.md)
- [Health check and backup](docs/07-health-check-and-backup.md)
- [Troubleshooting](docs/08-troubleshooting.md)
- [Development notes](docs/09-development-notes.md)
- [Known-good baseline](docs/10-known-good-baseline.md)

## Repository layout

```text
.
├── README.md
├── config/
│   └── config.example.json
├── docs/
├── scripts/
│   ├── mw_backup.py
│   ├── mw_health.py
│   └── start_morse_whisperer.sh
├── screenshots/
└── src/
    ├── morse_whisperer_pi.py
    └── morse_whisperer_splash.py
```

## Licence

Add a `LICENSE` file before publishing publicly.
