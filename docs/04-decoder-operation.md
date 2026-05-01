# 04 - Decoder Operation

## Starting the decoder

After boot, the splash screen is shown. Press the MODE button or hit Enter to start the decoder.

## LCD pages and controls

The LCD displays live operator copy, signal state, mode, tone, WPM, SNR, and soft-key labels.

Physical buttons:

| Button | Short press | Long press |
|---|---|---|
| MODE | Cycle mode | RX/timing reset |
| SCAN | Request tone scan | Enable auto-track |
| RESET | Reset timing | Return to splash |
| CLEAR | Clear copy | Cycle LCD page |

## Runtime modes

The main production mode is AUTO/AUTO TRACK. The decoder can scan for the strongest allowed tone and lock on it.

Allowed tones by default:

```text
400, 500, 550, 600, 650, 700, 750, 800, 850, 900, 950, 1000 Hz
```

## Squelch

Squelch uses both detector activity and the configured SNR threshold. The web interface exposes a squelch slider. The current value is persisted to `config.json` and survives restart.

Current working value in the uploaded config:

```json
"squelch_snr": 2.5
```

If SQL opens with no real audio, raise the squelch threshold. If real CW does not open reliably, lower it slightly.

## RAW vs Operator Copy

The decoder intentionally separates:

- RAW Decode: literal decoder stream
- Operator Copy: human-readable ham/CW formatting

RAW can include startup artefacts while tone and timing acquisition settle. That is expected. Operator Copy is meant to be useful for reading and QSO logging, not a forensic stream.

## Test with WAV input

The decoder supports WAV input for repeatable tests:

```bash
/opt/morse-whisperer-pi/venv/bin/python /opt/morse-whisperer-pi/morse_whisperer_pi.py --wav /path/to/test.wav
```

Optional loop:

```bash
--wav-loop
```
