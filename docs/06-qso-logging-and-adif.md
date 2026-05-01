# 06 - QSO Logging and ADIF

The web interface includes a QSO Draft card and can save the current decode as a persistent QSO record.

Saved QSOs are written to:

```text
/opt/morse-whisperer-pi/qso/
```

For each QSO the application writes:

```text
qso-*.json
qso-*.adi
```

## ADIF fields

The current QSO model supports:

| ADIF field | Description |
|---|---|
| CALL | Other station callsign |
| QSO_DATE | UTC date as `YYYYMMDD` |
| TIME_ON | UTC start time as `HHMMSS` |
| TIME_OFF | UTC end time as `HHMMSS` |
| MODE | Usually `CW` |
| BAND | Operator-entered band |
| FREQ | Operator-entered frequency in MHz |
| RST_SENT | Sent report |
| RST_RCVD | Received report |
| NAME | Optional operator name |
| QTH | Optional location |
| COMMENT | Notes / decoder metadata |
| STATION_CALLSIGN | Optional station callsign |
| OPERATOR | Optional operator callsign |

## Internal metadata

The JSON record also contains:

- operator copy
- raw decode
- raw literal decode
- expanded copy
- QSO metrics
- average WPM
- SNR data
- tone data
- decoder version string

## Saving from the web UI

1. Decode a QSO or CQ call.
2. Confirm or edit Callsign, Band, Frequency, RST, Name, QTH, and Notes.
3. Click **Save QSO**.
4. Use **Export ADIF** to download an `.adi` file.

## Export current ADIF

```bash
curl -s http://127.0.0.1:8080/download/current.adi
```

## Recent QSOs

```bash
curl -s http://127.0.0.1:8080/api/qso/recent | python3 -m json.tool
```

## QRZ upload roadmap

QRZ upload should be added as a separate feature after credentials are handled safely.

Suggested future controls:

- QRZ enabled/disabled
- QRZ API/session key storage
- station callsign
- upload selected QSO
- upload pending QSOs
- store upload result/status per QSO
