# 01 - Hardware

## Tested target

- Raspberry Pi 4
- GoodTFT / Jaycar XC9022 style 2.8 inch SPI LCD
- USB audio capture device
- Linux framebuffer display on `/dev/fb1`

## Display

The working system reports:

```text
/dev/fb0
/dev/fb1
fb1 name: fb_ili9340
virtual_size: 320,240
bits_per_pixel: 16
stride: 640
```

The working LCD overlay reported by `mw-health` is:

```text
dtoverlay=pitft28-resistive,rotate=90,speed=32000000,fps=20
```

The display is written directly through the framebuffer. No desktop environment is required.

## Touchscreen status

Touchscreen input is not required for production use.

The project previously tested ADS7846/XPT2046 style touch input, but it was unreliable on this LCD-show/kernel combination and created risk around LCD GPIO conflicts. The current production path is:

- splash start: GPIO23 MODE button or Enter key
- decoder controls: physical buttons
- richer controls: web interface

## GPIO safety

Known GPIO safety notes from development:

- Avoid GPIO22 and GPIO27 for new inputs on this LCD family unless the exact overlay and wiring are verified.
- Avoid GPIO17 for touch experiments unless ADS7846 PENIRQ usage is understood.
- Do not use SPI chip-select pins GPIO7/GPIO8 for buttons.

The current decoder button map is:

| Button | GPIO | Short press | Long press |
|---|---:|---|---|
| MODE | GPIO23 | Cycle mode | RX/timing reset |
| SCAN | GPIO22 | Request tone scan | Enable auto-track |
| RESET | GPIO27 | Reset timing | Return to splash |
| CLEAR | GPIO18 | Clear copy | Cycle LCD page |

The splash launcher uses GPIO23 only.

## USB audio

The current health bundle reports a C-Media USB audio device:

```text
card 2: Device [USB Audio Device], device 0: USB Audio [USB Audio]
```

The config prefers these USB IDs:

```json
[
  { "vid": "046d", "pid": "081d" },
  { "vid": "0d8c", "pid": "0014" }
]
```

If direct PortAudio capture fails, the decoder falls back to `arecord` with ALSA `plughw` conversion.
