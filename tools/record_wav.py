#!/usr/bin/env python3
import argparse
import subprocess
from pathlib import Path

import sys
from pathlib import Path as _MWPath
sys.path.insert(0, str(_MWPath(__file__).resolve().parents[1]))
from morse_whisperer.config import load_config
from morse_whisperer.audio import ARecordCapture

def main():
    ap = argparse.ArgumentParser(description="Record a diagnostic mono 8 kHz WAV using arecord.")
    ap.add_argument("outfile", nargs="?", default="/tmp/mw-test.wav")
    ap.add_argument("--device", default="auto", help="ALSA device, e.g. plughw:1,0. Default: auto USB-ish capture")
    ap.add_argument("--seconds", "-d", type=int, default=45)
    ap.add_argument("--rate", "-r", type=int, default=None)
    args = ap.parse_args()
    cfg = load_config()
    rate = args.rate or int(cfg.get("sample_rate", 8000))
    dev = args.device
    detail = "requested"
    if dev.lower() == "auto":
        cap = ARecordCapture(None, cfg)  # type: ignore[arg-type]
        dev, detail = cap._select_device()
    out = Path(args.outfile)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["arecord", "-D", dev, "-r", str(rate), "-f", "S16_LE", "-c", "1", "-d", str(args.seconds), str(out)]
    print("Device:", dev, detail)
    print("Running:", " ".join(cmd))
    raise SystemExit(subprocess.call(cmd))
if __name__ == "__main__": main()

