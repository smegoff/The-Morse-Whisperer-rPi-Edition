# Changelog

## 2026-05-07 - Raspberry Pi 4 Appliance Milestone

### Added

- Raspberry Pi 4 appliance rebuild documentation.
- USB audio capture support notes.
- 8000 Hz mono audio path documentation.
- Live CW decoder service notes.
- Web dashboard notes for port 8080.
- TFT framebuffer display documentation.
- Jaycar XC9022 / GoodTFT 2.8" display notes.
- Safe splash screen v2 notes.
- systemd service references for the main decoder.
- systemd sidecar service references for TFT buttons.
- Four physical TFT button documentation.
- TFT button press highlight documentation.
- Manual tone scan API documentation:
  - `POST /api/tone/scan`
- Manual tone scan via Button 2.
- Reset / clear via Buttons 3 and 4.
- TFT page switch via Button 1.
- TFT freeze via Button 1 long press.
- Diagnostic tone truth workflow.
- Offline WAV decode workflow.
- Known-good snapshot process.

### Confirmed Hardware Button Map

```text
Button 1 = GPIO23 = PAGE / hold FREEZE
Button 2 = GPIO22 = SCAN / hold RESTART
Button 3 = GPIO27 = RESET / hold FULL RESET
Button 4 = GPIO18 = CLEAR / hold FULL RESET
```

### Fixed / Learned

- Resolved stale splash / stuck framebuffer issue by moving splash into a safe standalone pre-start script.
- Removed unsafe in-app splash behaviour from the boot path.
- Reworked splash to show a handoff screen instead of sitting on a full progress bar.
- Fixed button detection by using `RPi.GPIO` pull-ups instead of passive `pinctrl get` polling.
- Confirmed GPIO22 and GPIO27 only respond correctly when configured with pull-ups.
- Fixed Button 2 scan action by adding `/api/tone/scan`.
- Updated TFT footer labels to show all four real buttons.
- Added visual feedback when a TFT button is pressed.

### Changed

Splash is now controlled by:

```json
{
  "splash_enabled": false,
  "systemd_splash_enabled": true,
  "safe_splash_seconds": 3.2
}
```

The main decoder display loop no longer owns splash behaviour. Button handling is performed by an external sidecar service.

### Current Known Good State

- Decoder running.
- Web UI running.
- TFT display running.
- Four buttons working.
- Splash v2 working.
- Manual tone scan working.
- Button press indication working.
