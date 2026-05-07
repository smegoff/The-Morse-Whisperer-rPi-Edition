#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path as _MWPath
sys.path.insert(0, str(_MWPath(__file__).resolve().parents[1]))
sys.path.insert(0, str(_MWPath(__file__).resolve().parent))
from decode_wav import read_wav
from morse_whisperer.config import load_config
from morse_whisperer.dsp import audio_metrics, rank_tones

def main():
    ap = argparse.ArgumentParser(description="Tone truth check: rank CW tone candidates without decoding.")
    ap.add_argument("wav")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    samples, rate, channels, sw, frames = read_wav(args.wav)
    metrics = audio_metrics(samples)
    ranking = rank_tones(samples, rate, cfg.get("allowed_tones_hz"))
    report = {"file": args.wav, "sample_rate": rate, "duration_sec": frames/float(rate), "audio": metrics.__dict__, "tone_ranking": [r.__dict__ for r in ranking]}
    if args.json:
        print(json.dumps(report, indent=2)); return
    print("=== Tone truth ===")
    print(f"File: {args.wav}  {rate} Hz  {frames/float(rate):.2f}s")
    print(f"Level: {metrics.level_status} RMS={metrics.rms:.5f} peak={metrics.peak:.3f} clipping={metrics.clipping_percent:.4f}%")
    print("Tone ranking:")
    for r in ranking:
        print(f"  {r.tone_hz:4d} Hz  score={r.score:.6e}")
    if len(ranking) >= 2:
        ratio = (ranking[0].score + 1e-12)/(ranking[1].score + 1e-12)
        print(f"Winner: {ranking[0].tone_hz} Hz  ratio={ratio:.2f}")
if __name__ == "__main__": main()

