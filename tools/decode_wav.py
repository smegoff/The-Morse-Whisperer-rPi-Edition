#!/usr/bin/env python3
import argparse
import json
import wave
from pathlib import Path

import sys
from pathlib import Path as _MWPath
sys.path.insert(0, str(_MWPath(__file__).resolve().parents[1]))
import numpy as np
from morse_whisperer.config import load_config
from morse_whisperer.dsp import analyse_samples

def read_wav(path: str):
    with wave.open(path, "rb") as w:
        channels = w.getnchannels(); rate = w.getframerate(); sw = w.getsampwidth(); frames = w.getnframes()
        data = w.readframes(frames)
    if sw == 2:
        arr = np.frombuffer(data, dtype="<i2")
    elif sw == 1:
        arr = (np.frombuffer(data, dtype=np.uint8).astype(np.int16) - 128) << 8
    elif sw == 4:
        arr = (np.frombuffer(data, dtype="<i4") / 65536).astype(np.int16)
    else:
        raise RuntimeError(f"Unsupported sample width: {sw}")
    if channels > 1:
        arr = arr.reshape(-1, channels)[:,0]
    return arr, rate, channels, sw, frames

def main():
    ap = argparse.ArgumentParser(description="Offline decode and diagnostics for The Morse Whisperer WAV files.")
    ap.add_argument("wav")
    ap.add_argument("--tone", default=None, help="auto or Hz. Default uses config tone_mode/target_tone_hz")
    ap.add_argument("--wpm", type=float, default=None)
    ap.add_argument("--window-ms", type=float, default=None)
    ap.add_argument("--hop-ms", type=float, default=None)
    ap.add_argument("--threshold-bias", type=float, default=None)
    ap.add_argument("--event-comp-ms", type=float, default=None)
    ap.add_argument("--events", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    for key, val in (("window_ms", args.window_ms), ("hop_ms", args.hop_ms), ("threshold_bias", args.threshold_bias), ("event_comp_ms", args.event_comp_ms)):
        if val is not None: cfg[key] = val
    samples, rate, channels, sw, frames = read_wav(args.wav)
    cfg["sample_rate"] = int(rate)
    result = analyse_samples(samples, cfg, tone_override=args.tone, wpm_override=args.wpm)
    report = result.__dict__.copy()
    report["file"] = {"path": str(Path(args.wav)), "sample_rate": rate, "channels": channels, "sample_width_bytes": sw, "frames": frames, "duration_sec": frames/float(rate)}
    if args.json:
        print(json.dumps(report, indent=2))
        return
    print("=== The Morse Whisperer offline decode ===")
    f = report["file"]
    print(f"File: {f['path']}")
    print(f"Audio: {f['sample_rate']} Hz, channels={f['channels']}, width={f['sample_width_bytes']} bytes, duration={f['duration_sec']:.2f}s")
    a = report["audio"]
    print(f"Level: {a['level_status']}  RMS={a['rms']:.5f}  peak={a['peak']:.3f}  clipping={a['clipping_percent']:.4f}%  dc={a['dc_offset']:.5f}")
    print("Tone ranking:")
    for t in report["tone_ranking"][:12]:
        print(f"  {int(t['tone_hz']):4d} Hz  score={t['score']:.6e}")
    print(f"Selected tone: {report['selected_tone_hz']} Hz  target={report['target_tone_hz']} Hz  mode={report['tone_mode']}  mismatch={report['target_detected_mismatch']}")
    print(f"Winner ratio: {report['winner_ratio']:.2f}  SNR estimate: {report['snr_db']:.2f} dB")
    print(f"Threshold: {report['threshold']:.6e}  low={report['low_threshold']:.6e}  high={report['high_threshold']:.6e}")
    print(f"Timing: dot={report['dot_ms']:.1f} ms  WPM={report['wpm']:.2f}")
    print(f"Events: marks={report['marks']} spaces={report['spaces']} decoded={report['decoded_symbols']} failed={report['failed_symbols']}")
    print(f"Confidence: {report['confidence']:.2f}  reason={report['reason']}")
    print("RAW:")
    print(report["raw"] or "")
    print("COPY:")
    print(report["copy"] or "")
    if args.events:
        print("Events:")
        for e in report["events"]:
            print(f"  {e['kind']:5s} {e['ms']:7.1f} ms  {e['start_ms']:8.1f}-{e['end_ms']:8.1f}")
if __name__ == "__main__": main()
