# 05 - Web Interface

The decoder runs a local web dashboard on port `8080`.

Open from another device on the same LAN:

```text
http://<pi-ip>:8080/
```

The LCD displays the web URL when available.

## Main sections

The web interface is laid out as an operator console:

- top status bar: mode, tone, WPM, SNR, sync, UTC clock
- operator copy panel
- signal strip including squelch control
- raw decode panel
- current session panel
- QSO draft panel
- decode metrics panel

## Squelch control

The squelch slider posts to:

```text
POST /api/control/squelch
```

Example:

```bash
curl -s -X POST http://127.0.0.1:8080/api/control/squelch \
  -H 'Content-Type: application/json' \
  -d '{"squelch_snr":2.8}' | python3 -m json.tool
```

The setting is written back to `/opt/morse-whisperer-pi/config.json`.

## API endpoints

Current endpoints:

```text
GET  /api/snapshot
GET  /api/qso/current
GET  /api/qso/recent
POST /api/qso/save
POST /api/control/squelch
```

Download endpoints:

```text
GET /download/current.txt
GET /download/current.md
GET /download/current.json
GET /download/current.csv
GET /download/current.adi
GET /download/qso/<id>.json
GET /download/qso/<id>.adi
```

## QRZ status

QRZ upload is not implemented yet. The UI and QSO fields are shaped for future ADIF/QRZ workflow.

Do not store QRZ credentials in source code. Add a dedicated settings file or environment-based secret handling before implementing uploads.
