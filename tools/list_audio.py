#!/usr/bin/env python3
import subprocess

import sys
from pathlib import Path as _MWPath
sys.path.insert(0, str(_MWPath(__file__).resolve().parents[1]))
print("=== ALSA capture devices: arecord -l ===")
subprocess.run(["arecord", "-l"], check=False)
print("\n=== ALSA PCM devices: arecord -L | first 120 lines ===")
p = subprocess.run(["arecord", "-L"], text=True, capture_output=True, check=False)
print("\n".join((p.stdout or p.stderr).splitlines()[:120]))
print("\n=== sounddevice inputs ===")
try:
    import sounddevice as sd
    for i, d in enumerate(sd.query_devices()):
        if int(d.get('max_input_channels', 0)) > 0:
            print(f"{i}: {d.get('name')} inputs={d.get('max_input_channels')} default_sr={d.get('default_samplerate')}")
except Exception as e:
    print(f"sounddevice unavailable/error: {e}")

