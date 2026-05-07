from __future__ import annotations
import collections
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple
import numpy as np

@dataclass
class AudioDeviceInfo:
    backend: str
    device: str
    detail: str

class AudioRing:
    def __init__(self, sample_rate: int, max_seconds: float) -> None:
        self.sample_rate = sample_rate
        self.max_samples = int(sample_rate * max_seconds)
        self._buf: Deque[np.ndarray] = collections.deque()
        self._samples = 0
        self._lock = threading.Lock()
        self.overruns = 0
        self.total_samples = 0
        self.last_audio_at = 0.0

    def append(self, arr: np.ndarray) -> None:
        if arr.size == 0:
            return
        data = arr.astype(np.float32, copy=False)
        with self._lock:
            self._buf.append(data.copy())
            self._samples += data.size
            self.total_samples += data.size
            self.last_audio_at = time.time()
            while self._samples > self.max_samples and self._buf:
                old = self._buf.popleft()
                self._samples -= old.size
                self.overruns += 1

    def last(self, seconds: float) -> np.ndarray:
        need = int(self.sample_rate * seconds)
        with self._lock:
            chunks = list(self._buf)
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        arr = np.concatenate(chunks)
        if arr.size > need:
            arr = arr[-need:]
        return arr.astype(np.float32, copy=False)

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
            self._samples = 0

    def stats(self) -> Dict[str, float | int]:
        with self._lock:
            buffered = self._samples
            overruns = self.overruns
            total = self.total_samples
            last = self.last_audio_at
        return {
            "buffered_samples": buffered,
            "buffered_seconds": buffered / float(self.sample_rate),
            "overruns": overruns,
            "total_samples": total,
            "last_audio_at": last,
        }

class BaseCapture:
    def start(self) -> None: raise NotImplementedError
    def stop(self) -> None: raise NotImplementedError
    def info(self) -> AudioDeviceInfo: raise NotImplementedError

class SoundDeviceCapture(BaseCapture):
    def __init__(self, ring: AudioRing, config: Dict) -> None:
        self.ring = ring
        self.config = config
        self.sd = None
        self.stream = None
        self.device = None
        self.device_name = ""
        self.status_count = 0

    def _select_device(self):
        import sounddevice as sd
        requested = str(self.config.get("audio_device", "auto"))
        devices = sd.query_devices()
        if requested and requested.lower() != "auto":
            try:
                return int(requested), str(devices[int(requested)].get("name", requested))
            except Exception:
                for idx, d in enumerate(devices):
                    if requested.lower() in str(d.get("name", "")).lower() and int(d.get("max_input_channels", 0)) > 0:
                        return idx, str(d.get("name", ""))
                raise RuntimeError(f"Requested sounddevice input not found: {requested}")
        preferred = ("usb", "c-media", "logitech", "microphone", "capture", "audio")
        first_input = None
        for idx, d in enumerate(devices):
            if int(d.get("max_input_channels", 0)) <= 0:
                continue
            if first_input is None:
                first_input = (idx, str(d.get("name", "")))
            name = str(d.get("name", "")).lower()
            if any(p in name for p in preferred):
                return idx, str(d.get("name", ""))
        if first_input is None:
            raise RuntimeError("No sounddevice input devices found")
        return first_input

    def start(self) -> None:
        import sounddevice as sd
        self.sd = sd
        self.device, self.device_name = self._select_device()
        sr = int(self.config.get("sample_rate", 8000))
        blocksize = int(self.config.get("audio_blocksize", 64))
        def cb(indata, frames, time_info, status):
            if status:
                self.status_count += 1
            data = np.asarray(indata[:, 0], dtype=np.float32)
            self.ring.append(data)
        self.stream = sd.InputStream(device=self.device, channels=1, samplerate=sr, blocksize=blocksize, dtype="float32", callback=cb)
        self.stream.start()

    def stop(self) -> None:
        if self.stream:
            self.stream.stop()
            self.stream.close()

    def info(self) -> AudioDeviceInfo:
        return AudioDeviceInfo("sounddevice", str(self.device), f"{self.device_name}; callback_status_count={self.status_count}")

class ARecordCapture(BaseCapture):
    def __init__(self, ring: AudioRing, config: Dict) -> None:
        self.ring = ring
        self.config = config
        self.proc: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.device = "default"
        self.detail = ""

    @staticmethod
    def list_hw_devices() -> List[Tuple[str, str]]:
        try:
            p = subprocess.run(["arecord", "-l"], text=True, capture_output=True, check=False)
            text = p.stdout + "\n" + p.stderr
        except Exception:
            return []
        out: List[Tuple[str, str]] = []
        for line in text.splitlines():
            m = re.search(r"card\s+(\d+):\s*([^,]+).*device\s+(\d+):\s*([^\[]+)", line, flags=re.I)
            if m:
                card, cname, dev, dname = m.group(1), m.group(2), m.group(3), m.group(4).strip()
                out.append((f"plughw:{card},{dev}", f"card {card} {cname} device {dev} {dname}"))
        return out

    def _select_device(self) -> Tuple[str, str]:
        requested = str(self.config.get("audio_device", "auto"))
        if requested and requested.lower() != "auto":
            return requested, "requested"
        devices = self.list_hw_devices()
        preferred = ("usb", "c-media", "logitech", "microphone", "audio", "capture")
        for dev, detail in devices:
            if any(p in detail.lower() for p in preferred):
                return dev, detail
        if devices:
            return devices[0]
        return "default", "ALSA default"

    def start(self) -> None:
        self.device, self.detail = self._select_device()
        sr = str(int(self.config.get("sample_rate", 8000)))
        cmd = ["arecord", "-q", "-D", self.device, "-r", sr, "-f", "S16_LE", "-c", "1", "-t", "raw"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        chunk_bytes = int(self.config.get("audio_blocksize", 64)) * 2
        while not self.stop_event.is_set():
            data = self.proc.stdout.read(chunk_bytes)
            if not data:
                time.sleep(0.05)
                if self.proc.poll() is not None:
                    break
                continue
            arr = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            self.ring.append(arr)

    def stop(self) -> None:
        self.stop_event.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def info(self) -> AudioDeviceInfo:
        return AudioDeviceInfo("arecord", self.device, self.detail)

def start_capture(ring: AudioRing, config: Dict) -> BaseCapture:
    backend = str(config.get("audio_backend", "auto")).lower()
    errors: List[str] = []
    if backend in ("auto", "sounddevice"):
        try:
            cap = SoundDeviceCapture(ring, config)
            cap.start()
            return cap
        except Exception as e:
            errors.append(f"sounddevice failed: {e}")
            if backend == "sounddevice":
                raise
    if backend in ("auto", "arecord"):
        try:
            cap = ARecordCapture(ring, config)
            cap.start()
            return cap
        except Exception as e:
            errors.append(f"arecord failed: {e}")
            if backend == "arecord":
                raise
    raise RuntimeError("; ".join(errors) or "No audio backend available")
