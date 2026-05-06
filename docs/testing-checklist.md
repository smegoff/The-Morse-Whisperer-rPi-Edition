# The Morse Whisperer Testing Checklist

Use this checklist after installing or patching the Raspberry Pi 4 appliance build.

## Service Health

```bash
sudo systemctl status morse-whisperer --no-pager -l
sudo systemctl status morse-whisperer-buttons --no-pager -l
```

Expected:

```text
morse-whisperer = active
morse-whisperer-buttons = active
```

## Web Dashboard

Open:

```text
http://<pi-ip-address>:8080
```

Check:

- [ ] COPY is visible.
- [ ] RAW is visible.
- [ ] tone status is visible.
- [ ] audio status is visible.
- [ ] reset works.
- [ ] page updates continue.

## TFT Display

Check:

- [ ] splash appears at boot/restart.
- [ ] splash hands off to live display.
- [ ] live COPY page appears.
- [ ] tone/WPM/status areas update.
- [ ] web URL is visible.
- [ ] all four button labels are visible.
- [ ] button press highlight works.

## Buttons

Watch logs:

```bash
sudo journalctl -u morse-whisperer-buttons -f --no-pager
```

Press each button.

Expected:

```text
Button 1 GPIO23 pressed/released
Button 2 GPIO22 pressed/released
Button 3 GPIO27 pressed/released
Button 4 GPIO18 pressed/released
```

Functional tests:

- [ ] Button 1 short press changes TFT page.
- [ ] Button 1 long press freezes/unfreezes TFT.
- [ ] Button 2 short press calls `/api/tone/scan`.
- [ ] Button 3 short press resets copy/buffer.
- [ ] Button 4 short press clears/resets copy/buffer.

## Manual Tone Scan API

```bash
curl -s -X POST http://127.0.0.1:8080/api/tone/scan | python3 -m json.tool
```

Expected:

```json
{
  "ok": true,
  "tone_scan_counter": 1,
  "tone_scan_requested_at": 1234567890.123
}
```

Check snapshot:

```bash
curl -s http://127.0.0.1:8080/api/snapshot | python3 -m json.tool | grep -A8 -B3 tone_scan
```

## Tone Lock Test

1. Play/send CW at 700 Hz.
2. Confirm decoder locks to 700 Hz.
3. Switch source to 550 Hz.
4. Press Button 2.
5. Send CW again.
6. Confirm decoder reacquires 550 Hz.

Expected:

- [ ] tone changes after scan.
- [ ] copy continues without service restart.
- [ ] no old stale copy overwrites better copy.

## WAV Capture Test

Record:

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

## Cold Boot Test

Reboot:

```bash
sudo reboot
```

After boot:

- [ ] splash appears.
- [ ] live TFT appears.
- [ ] web dashboard works.
- [ ] buttons service starts.
- [ ] audio capture starts.
- [ ] decoder responds to CW.
