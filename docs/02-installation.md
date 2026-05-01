# 02 - Installation

This guide documents the current working appliance layout. It assumes a Raspberry Pi with Python, ALSA, the LCD framebuffer driver, and a Python virtual environment already prepared.

## Application path

Production path:

```text
/opt/morse-whisperer-pi
```

Expected files:

```text
/opt/morse-whisperer-pi/morse_whisperer_pi.py
/opt/morse-whisperer-pi/morse_whisperer_splash.py
/opt/morse-whisperer-pi/config.json
/opt/morse-whisperer-pi/mw_health.py
/opt/morse-whisperer-pi/mw_backup.py
/opt/morse-whisperer-pi/start_morse_whisperer.sh
/opt/morse-whisperer-pi/venv/bin/python
```

## Python dependencies

The code imports:

- `numpy`
- `Pillow`
- `sounddevice`
- `RPi.GPIO`

The decoder can fall back to `arecord` for audio capture, but `sounddevice` is still used for device enumeration and preferred capture.

## Install scripts and commands

Copy the application files into `/opt/morse-whisperer-pi`, then make scripts executable:

```bash
sudo chmod +x /opt/morse-whisperer-pi/start_morse_whisperer.sh
sudo chmod +x /opt/morse-whisperer-pi/mw_health.py
sudo chmod +x /opt/morse-whisperer-pi/mw_backup.py
```

Recommended helper commands:

```bash
sudo tee /usr/local/bin/mw-health > /dev/null <<'SH'
#!/usr/bin/env bash
exec /opt/morse-whisperer-pi/venv/bin/python /opt/morse-whisperer-pi/mw_health.py "$@"
SH
sudo chmod +x /usr/local/bin/mw-health

sudo tee /usr/local/bin/mw-backup > /dev/null <<'SH'
#!/usr/bin/env bash
exec /opt/morse-whisperer-pi/venv/bin/python /opt/morse-whisperer-pi/mw_backup.py "$@"
SH
sudo chmod +x /usr/local/bin/mw-backup
```

## Systemd service

Install the service as:

```text
/etc/systemd/system/morse-whisperer.service
```

Then enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable morse-whisperer.service
sudo systemctl restart morse-whisperer.service
```

## Verify

Run:

```bash
mw-health
sudo systemctl status morse-whisperer.service --no-pager
```

A good baseline has all OK results except the optional capture test warning if `mw-health --capture` was not requested.
