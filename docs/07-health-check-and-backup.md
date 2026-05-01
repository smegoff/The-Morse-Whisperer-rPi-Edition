# 07 - Health Check and Backup

## Health check

Run:

```bash
mw-health
```

Current uploaded health result:

```text
RESULT: WARN (1 warning)
```

The only warning was that the audio capture test was skipped. Use `mw-health --capture` to test recording.

Expected checks include:

- application files exist
- Python files compile
- imports work
- `/dev/fb1` exists and is writable
- framebuffer metadata is sane
- USB audio device is present
- ALSA capture device is present
- systemd service is enabled and active
- service log exists
- LCD overlay is detected

## Backup

Run before meaningful changes:

```bash
mw-backup --name before-change
```

Backups are written under:

```text
/opt/morse-whisperer-pi/backups/
```

The backup includes app files, service files, boot/config information where available, command output, recent logs, and a manifest.

## Recommended workflow

Before a patch:

```bash
mw-backup --name before-feature-name
```

After a successful feature:

```bash
mw-health
mw-backup --name feature-name-working
```

For fast manual checks:

```bash
/opt/morse-whisperer-pi/venv/bin/python -m py_compile /opt/morse-whisperer-pi/morse_whisperer_pi.py
/opt/morse-whisperer-pi/venv/bin/python -m py_compile /opt/morse-whisperer-pi/morse_whisperer_splash.py
sudo systemctl restart morse-whisperer.service
sudo systemctl status morse-whisperer.service --no-pager
```
