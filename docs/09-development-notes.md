# 09 - Development Notes

## Design principles

- Keep the appliance usable without a desktop GUI.
- Prefer framebuffer and web UI over X11/Wayland dependencies.
- Keep RAW decode literal for diagnostics.
- Keep Operator Copy useful for humans.
- Do not hide decoder problems by rewriting RAW.
- Use backups before patches.
- Keep GPIO usage explicit and documented.

## Important trade-offs

### RAW vs Operator Copy

The RAW stream is not intended to be polished. It shows what the decoder committed. It is useful for tuning gap timing, tone lock, and startup behaviour.

Operator Copy is allowed to format common CW/ham traffic for readability.

### Touchscreen

Touchscreen support was tested but parked because it created reliability issues and could conflict with LCD control pins depending on overlay state. Physical buttons plus web controls are the safer production approach.

### Web controls

The web dashboard started read-only. Runtime squelch and QSO save were later added. Future settings should be added carefully and persisted in `config.json` or a dedicated settings file.

## Future improvements

- QRZ upload with safe credential handling
- persistent station settings
- QSO history page
- ADIF bulk export
- improved startup tone/timing acquisition
- optional WAV regression test suite
- cleaner module split: audio, decoder, display, web, qso, config
- formal install script
- screenshots in the repo

## Code clean-up notes

The current `morse_whisperer_pi.py` is functional but large. A future refactor should split it into modules only after the current appliance behaviour is frozen and backed up.

Suggested future modules:

```text
morse_whisperer/
  audio.py
  decoder.py
  display.py
  buttons.py
  web.py
  qso.py
  config.py
  health.py
```

Avoid refactoring decoder timing and display code in the same change.
