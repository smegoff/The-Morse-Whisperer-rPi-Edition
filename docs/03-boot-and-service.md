# 03 - Boot and Service

## Service flow

The systemd service starts the wrapper script:

```text
/opt/morse-whisperer-pi/start_morse_whisperer.sh
```

The wrapper:

1. creates `/var/log/morse-whisperer/service.log`
2. redirects stdout/stderr into that log
3. waits briefly for `/dev/fb1`
4. starts the splash launcher with `--no-touch`

Production command:

```bash
/opt/morse-whisperer-pi/venv/bin/python -u /opt/morse-whisperer-pi/morse_whisperer_splash.py --no-touch
```

## Service unit

The current service uses:

```ini
[Unit]
Description=The Morse Whisperer Splash Launcher
After=multi-user.target sound.target local-fs.target
Wants=sound.target

[Service]
Type=simple
WorkingDirectory=/opt/morse-whisperer-pi
ExecStart=/opt/morse-whisperer-pi/start_morse_whisperer.sh
User=root
Restart=on-failure
RestartSec=2
Environment=PYTHONUNBUFFERED=1
Environment=TERM=linux
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

The wrapper writes the main appliance log to:

```text
/var/log/morse-whisperer/service.log
```

## Splash launcher

The splash launcher owns the pre-decoder screen.

Start methods:

- physical MODE button on GPIO23
- Enter key on an attached console/SSH session
- optional timeout if launched with `--timeout`

Touch support exists in the splash launcher but is opt-in and not used in production.

## Return to splash

From the decoder, long-press RESET to replace the decoder process with the splash launcher. This is a deliberate process handoff rather than a second service.

## Logs

Useful commands:

```bash
sudo systemctl status morse-whisperer.service --no-pager
sudo journalctl -u morse-whisperer.service -n 200 --no-pager
sudo tail -f /var/log/morse-whisperer/service.log
```
