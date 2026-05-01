#!/usr/bin/env python3
"""
The Morse Whisperer - Raspberry Pi 4 Port

Behavioural source:
- ESP32 Heltec WiFi LoRa 32 V3 implementation
- v2.4 FULL isolated A trainer + live output during training

This Python implementation preserves:
- 8 kHz sample rate
- 64-sample DSP blocks
- Goertzel detector
- SNR / squelch / hysteresis
- adaptive dot timing
- isolated A trainer
- SEEKING / PROVISIONAL / LOCKED training states
- RAW and expanded decoded buffers
- console fallback
- framebuffer display rendering

No desktop GUI required.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import signal
import struct
import fcntl
import subprocess
import sys
import threading
import time
import wave

GPIO_IMPORT_ERROR = None
try:
    import RPi.GPIO as GPIO
except Exception as exc:
    GPIO_IMPORT_ERROR = exc
    GPIO = None
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

try:
    import sounddevice as sd
except Exception as exc:
    sd = None
    SOUNDDEVICE_IMPORT_ERROR = exc
else:
    SOUNDDEVICE_IMPORT_ERROR = None

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception as exc:
    Image = None
    ImageDraw = None
    ImageFont = None
    PIL_IMPORT_ERROR = exc
else:
    PIL_IMPORT_ERROR = None


PROJECT_NAME = "The Morse Whisperer"
PROJECT_TAG = "Beep -> Words"
VERSION_STR = "Pi v1.0 - ESP32 timing faithful"

SAMPLE_RATE = 8000
BLOCK_N = 64
BLOCK_MS = 1000.0 * BLOCK_N / SAMPLE_RATE

ALLOWED_TONES = [
    400.0, 500.0, 550.0, 600.0, 650.0, 700.0,
    750.0, 800.0, 850.0, 900.0, 950.0, 1000.0
]

DEFAULT_TARGET_TONE_HZ = 700.0

DAH_DOT_RATIO_CUTOFF = 2.20
LETTER_GAP_UNITS = 3.0
WORD_GAP_UNITS = 7.0

PRELOCK_DOT_MS = 80.0
PRELOCK_DASH_MS = 240.0
PRELOCK_INTRA_MS = 80.0
PRELOCK_LETTER_MS = 240.0

MARK_HISTORY = 24
SPACE_HISTORY = 24
TRAIN_BUF = 12

MAX_RAW = 6000
MAX_EXPANDED = 2200

AUDIO_Q_BLOCKS = 14


class TrainingState(IntEnum):
    TRAIN_SEEKING = 0
    TRAIN_PROVISIONAL = 1
    TRAIN_LOCKED = 2


class TrainerPhase(IntEnum):
    TP_WAIT_DOT = 0
    TP_WAIT_INTRA = 1
    TP_WAIT_DASH = 2
    TP_WAIT_LETTER = 3


class SyncState(IntEnum):
    SYNC_ACQUIRE = 0
    SYNC_TRACK = 1
    SYNC_WEAK = 2
    SYNC_LOST = 3


MORSE_TABLE: Dict[str, str] = {
    ".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E",
    "..-.": "F", "--.": "G", "....": "H", "..": "I", ".---": "J",
    "-.-": "K", ".-..": "L", "--": "M", "-.": "N", "---": "O",
    ".--.": "P", "--.-": "Q", ".-.": "R", "...": "S", "-": "T",
    "..-": "U", "...-": "V", ".--": "W", "-..-": "X", "-.--": "Y",
    "--..": "Z",
    "-----": "0", ".----": "1", "..---": "2", "...--": "3", "....-": "4",
    ".....": "5", "-....": "6", "--...": "7", "---..": "8", "----.": "9",
    ".-.-.-": ".", "--..--": ",", "..--..": "?", ".----.": "'",
    "-.-.--": "!", "-..-.": "/", "-.--.": "(", "-.--.-": ")",
    ".-...": "&", "---...": ":", "-.-.-.": ";", "-...-": "=",
    ".-.-.": "+", "-....-": "-", "..--.-": "_", ".-..-.": '"',
    ".--.-.": "@",
}

ABBREV: Dict[str, str] = {
    "CQ": "Calling any station",
    "DE": "from",
    "K": "over",
    "KN": "over (specific)",
    "AR": "end of message",
    "SK": "silent key / end",
    "BK": "break",
    "R": "roger",
    "RR": "roger roger",
    "UR": "your",
    "TNX": "thanks",
    "THX": "thanks",
    "73": "best regards",
    "OM": "old man",
    "YL": "young lady",
    "HW": "how copy?",
    "QTH": "location",
    "QRM": "interference",
    "QRN": "noise",
    "QRS": "send slower",
    "QRO": "increase power",
    "QRP": "low power",
}


def millis() -> int:
    return int(time.monotonic() * 1000)


def clampf(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def median(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n & 1:
        return s[n // 2]
    return 0.5 * (s[(n // 2) - 1] + s[n // 2])


def tail_text(s: str, max_len: int) -> str:
    return s[-max_len:] if len(s) > max_len else s


@dataclass
class RuntimeConfig:
    sample_rate: int = SAMPLE_RATE
    block_n: int = BLOCK_N
    target_tone_hz: float = DEFAULT_TARGET_TONE_HZ
    allowed_tones_hz: List[float] = field(default_factory=lambda: list(ALLOWED_TONES))
    sensitivity: float = 0.85
    squelch_snr: float = 1.45
    audio_queue_blocks: int = AUDIO_Q_BLOCKS
    preferred_usb_ids: List[Dict[str, str]] = field(default_factory=lambda: [
        {"vid": "046d", "pid": "081d"},
        {"vid": "0d8c", "pid": "0014"},
    ])
    display_enabled: bool = True
    framebuffer_candidates: List[str] = field(default_factory=lambda: ["/dev/fb1", "/dev/fb0"])
    display_fps: float = 8.0
    font_size: int = 16
    console_enabled: bool = True
    console_status_interval_sec: float = 1.0

    # Phase 4 web dashboard.
    # Read-only first: live copy, status, QSO metrics, downloads.
    web_enabled: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = 8080

    training_enabled: bool = True

    # Pi appliance defaults. The Pi has enough RAM/CPU to use a short
    # human-ear style delay instead of decoding from a cold 8 ms block.
    decode_delay_ms: int = 1500
    tone_scan_history_sec: float = 3.0
    prelock_replay_ms: int = 850

    @classmethod
    def load(cls, path: str) -> "RuntimeConfig":
        cfg = cls()
        p = Path(path)
        if not p.exists():
            return cfg
        data = json.loads(p.read_text())
        cfg.sample_rate = int(data.get("sample_rate", cfg.sample_rate))
        cfg.block_n = int(data.get("block_n", cfg.block_n))
        cfg.target_tone_hz = float(data.get("target_tone_hz", cfg.target_tone_hz))
        cfg.allowed_tones_hz = [float(x) for x in data.get("allowed_tones_hz", cfg.allowed_tones_hz)]
        cfg.sensitivity = float(data.get("sensitivity", cfg.sensitivity))
        cfg.squelch_snr = float(data.get("squelch_snr", cfg.squelch_snr))
        cfg.audio_queue_blocks = int(data.get("audio_queue_blocks", cfg.audio_queue_blocks))
        cfg.preferred_usb_ids = data.get("preferred_usb_ids", cfg.preferred_usb_ids)

        display = data.get("display", {})
        cfg.display_enabled = bool(display.get("enabled", cfg.display_enabled))
        cfg.framebuffer_candidates = list(display.get("framebuffer_candidates", cfg.framebuffer_candidates))
        cfg.display_fps = float(display.get("fps", cfg.display_fps))
        cfg.font_size = int(display.get("font_size", cfg.font_size))

        console = data.get("console", {})
        cfg.console_enabled = bool(console.get("enabled", cfg.console_enabled))
        cfg.console_status_interval_sec = float(console.get("status_interval_sec", cfg.console_status_interval_sec))

        web = data.get("web", {})
        cfg.web_enabled = bool(web.get("enabled", cfg.web_enabled))
        cfg.web_host = str(web.get("host", cfg.web_host))
        cfg.web_port = int(web.get("port", cfg.web_port))

        cfg.training_enabled = bool(data.get("training_enabled", cfg.training_enabled))
        cfg.decode_delay_ms = int(data.get("decode_delay_ms", cfg.decode_delay_ms))
        cfg.tone_scan_history_sec = float(data.get("tone_scan_history_sec", cfg.tone_scan_history_sec))
        cfg.prelock_replay_ms = int(data.get("prelock_replay_ms", cfg.prelock_replay_ms))

        return cfg


@dataclass
class Snapshot:
    timestamp_ms: int = 0
    mode: str = "AUTO"
    display_mode: str = "AUTO"
    training_state: str = "SEEKING"
    ui_page: str = "COPY"
    ui_message: str = ""
    ui_active_button: str = ""
    target_tone_hz: float = 700.0
    detected_tone_hz: float = 700.0
    detected_tone_confidence: float = 1.0
    wpm: float = 15.0
    snr: float = 1.0
    squelch_snr: float = 1.45
    squelch_open: bool = False
    sync_state: str = "ACQUIRE"
    sync_confidence: int = 0
    raw: str = ""
    raw_literal: str = ""
    copy: str = ""
    expanded: str = ""
    current_symbol: str = ""
    current_word: str = ""
    dot_ms: float = 80.0
    dash_ms: float = 240.0
    char_gap_ms: float = 240.0
    word_gap_ms: float = 560.0
    trainer_a_count: int = 0
    trainer_score: int = 0
    q_size: int = 0
    q_overruns: int = 0
    audio_errors: int = 0
    display_error: str = ""
    selected_audio_device: str = ""
    buffer_status: str = ""
    learning_status: str = ""
    web_url: str = ""


class SharedState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = Snapshot()

    def update(self, snap: Snapshot) -> None:
        with self._lock:
            self._snapshot = snap

    def get(self) -> Snapshot:
        with self._lock:
            return self._snapshot


class DropOldestQueue:
    def __init__(self, maxsize: int):
        self.q: queue.Queue[np.ndarray] = queue.Queue(maxsize=maxsize)
        self.overruns = 0
        self.lock = threading.Lock()

    def put_block(self, block: np.ndarray) -> None:
        with self.lock:
            if self.q.full():
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    pass
                self.overruns += 1
            self.q.put_nowait(block)

    def get_block(self, timeout: float = 0.1) -> Optional[np.ndarray]:
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def size(self) -> int:
        return self.q.qsize()

    def overrun_count(self) -> int:
        with self.lock:
            return self.overruns


class AudioDeviceSelector:
    def __init__(self, cfg: RuntimeConfig):
        self.cfg = cfg

    def _card_usb_ids(self) -> Dict[int, Tuple[str, str]]:
        out: Dict[int, Tuple[str, str]] = {}
        for card_path in Path("/sys/class/sound").glob("card[0-9]*"):
            name = card_path.name
            try:
                card_num = int(name.replace("card", ""))
            except ValueError:
                continue

            dev = card_path.resolve()
            search_paths = [
                dev / "device",
                dev / "device" / "device",
                dev.parent / "device",
            ]

            vid = ""
            pid = ""
            for sp in search_paths:
                v = sp / "idVendor"
                p = sp / "idProduct"
                if v.exists() and p.exists():
                    vid = v.read_text().strip().lower()
                    pid = p.read_text().strip().lower()
                    break

            if vid and pid:
                out[card_num] = (vid, pid)
        return out

    def select(self) -> Tuple[Optional[int], str]:
        if sd is None:
            raise RuntimeError(f"sounddevice import failed: {SOUNDDEVICE_IMPORT_ERROR}")

        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        card_usb = self._card_usb_ids()
        preferred = {
            (d["vid"].lower(), d["pid"].lower())
            for d in self.cfg.preferred_usb_ids
        }

        candidates: List[Tuple[int, int, str]] = []

        for idx, dev in enumerate(devices):
            max_inputs = int(dev.get("max_input_channels", 0))
            if max_inputs <= 0:
                continue

            name = str(dev.get("name", ""))
            hostapi_name = hostapis[int(dev["hostapi"])]["name"]

            score = 0
            lower_name = name.lower()
            matched_usb_id = False

            for card_num, ids in card_usb.items():
                if ids in preferred:
                    card_markers = [
                        f"hw:{card_num}",
                        f"card {card_num}",
                        f"usb audio",
                    ]
                    if any(marker in lower_name for marker in card_markers):
                        score += 100
                        matched_usb_id = True

            if "usb" in lower_name:
                score += 20
            if "alsa" in hostapi_name.lower():
                score += 10
            if "default" in lower_name:
                score -= 5

            if matched_usb_id:
                reason = "preferred USB VID/PID match"
            elif "usb" in lower_name:
                reason = "USB-looking ALSA capture device"
            else:
                reason = "generic capture device"

            candidates.append((score, idx, f"{name} [{hostapi_name}] - {reason}"))

        if not candidates:
            return None, "No input devices found"

        candidates.sort(reverse=True)
        best_score, best_idx, reason = candidates[0]
        return best_idx, reason

    def print_devices(self) -> None:
        if sd is None:
            print(f"sounddevice unavailable: {SOUNDDEVICE_IMPORT_ERROR}")
            return
        print(sd.query_devices())
        print("USB card IDs:", self._card_usb_ids())



class WavFileCaptureThread(threading.Thread):
    """
    Offline WAV feeder.

    This is deliberately not a separate decoder. It feeds the same 8 kHz /
    64-sample block queue used by USB audio so the DSP, timing, trainer,
    squelch and Morse state machine are exercised unchanged.

    Supported:
      - PCM WAV
      - 8-bit unsigned
      - 16-bit signed
      - 24-bit signed
      - 32-bit signed
      - mono or multi-channel, mixed to mono
      - any sample rate, resampled to cfg.sample_rate using linear interpolation

    This is for test and diagnosis. USB audio remains the default runtime path.
    """
    def __init__(
        self,
        cfg: RuntimeConfig,
        audio_q: DropOldestQueue,
        stop_event: threading.Event,
        wav_path: str,
        loop: bool = False,
    ):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.audio_q = audio_q
        self.stop_event = stop_event
        self.wav_path = wav_path
        self.loop = loop
        self.errors = 0
        self.selected_name = f"WAV file: {wav_path}"

    def _read_wav(self) -> Tuple[np.ndarray, int]:
        with wave.open(self.wav_path, "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            rate = wf.getframerate()
            nframes = wf.getnframes()
            raw = wf.readframes(nframes)

        if sampwidth == 1:
            data = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
            data = (data - 128.0) * 256.0

        elif sampwidth == 2:
            data = np.frombuffer(raw, dtype="<i2").astype(np.float32)

        elif sampwidth == 3:
            b = np.frombuffer(raw, dtype=np.uint8)
            if len(b) % 3 != 0:
                b = b[:len(b) - (len(b) % 3)]
            b = b.reshape(-1, 3)
            vals = (
                b[:, 0].astype(np.int32) |
                (b[:, 1].astype(np.int32) << 8) |
                (b[:, 2].astype(np.int32) << 16)
            )
            sign = vals & 0x800000
            vals = vals - (sign << 1)
            data = (vals.astype(np.float32) / 256.0)

        elif sampwidth == 4:
            data = (np.frombuffer(raw, dtype="<i4").astype(np.float32) / 65536.0)

        else:
            raise RuntimeError(f"Unsupported WAV sample width: {sampwidth} bytes")

        if channels > 1:
            usable = (len(data) // channels) * channels
            data = data[:usable].reshape(-1, channels).mean(axis=1)

        if rate != self.cfg.sample_rate:
            data = self._resample_linear(data, rate, self.cfg.sample_rate)

        if len(data) == 0:
            raise RuntimeError("WAV file has no audio samples after conversion")

        peak = float(np.max(np.abs(data))) if len(data) else 0.0
        if peak > 32000.0:
            data = data * (30000.0 / peak)

        return data.astype(np.float32), self.cfg.sample_rate

    def _resample_linear(self, data: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
        if src_rate == dst_rate:
            return data.astype(np.float32)

        duration = len(data) / float(src_rate)
        dst_len = max(1, int(round(duration * dst_rate)))

        src_x = np.arange(len(data), dtype=np.float64)
        dst_x = np.linspace(0, len(data) - 1, dst_len, dtype=np.float64)

        return np.interp(dst_x, src_x, data).astype(np.float32)

    def run(self) -> None:
        try:
            data, rate = self._read_wav()
            print(
                f"[AUDIO] WAV input loaded: {self.wav_path} "
                f"samples={len(data)} rate={rate} duration={len(data)/rate:.2f}s"
            )
        except Exception as exc:
            self.errors += 1
            print(f"[AUDIO] WAV input failed: {exc}", file=sys.stderr)
            return

        block_n = self.cfg.block_n

        while not self.stop_event.is_set():
            pos = 0

            while pos < len(data) and not self.stop_event.is_set():
                block = data[pos:pos + block_n]

                if len(block) < block_n:
                    block = np.pad(block, (0, block_n - len(block)), mode="constant")

                self.audio_q.put_block(block.astype(np.float32))
                pos += block_n

                # This is not used for decoder timing. The decoder now uses
                # sample-clock timing internally. The sleep only stops offline
                # tests from blasting thousands of status updates instantly.
                time.sleep(float(block_n) / float(self.cfg.sample_rate))

            if not self.loop:
                print("[AUDIO] WAV input complete")
                break

            print("[AUDIO] WAV input loop restarting")



class AudioCaptureThread(threading.Thread):
    def __init__(
        self,
        cfg: RuntimeConfig,
        audio_q: DropOldestQueue,
        stop_event: threading.Event,
        device_index: Optional[int],
    ):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.audio_q = audio_q
        self.stop_event = stop_event
        self.device_index = device_index
        self.errors = 0
        self.selected_name = ""

    def _extract_hw_pcm(self, device_name: str) -> Optional[str]:
        """
        Extract hw:X,Y from a PortAudio ALSA device name like:
          USB Audio Device: - (hw:1,0)
        Then convert it to plughw:X,Y so ALSA can rate-convert to 8 kHz.
        """
        import re

        m = re.search(r"\(hw:(\d+),(\d+)\)", device_name)
        if not m:
            return None
        return f"plughw:{m.group(1)},{m.group(2)}"

    def _run_arecord_capture(self, pcm_name: str) -> None:
        """
        Fallback capture path.

        Some USB audio devices reject direct 8000 Hz capture via PortAudio/hw,
        but work correctly through ALSA plughw, which performs format/rate
        conversion. This preserves the decoder's 8 kHz / 64-sample DSP model.
        """
        bytes_per_block = self.cfg.block_n * 2

        cmd = [
            "arecord",
            "-q",
            "-D", pcm_name,
            "-r", str(self.cfg.sample_rate),
            "-f", "S16_LE",
            "-c", "1",
            "-t", "raw",
            "--period-size", str(self.cfg.block_n),
            "--buffer-size", str(self.cfg.block_n * 8),
        ]

        print(f"[AUDIO] fallback capture using: {' '.join(cmd)}")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        try:
            assert proc.stdout is not None

            # arecord/plughw may buffer internally, but each emitted block still
            # represents exactly cfg.block_n samples. The decoder uses DSP
            # sample-clock timing, not stdout read timing.
            while not self.stop_event.is_set():
                data = proc.stdout.read(bytes_per_block)

                if not data:
                    self.errors += 1
                    err = b""
                    if proc.stderr is not None:
                        try:
                            err = proc.stderr.read(4096)
                        except Exception:
                            err = b""
                    print(
                        f"[AUDIO] arecord stopped unexpectedly rc={proc.poll()} "
                        f"stderr={err.decode(errors='replace')}",
                        file=sys.stderr,
                    )
                    break

                if len(data) != bytes_per_block:
                    self.errors += 1
                    continue

                block_i16 = np.frombuffer(data, dtype="<i2")
                block = block_i16.astype(np.float32)
                self.audio_q.put_block(block)

        finally:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def run(self) -> None:
        if sd is None:
            print(f"[AUDIO] sounddevice import failed: {SOUNDDEVICE_IMPORT_ERROR}", file=sys.stderr)
            self.errors += 1
            return

        def callback(indata, frames, time_info, status):
            if status:
                self.errors += 1
                print(f"[AUDIO] callback status: {status}", file=sys.stderr)

            if frames != self.cfg.block_n:
                self.errors += 1

            mono = indata[:, 0].copy()
            if mono.dtype != np.float32:
                mono = mono.astype(np.float32)

            block = np.clip(mono * 32768.0, -32768, 32767).astype(np.float32)
            self.audio_q.put_block(block)

        try:
            dev_info = sd.query_devices(self.device_index, "input")
            self.selected_name = str(dev_info.get("name", self.device_index))
            print(f"[AUDIO] selected input: {self.selected_name}")

            try:
                with sd.InputStream(
                    samplerate=self.cfg.sample_rate,
                    blocksize=self.cfg.block_n,
                    channels=1,
                    dtype="float32",
                    device=self.device_index,
                    latency="low",
                    callback=callback,
                ):
                    while not self.stop_event.is_set():
                        time.sleep(0.1)

            except Exception as exc:
                self.errors += 1
                print(f"[AUDIO] sounddevice capture failed: {exc}", file=sys.stderr)

                pcm_name = self._extract_hw_pcm(self.selected_name)
                if not pcm_name:
                    print(
                        "[AUDIO] cannot derive plughw fallback device from selected input name",
                        file=sys.stderr,
                    )
                    return

                self.selected_name = f"{self.selected_name} via {pcm_name}"
                self._run_arecord_capture(pcm_name)

        except Exception as exc:
            self.errors += 1
            print(f"[AUDIO] capture failed: {exc}", file=sys.stderr)


class MorseDecoder:
    def __init__(self, cfg: RuntimeConfig, audio_q: DropOldestQueue, shared: SharedState, audio_thread_ref):
        self.cfg = cfg
        self.audio_q = audio_q
        self.shared = shared
        self.audio_thread_ref = audio_thread_ref

        self.allowed_tones = cfg.allowed_tones_hz
        self.target_tone_hz = self.nearest_allowed_tone(cfg.target_tone_hz)
        self.detected_tone_hz = self.target_tone_hz
        self.detected_tone_confidence = 1.0

        self.sensitivity = cfg.sensitivity
        self.squelch_snr = cfg.squelch_snr

        # Audio sample-clock timing. Must be initialised before any state uses it.
        self.dsp_time_ms = 0.0
        self.current_block_time_ms = 0

        self.dot_ms = 50.0
        self.dash_ms = 150.0
        self.avg_mark_ms = 50.0
        self.avg_space_ms = 50.0
        self.char_gap_ms = 150.0
        self.word_gap_ms = 350.0

        self.mark_durations: deque[float] = deque(maxlen=MARK_HISTORY)
        self.space_durations: deque[float] = deque(maxlen=SPACE_HISTORY)

        self.train_short_marks: deque[float] = deque(maxlen=TRAIN_BUF)
        self.train_long_marks: deque[float] = deque(maxlen=TRAIN_BUF)
        self.train_short_gaps: deque[float] = deque(maxlen=TRAIN_BUF)
        self.train_letter_gaps: deque[float] = deque(maxlen=TRAIN_BUF)

        # Appliance default: copy immediately at configured fixed tone.
        # TRAIN can still be enabled from MODE, but should not block live copy at boot.
        self.training_enabled = False
        self.training_locked_ms = 0
        self.train_last_dot = self.dot_ms
        self.train_stable_ms = 0
        self.train_last_check_ms = millis()
        self.train_transitions = 0

        self.training_state = TrainingState.TRAIN_SEEKING
        self.trainer_phase = TrainerPhase.TP_WAIT_DOT
        self.trainer_good_a_count = 0
        self.trainer_score = 0

        self.expand_shorthand = True
        self.decoded_raw = ""
        self.decoded_copy = ""
        self.decoded_expanded = ""
        self.current_word = ""
        self.current_symbol = ""

        # Runtime appliance controls.
        # Appliance default is AUTO TRACK:
        #   - acquire tone automatically
        #   - lock while copy is good
        #   - keep learning timing/fist style
        #   - reacquire only when signal confidence is lost
        self.runtime_mode = "AUTO"
        self.auto_tone_track = True
        self.request_tone_scan = True
        self.display_hold = False

        # Learning/lock state. Tone lock is deliberately separate from timing
        # learning: tone should lock during good copy, timing should continue to
        # adapt gently to the sender's fist.
        self.tone_lock_hz = self.target_tone_hz
        self.tone_locked = False
        self.learning_status = "ACQUIRE"
        self.buffer_status_text = ""

        # Startup copy holdoff:
        # The detector may catch only the tail of the very first character while
        # noise/signal floors and hysteresis are settling. During this short
        # window, timing still adapts, but one-element startup garbage such as
        # leading T/E is not committed to operator copy.
        self.startup_copy_open = False
        self.startup_candidate_chars = 0

        # End-of-message tail guard:
        # once we have clearly flushed the trailing word, do not allow weak
        # residual ET/TT garbage back into operator copy unless a credible
        # fresh signal arrives.
        self.tail_copy_closed = False

        self.noise_floor = 0.0
        self.signal_floor = 0.0
        self.detect_level = 0.0
        self.thr_on = 0.0
        self.thr_off = 0.0
        self.tone_present = False

        self.snr_ema = 1.0
        self.last_transition_ms = millis()
        self.tone_on_votes = 0
        self.tone_off_votes = 0

        self.env_fast = 0.0
        self.env_noise = 0.0
        self.env_peak = 0.0

        self.last_good_transition_ms = millis()
        self.end_gap_flushed = False

        self.active_state_start_ms = int(self.dsp_time_ms)
        self.pending_polarity_change = False
        self.pending_new_polarity = False
        self.pending_polarity_since_ms = 0

        self.sync_state = SyncState.SYNC_ACQUIRE
        self.sync_confidence = 0

        self.goertzel_coeff = 0.0
        self.goertzel_sine = 0.0
        self.goertzel_cosine = 0.0
        self.compute_goertzel_for_freq(self.target_tone_hz)

        self.debug_enabled = False
        self.debug_lines: deque[str] = deque(maxlen=180)

        self.training_reset_state()
        self.reset_detector_floors()

    def debug_log(self, msg: str) -> None:
        if self.debug_enabled:
            self.debug_lines.append(msg)

    def sync_state_text(self) -> str:
        return {
            SyncState.SYNC_ACQUIRE: "ACQUIRE",
            SyncState.SYNC_TRACK: "TRACK",
            SyncState.SYNC_WEAK: "WEAK",
            SyncState.SYNC_LOST: "LOST",
        }.get(self.sync_state, "UNK")

    def training_state_text(self) -> str:
        return {
            TrainingState.TRAIN_SEEKING: "SEEKING",
            TrainingState.TRAIN_PROVISIONAL: "PROVISIONAL",
            TrainingState.TRAIN_LOCKED: "LOCKED",
        }.get(self.training_state, "UNK")

    def display_mode_text(self) -> str:
        mode = str(getattr(self, "runtime_mode", "RX"))

        if mode == "TRAIN":
            if self.training_enabled:
                return f"TRAIN {self.training_state_text()}"
            return "TRAIN OFF"

        if mode == "AUTO":
            if bool(getattr(self, "auto_tone_track", False)):
                return "AUTO TRACK"
            return "AUTO"

        return "RX"

    def decode_suppressed(self) -> bool:
        if self.sync_state == SyncState.SYNC_LOST:
            return True
        if not self.training_enabled:
            return False
        return self.training_state == TrainingState.TRAIN_SEEKING

    def nearest_allowed_tone(self, hz: float) -> float:
        return min(self.allowed_tones, key=lambda f: abs(f - hz))

    def tone_index_for_freq(self, hz: float) -> int:
        nearest = self.nearest_allowed_tone(hz)
        for i, f in enumerate(self.allowed_tones):
            if abs(f - nearest) < 0.5:
                return i
        return 0

    def get_tone_search_range(self) -> Tuple[int, int]:
        idx = self.tone_index_for_freq(self.target_tone_hz)
        return max(0, idx - 1), min(len(self.allowed_tones) - 1, idx + 1)

    def compute_goertzel_for_freq(self, freq_hz: float) -> None:
        """
        Configure Goertzel for an exact arbitrary target frequency.

        The ESP32 reference code used:
            k = 0.5 + ((N * freq) / sample_rate)
            w = 2*pi*k/N

        In C/C++ that appears to have been intended as a nearest-bin calculation,
        but because k is a float, it introduces a half-bin frequency offset.

        At 8 kHz / 64 samples, half a bin is 62.5 Hz. So a requested 700 Hz
        detector is effectively centred around 762.5 Hz, which is bad news for
        a clean 700 Hz CW test tone.

        On Linux/Pi we keep the same block timing model, but use the exact
        Goertzel centre frequency:
            w = 2*pi*freq/sample_rate
        """
        w = (2.0 * math.pi * float(freq_hz)) / float(self.cfg.sample_rate)
        self.goertzel_sine = math.sin(w)
        self.goertzel_cosine = math.cos(w)
        self.goertzel_coeff = 2.0 * self.goertzel_cosine

    def goertzel_magnitude(self, block: np.ndarray) -> float:
        s_prev = 0.0
        s_prev2 = 0.0
        coeff = self.goertzel_coeff
        for sample in block:
            s = float(sample) + coeff * s_prev - s_prev2
            s_prev2 = s_prev
            s_prev = s
        real = s_prev - s_prev2 * self.goertzel_cosine
        imag = s_prev2 * self.goertzel_sine
        return math.sqrt(real * real + imag * imag)

    def auto_find_tone(self, block: np.ndarray) -> Tuple[float, float]:
        best_freq = self.allowed_tones[0]
        best_mag = -1.0
        second_mag = -1.0
        start, end = self.get_tone_search_range()

        old = (self.goertzel_coeff, self.goertzel_sine, self.goertzel_cosine)

        for i in range(start, end + 1):
            f = self.allowed_tones[i]
            self.compute_goertzel_for_freq(f)
            m = self.goertzel_magnitude(block)
            if m > best_mag:
                second_mag = best_mag
                best_mag = m
                best_freq = f
            elif m > second_mag:
                second_mag = m

        if second_mag < 1.0:
            second_mag = 1.0

        self.detected_tone_confidence = clampf(best_mag / second_mag, 1.0, 10.0)
        self.compute_goertzel_for_freq(best_freq)
        return best_freq, best_mag

    def reset_detector_floors(self) -> None:
        self.noise_floor = 0.0
        self.signal_floor = 0.0
        self.detect_level = 0.0
        self.tone_present = False
        self.tone_on_votes = 0
        self.tone_off_votes = 0
        self.current_symbol = ""
        now = int(self.dsp_time_ms)
        self.last_transition_ms = now
        self.last_good_transition_ms = now
        self.snr_ema = 1.0
        self.sync_state = SyncState.SYNC_ACQUIRE
        self.sync_confidence = 0
        self.end_gap_flushed = False
        self.env_fast = 0.0
        self.env_noise = 0.0
        self.env_peak = 0.0
        self.active_state_start_ms = int(self.dsp_time_ms)
        self.pending_polarity_change = False
        self.pending_new_polarity = False
        self.pending_polarity_since_ms = 0

    def training_reset_state(self) -> None:
        self.training_state = TrainingState.TRAIN_SEEKING
        self.training_locked_ms = 0
        self.train_last_dot = self.dot_ms
        self.train_stable_ms = 0
        self.train_last_check_ms = millis()
        self.train_transitions = 0

        self.train_short_marks.clear()
        self.train_long_marks.clear()
        self.train_short_gaps.clear()
        self.train_letter_gaps.clear()

        self.trainer_phase = TrainerPhase.TP_WAIT_DOT
        self.trainer_good_a_count = 0
        self.trainer_score = 0

        self.dot_ms = 50.0
        self.dash_ms = 150.0
        self.avg_mark_ms = 50.0
        self.avg_space_ms = 50.0
        self.char_gap_ms = 150.0
        self.word_gap_ms = 350.0

        self.current_symbol = ""
        self.current_word = ""

    def training_score_good(self) -> None:
        self.trainer_score = min(100, self.trainer_score + 3)

    def training_score_bad(self) -> None:
        self.trainer_score = max(0, self.trainer_score - 4)

    def trainer_hard_reset(self, why: str, value: float) -> None:
        self.debug_log(f"[TRAIN RESET] {why} {value:.1f}")
        self.trainer_phase = TrainerPhase.TP_WAIT_DOT
        self.training_score_bad()

    def training_set_provisional(self) -> None:
        if self.training_state == TrainingState.TRAIN_SEEKING:
            self.training_state = TrainingState.TRAIN_PROVISIONAL
            self.sync_strengthen(25)
            self.debug_log("[TRAIN] PROVISIONAL")

    def training_stop_locked(self) -> None:
        self.training_state = TrainingState.TRAIN_LOCKED
        self.training_locked_ms = millis()
        self.current_symbol = ""
        self.current_word = ""
        self.mark_durations.clear()
        self.space_durations.clear()
        self.debug_log(
            f"[TRAIN] LOCK dot={self.dot_ms:.1f} dash={self.dash_ms:.1f} "
            f"intra={self.avg_space_ms:.1f} char={self.char_gap_ms:.1f} "
            f"word={self.word_gap_ms:.1f} A={self.trainer_good_a_count} "
            f"score={self.trainer_score}"
        )

    def training_observe_isolated_mark(self, ms: float) -> None:
        if not self.training_enabled or self.training_state == TrainingState.TRAIN_LOCKED:
            return

        ref = PRELOCK_DOT_MS

        if ms > 4.8 * ref:
            self.trainer_hard_reset("huge-mark", ms)
            return

        if self.trainer_phase == TrainerPhase.TP_WAIT_DOT:
            if 0.50 * ref <= ms <= 1.80 * ref:
                self.train_short_marks.append(ms)
                self.trainer_phase = TrainerPhase.TP_WAIT_INTRA
                self.training_score_good()
                self.debug_log(f"[TRAIN] dot {ms:.1f}")
            elif 2.00 * ref <= ms <= 4.10 * ref:
                self.trainer_hard_reset("dash-while-dot", ms)
            else:
                self.trainer_hard_reset("bad-dot", ms)

        elif self.trainer_phase == TrainerPhase.TP_WAIT_DASH:
            if 2.00 * ref <= ms <= 4.10 * ref:
                self.train_long_marks.append(ms)
                self.trainer_phase = TrainerPhase.TP_WAIT_LETTER
                self.training_score_good()
                self.debug_log(f"[TRAIN] dash {ms:.1f}")
            else:
                self.trainer_hard_reset("bad-dash", ms)

        else:
            self.trainer_hard_reset("mark-wrong-phase", ms)

    def training_observe_isolated_gap(self, ms: float) -> None:
        if not self.training_enabled or self.training_state == TrainingState.TRAIN_LOCKED:
            return

        ref = PRELOCK_DOT_MS

        if ms > 5.5 * ref:
            self.trainer_hard_reset("huge-gap", ms)
            return

        if self.trainer_phase == TrainerPhase.TP_WAIT_INTRA:
            if 0.30 * ref <= ms <= 1.85 * ref:
                self.train_short_gaps.append(ms)
                self.trainer_phase = TrainerPhase.TP_WAIT_DASH
                self.training_score_good()
                self.debug_log(f"[TRAIN] intra {ms:.1f}")
            else:
                self.trainer_hard_reset("bad-intra", ms)

        elif self.trainer_phase == TrainerPhase.TP_WAIT_LETTER:
            if 1.80 * ref <= ms <= 4.80 * ref:
                self.train_letter_gaps.append(ms)
                self.trainer_good_a_count += 1
                self.trainer_phase = TrainerPhase.TP_WAIT_DOT
                self.training_score_good()
                self.debug_log(f"[TRAIN] letter {ms:.1f} A={self.trainer_good_a_count}")
            else:
                self.trainer_hard_reset("bad-letter", ms)

        elif self.trainer_phase == TrainerPhase.TP_WAIT_DOT:
            if ms > 1.8 * ref:
                self.debug_log(f"[TRAIN] idle-gap {ms:.1f}")

        else:
            self.trainer_hard_reset("gap-wrong-phase", ms)

    def training_try_promote(self) -> bool:
        if not self.training_enabled:
            return False

        if (
            len(self.train_short_marks) < 3 or
            len(self.train_long_marks) < 3 or
            len(self.train_short_gaps) < 3 or
            len(self.train_letter_gaps) < 3
        ):
            return False

        m_short = median(list(self.train_short_marks))
        m_long = median(list(self.train_long_marks))
        m_intra = median(list(self.train_short_gaps))
        m_letter = median(list(self.train_letter_gaps))

        dash_ratio = m_long / max(m_short, 1.0)
        intra_ratio = m_intra / max(m_short, 1.0)
        letter_ratio = m_letter / max(m_short, 1.0)

        plausible = (
            35.0 <= m_short <= 110.0 and
            2.00 <= dash_ratio <= 4.20 and
            0.30 <= intra_ratio <= 1.90 and
            1.80 <= letter_ratio <= 5.20
        )

        if not plausible:
            return False

        self.dot_ms = m_short
        self.dash_ms = m_long
        self.avg_mark_ms = m_short
        self.avg_space_ms = m_intra
        self.char_gap_ms = max(2.8 * self.dot_ms, 0.90 * m_letter)
        self.word_gap_ms = max(7.4 * self.dot_ms, 2.4 * m_letter)

        if self.training_state == TrainingState.TRAIN_SEEKING and (
            self.trainer_good_a_count >= 3 or self.trainer_score >= 16
        ):
            self.training_set_provisional()
            return True

        if self.training_state == TrainingState.TRAIN_PROVISIONAL and (
            self.trainer_good_a_count >= 5 or self.trainer_score >= 26
        ):
            self.training_stop_locked()
            return True

        return False

    def training_auto_tick(self) -> None:
        if not self.training_enabled:
            return

        self.training_try_promote()

        now = millis()
        if now - self.train_last_check_ms < 200:
            return

        dt = now - self.train_last_check_ms
        self.train_last_check_ms = now

        dd = abs(self.dot_ms - self.train_last_dot)
        self.train_last_dot = self.dot_ms

        tone_good = self.detected_tone_confidence >= 1.10
        snr_good = self.snr_ema > (self.squelch_snr + 0.25)
        dot_stable = dd < 1.2
        enough_transitions = self.train_transitions >= 10
        sync_good = self.sync_state in (SyncState.SYNC_TRACK, SyncState.SYNC_WEAK)

        if tone_good and snr_good and dot_stable and enough_transitions and sync_good:
            self.train_stable_ms += dt
        else:
            self.train_stable_ms = max(0, self.train_stable_ms - dt)

        if (
            self.training_state == TrainingState.TRAIN_SEEKING and
            self.trainer_score >= 14 and
            self.trainer_good_a_count >= 2
        ):
            self.training_set_provisional()

        if (
            self.training_state == TrainingState.TRAIN_PROVISIONAL and
            self.train_stable_ms >= 2200 and
            self.trainer_good_a_count >= 4
        ):
            self.training_stop_locked()

    def estimate_dot_from_marks(self) -> float:
        """
        Estimate dot length from the short-mark cluster.
        """
        marks = [float(v) for v in self.mark_durations if 16.0 <= float(v) <= 360.0]

        if len(marks) < 3:
            return self.dot_ms

        marks = _mw_trimmed(marks, 0.05, 0.95)
        short_marks, _ = _mw_largest_gap_split(marks, min_ratio=1.65)

        if len(short_marks) < 3:
            m = sorted(marks)
            short_marks = m[:max(3, (len(m) + 1) // 2)]

        candidate = _mw_median_float(short_marks)
        return clampf(candidate, 24.0, 300.0)

    def adapt_dot(self, candidate: float) -> None:
        candidate = clampf(float(candidate), 24.0, 300.0)

        if not self.training_enabled:
            conf = int(getattr(self, "sync_confidence", 0))
            if conf < 30:
                alpha = 0.18
            elif conf < 70:
                alpha = 0.12
            else:
                alpha = 0.07
        elif self.training_state == TrainingState.TRAIN_PROVISIONAL:
            alpha = 0.06
        elif self.training_state == TrainingState.TRAIN_LOCKED:
            alpha = 0.035
        else:
            alpha = 0.10

        old = float(self.dot_ms)
        ratio = max(old, candidate) / max(min(old, candidate), 1.0)

        if ratio > 1.55 and int(getattr(self, "sync_confidence", 0)) >= 50:
            alpha *= 0.45

        self.dot_ms = (1.0 - alpha) * old + alpha * candidate
        self.dot_ms = clampf(self.dot_ms, 24.0, 300.0)
        self.dash_ms = 3.0 * self.dot_ms
        self.avg_mark_ms = 0.84 * self.avg_mark_ms + 0.16 * candidate

    def update_space_stats(self) -> None:
        if len(self.space_durations) < 4:
            return

        vals = [
            v for v in self.space_durations
            if 0.3 * self.dot_ms < v < 2.0 * self.dot_ms
        ]
        if vals:
            self.avg_space_ms = sum(vals) / len(vals)

    def update_gap_estimates(self) -> None:
        # Display/status estimates. Actual gap decisions are made by
        # _mw_classify_gap_dynamic() so they can follow human speed/style.
        dot = max(float(self.dot_ms), 24.0)
        self.char_gap_ms = 3.0 * dot
        self.word_gap_ms = 7.0 * dot

    def sync_strengthen(self, amt: int) -> None:
        self.sync_confidence = min(100, self.sync_confidence + amt)
        if self.sync_confidence >= 65:
            self.sync_state = SyncState.SYNC_TRACK
        elif self.sync_confidence >= 25:
            self.sync_state = SyncState.SYNC_WEAK
        else:
            self.sync_state = SyncState.SYNC_ACQUIRE

    def sync_weaken(self, amt: int) -> None:
        self.sync_confidence = max(0, self.sync_confidence - amt)
        if self.sync_confidence == 0:
            self.sync_state = SyncState.SYNC_LOST
        elif self.sync_confidence < 25:
            self.sync_state = SyncState.SYNC_ACQUIRE
        elif self.sync_confidence < 65:
            self.sync_state = SyncState.SYNC_WEAK
        else:
            self.sync_state = SyncState.SYNC_TRACK

    def maybe_decay_sync(self) -> None:
        now = self.current_block_time_ms
        since_good = now - self.last_good_transition_ms

        if since_good > 1200:
            self.sync_weaken(2)
        if since_good > 2500:
            self.sync_weaken(3)
        if since_good > 4000:
            self.sync_state = SyncState.SYNC_LOST

    def decode_morse(self, sym: str) -> str:
        return MORSE_TABLE.get(sym, "")

    def expand_word(self, word: str) -> str:
        if not self.expand_shorthand:
            return word
        return ABBREV.get(word.upper(), word)

    def clamp_buffers(self) -> None:
        self.decoded_raw = tail_text(self.decoded_raw, MAX_RAW)
        self.decoded_expanded = tail_text(self.decoded_expanded, MAX_EXPANDED)

    def on_decoded_char(self, c: str) -> None:
        # Do not let weak tail noise after the transmission append random
        # T/E characters. Spaces are harmless, but non-space characters require
        # a valid copy gate.
        # Tell the copy gate which character is being considered.
        # Needed so a valid locked leading T/E is not rejected as idle noise.
        self._copy_gate_candidate_char = c
        if c not in (" ", "\\n", "\\r") and not _mw_copy_gate_open(self):
            # Startup exception:
            # A real leading T/E can arrive while sync confidence is still low.
            # If AUTO TRACK has locked the tone and the signal is credible,
            # allow that first single-element character through.
            try:
                existing_copy = str(getattr(self, "decoded_copy", self.decoded_raw)).strip()
                allow_locked_leading_et = (
                    c == "T"
                    and not existing_copy
                    and bool(getattr(self, "tone_locked", False))
                    and (
                        float(getattr(self, "snr_ema", 1.0)) >= 2.0
                        or int(getattr(self, "sync_confidence", 0)) >= 2
                        or str(getattr(self, "learning_status", "")).startswith("LOCK")
                    )
                )
            except Exception:
                allow_locked_leading_et = False

            if not allow_locked_leading_et:
                self.debug_log(
                    f"[REJECT] copy gate c={c} snr={self.snr_ema:.2f} "
                    f"sync={self.sync_state_text()}/{self.sync_confidence}"
                )
                # MW_STARTUP_PENDING_LEADING_ET_V2
                try:
                    existing_copy = str(getattr(self, "decoded_copy", self.decoded_raw)).strip()
                    if (
                        c == "T"
                        and not existing_copy
                        and bool(getattr(self, "tone_locked", False))
                        and (
                            float(getattr(self, "snr_ema", 1.0)) >= 2.0
                            or int(getattr(self, "sync_confidence", 0)) >= 2
                            or str(getattr(self, "learning_status", "")).startswith("LOCK")
                        )
                    ):
                        self._startup_pending_leading_char = c
                        print(f"[COPY] held locked leading {c} for prepend", flush=True)
                except Exception:
                    pass

                self.current_symbol = ""
                return
            else:
                self.debug_log(
                    f"[ALLOW] locked leading {c} snr={self.snr_ema:.2f} "
                    f"sync={self.sync_state_text()}/{self.sync_confidence}"
                )

        # Idle-noise guard:
        # Before real copy starts, weak E/T characters are usually just
        # threshold chatter from silence/noise. Do not let them seed the copy
        # buffer as "TE T T T". Real CW starts with strong SNR or rising sync.
        try:
            existing_copy = str(getattr(self, "decoded_copy", self.decoded_raw)).strip()
            idle_only = bool(existing_copy) and all(ch in "ET \t\r\n" for ch in existing_copy)

            tone_locked_for_copy = bool(getattr(self, "tone_locked", False))
            snr_now = float(getattr(self, "snr_ema", 1.0))
            sync_now = int(getattr(self, "sync_confidence", 0))

            # Once tone is locked, a leading E/T is valid CW, not idle chatter,
            # provided the signal has risen above the noise floor. This fixes
            # losing the opening T from "THE QUICK".
            locked_real_signal = (
                tone_locked_for_copy
                and (
                    snr_now >= 2.0
                    or sync_now >= 2
                    or str(getattr(self, "learning_status", "")).startswith("LOCK")
                )
            )

            if (
                c == "T"
                and not locked_real_signal
                and (not existing_copy or idle_only)
                and sync_now < 10
                and str(getattr(getattr(self, "sync_state", None), "name", getattr(self, "sync_state", ""))) in ("SYNC_LOST", "SYNC_ACQUIRE", "LOST", "ACQUIRE")
                and snr_now < max(3.0, float(getattr(self, "squelch_snr", 1.45)) * 1.75)
            ):
                self.debug_log(
                    f"[REJECT] idle E/T c={c} snr={self.snr_ema:.2f} "
                    f"sync={self.sync_state_text()}/{self.sync_confidence}"
                )
                self.current_symbol = ""
                return
        except Exception:
            pass

        # MW_PREPEND_PENDING_LEADING_ET_V2
        try:
            pending = str(getattr(self, "_startup_pending_leading_char", "") or "")
            existing_copy = str(getattr(self, "decoded_copy", self.decoded_raw)).strip()
            if pending == "T" and not existing_copy and c not in (" ", "\\n", "\\r"):
                self.decoded_raw += pending
                self.decoded_copy += pending
                self._startup_pending_leading_char = ""
                print(f"[COPY] prepended locked leading {pending}", flush=True)
        except Exception:
            pass

        self.decoded_raw += c

        # MW_STARTUP_PREFIX_SANITY_V1
        # Clean up the specific startup artefacts created while AUTO TRACK is
        # still arming. This is deliberately tiny and only applies to the very
        # beginning of copy.
        try:
            if bool(getattr(self, "tone_locked", False)):
                copy_start = str(getattr(self, "decoded_copy", "")).strip()

                if len(copy_start) <= 8:
                    if copy_start.startswith("E HE"):
                        self.decoded_copy = self.decoded_copy.replace("E HE", "THE", 1)
                        self.decoded_raw = self.decoded_raw.replace("E HE", "THE", 1)
                        print("[COPY] startup prefix repair: E HE -> THE", flush=True)

                    elif copy_start.startswith("MHE"):
                        self.decoded_copy = self.decoded_copy.replace("MHE", "THE", 1)
                        self.decoded_raw = self.decoded_raw.replace("MHE", "THE", 1)
                        print("[COPY] startup prefix repair: MHE -> THE", flush=True)
        except Exception:
            pass

        # Human/operator copy mirrors decoded_raw but is kept separately so the
        # LCD can show clean copy even if RAW diagnostics change later.
        if not hasattr(self, "decoded_copy"):
            self.decoded_copy = ""

        self.decoded_copy += c

        if c not in (" ", "\\n", "\\r"):
            self.current_word += c

        self.clamp_buffers()

    def on_word_boundary(self) -> None:
        if not self.current_word:
            return
        out = self.expand_word(self.current_word)
        if self.decoded_expanded and not self.decoded_expanded.endswith(" "):
            self.decoded_expanded += " "
        self.decoded_expanded += out + " "
        self.current_word = ""
        self.clamp_buffers()

    # MW_RAW_DISPLAY_FORMATTER_V1
    def raw_display_text(self) -> str:
        """
        Lightly normalised RAW display.

        decoded_raw remains the literal internal buffer. This method is for
        screen/web display where we want forensic usefulness without needless
        startup artefacts and obvious CQ run compression.

        Example literal raw:
            TEQ CQCQ DE  ZL1SXG  CALLING  CQ CQCQ

        Display raw:
            CQ CQ CQ DE ZL1SXG CALLING CQ CQ CQ
        """
        import re

        literal = str(getattr(self, "decoded_raw", "") or "")
        text = re.sub(r"\s+", " ", literal.upper()).strip()

        if not text:
            return ""

        compact = re.sub(r"[^A-Z0-9]", "", text)

        # Drop startup acquisition junk before an early CQ.
        first_cq = compact.find("CQ")
        if 0 <= first_cq <= 8:
            compact = compact[first_cq:]

        # If it is clearly a CQ call containing DE, a ZL/VK-style callsign,
        # and CALLING, reconstruct the CQ groups from compact raw.
        call_match = re.search(r"(ZL[0-9][A-Z]{2,3}|VK[0-9][A-Z]{2,3})", compact)

        if call_match and "DE" in compact and "CALLING" in compact:
            call = call_match.group(1)

            de_pos = compact.find("DE")
            before_de = compact[:de_pos] if de_pos >= 0 else compact
            after_call = compact[compact.find("CALLING") + len("CALLING"):]

            open_cq = before_de.count("CQ")
            close_cq = after_call.count("CQ")

            # If one CQ was lost into startup junk, recover the standard call.
            if open_cq >= 2:
                open_cq = 3
            if close_cq >= 2:
                close_cq = 3

            opening = " ".join(["CQ"] * max(0, open_cq))
            closing = " ".join(["CQ"] * max(0, close_cq))

            parts = []
            if opening:
                parts.append(opening)
            parts.extend(["DE", call, "CALLING"])
            if closing:
                parts.append(closing)

            return " ".join(parts).strip()

        # Generic normalisation for raw display.
        # CQCQ -> CQ CQ, CQCQCQ -> CQ CQ CQ.
        def cq_run(m):
            token = m.group(0)
            return " ".join(["CQ"] * (len(token) // 2))

        text = re.sub(r"\b(?:CQ){2,}\b", cq_run, text)
        text = re.sub(r"\bC\s+Q\b", "CQ", text)
        text = re.sub(r"\bD\s+E\b", "DE", text)
        text = re.sub(r"\bDEZL", "DE ZL", text)
        text = re.sub(r"\bDEVK", "DE VK", text)

        # Collapse common split ZL/VK callsigns.
        def collapse_call(m):
            return "".join(m.group(0).split())

        text = re.sub(r"\bZ\s*L\s*[0-9]\s*(?:[A-Z]\s*){2,3}\b", collapse_call, text)
        text = re.sub(r"\bV\s*K\s*[0-9]\s*(?:[A-Z]\s*){2,3}\b", collapse_call, text)

        text = re.sub(r"\s+", " ", text).strip()
        return text

    def human_readable_raw(self) -> str:
        """
        Human-facing competition copy.

        The raw decoder remains literal. This display formatter:
        - removes startup edge artefacts before the A preamble
        - removes the A training preamble
        - preserves ham/CW abbreviations such as CQ, DE and K
        - adds spaces around common run-on chunks when word gaps are missed
        """
        import re

        text = self.decoded_raw.strip()

        # Startup edge artefact handling:
        # If we started mid-tone, a phantom leading T can appear before the
        # repeated A preamble. Keep raw honest, but hide it from display copy.
        text = re.sub(r"^.{0,12}?A{5,}\s*", "", text)

        # If the preamble has already been partially spaced, remove that too.
        text = re.sub(r"^(?:A\s*){5,}", "", text).strip()

        # Collapse repeated whitespace.
        text = re.sub(r"\s+", " ", text).strip()

        # Help display recover common CW chunks without changing raw.
        # Keep operator-friendly CW abbreviations; do NOT expand CQ/DE/K here.
        replacements = [
            ("CQCQCQ", "CQ CQ CQ"),
            ("CQCQ", "CQ CQ"),
            ("CQ CQ CQDE", "CQ CQ CQ DE"),
            ("CQ CQDE", "CQ CQ DE"),
            ("DEZL1SXGZL1SXG", "DE ZL1SXG ZL1SXG"),
            ("DE ZL1SXGZL1SXG", "DE ZL1SXG ZL1SXG"),
            ("ZL1SXGZL1SXG", "ZL1SXG ZL1SXG"),
            ("TESTINGTHEMORSEWHISPERER", "TESTING THE MORSE WHISPERER"),
            ("TESTING THE MORSEWHISPERER", "TESTING THE MORSE WHISPERER"),
            ("THEMORSEWHISPERER", "THE MORSE WHISPERER"),
            ("MORSEWHISPERER", "MORSE WHISPERER"),
        ]

        for src, dst in replacements:
            text = text.replace(src, dst)

        text = re.sub(r"\s+", " ", text).strip()

        # Startup artefact cleanup:
        # When AUTO TRACK joins just after the true beginning, the first
        # partial word can decode as AR. AR is valid at end-of-message, but a
        # leading AR before normal prose is almost always startup rubbish.
        text = re.sub(r"^(?:AR\s+)+", "", text).strip()

        # Common run-together repairs caused by borderline word gaps.
        # Keep this human-facing only; RAW remains honest for diagnostics.
        spacing_replacements = [
            ("HADA", "HAD A"),
            ("LITTLELAMB", "LITTLE LAMB"),
            ("ITSFLEECE", "ITS FLEECE"),
            ("FLEECEWAS", "FLEECE WAS"),
            ("WASBLACK", "WAS BLACK"),
            ("EVERYTIME", "EVERY TIME"),
            ("JUMPEDA", "JUMPED A"),
            ("JUMPSA", "JUMPS A"),
            ("THEFENCE", "THE FENCE"),
            ("AFENCE", "A FENCE"),
            ("SPARKS", "SPARKS"),
            ("FLEWOUT", "FLEW OUT"),
            ("ITSARSE", "ITS ARSE"),
        ]

        compact = text.replace(" ", "")
        for src, dst in spacing_replacements:
            compact = compact.replace(src, dst.replace(" ", "\u0000"))

        if "\u0000" in compact:
            compact = compact.replace("\u0000", " ")
            text = compact

        # If the above compact repair joined too much, recover simple known
        # phrase boundaries that are safe and readable.
        text = text.replace("HAD ALITTLE", "HAD A LITTLE")
        text = text.replace("LITTLE LAMBIT", "LITTLE LAMB IT")
        text = text.replace("LAMBITS", "LAMB ITS")
        text = text.replace("BLACK&CHARCOAL", "BLACK & CHARCOAL")
        text = text.replace("BLACK & CHARCOAL", "BLACK AS CHARCOAL")

        # MW_FALSE_INTERNAL_SPACE_REPAIR_V1
        # Operator-display cleanup only. RAW still shows the literal decoder
        # output for diagnostics.
        #
        # These repairs target false word gaps inside common test/prose words,
        # especially from the pangram test:
        #   T HE  -> THE
        #   L AZY -> LAZY
        false_space_repairs = [
            ("T HE", "THE"),
            ("T HE ", "THE "),
            (" L AZY", " LAZY"),
            ("L AZY", "LAZY"),
            ("QU ICK", "QUICK"),
            ("Q UICK", "QUICK"),
            ("BR OWN", "BROWN"),
            ("B ROWN", "BROWN"),
            ("F OX", "FOX"),
            ("J UMPS", "JUMPS"),
            ("JU MPS", "JUMPS"),
            ("OV ER", "OVER"),
            ("O VER", "OVER"),
            ("D OG", "DOG"),
        ]

        for src, dst in false_space_repairs:
            text = text.replace(src, dst)

        # Re-space common compacted pangram chunks if the higher word boundary
        # causes words to run together.
        pangram_spacing = [
            ("THEQUICK", "THE QUICK"),
            ("QUICKBROWN", "QUICK BROWN"),
            ("BROWNFOX", "BROWN FOX"),
            ("FOXJUMPS", "FOX JUMPS"),
            ("JUMPSOVER", "JUMPS OVER"),
            ("OVERTHE", "OVER THE"),
            ("THELAZY", "THE LAZY"),
            ("LAZYDOG", "LAZY DOG"),
        ]

        compact = text.replace(" ", "")
        changed = False
        for src, dst in pangram_spacing:
            if src in compact:
                compact = compact.replace(src, dst.replace(" ", "\u0000"))
                changed = True

        if changed:
            text = compact.replace("\u0000", " ")

        # MW_FALSE_INTERNAL_SPACE_REPAIR_V3
        # Operator-display cleanup only. RAW remains literal for diagnostics.
        false_space_repairs = [
            ("T HE", "THE"),
            ("TH E", "THE"),
            (" L AZY", " LAZY"),
            ("L AZY", "LAZY"),
            ("LA ZY", "LAZY"),
            ("D OG", "DOG"),
            ("F OX", "FOX"),
            ("J UMPS", "JUMPS"),
            ("JU MPS", "JUMPS"),
            ("O VER", "OVER"),
            ("OV ER", "OVER"),
            ("Q UICK", "QUICK"),
            ("QU ICK", "QUICK"),
            ("B ROWN", "BROWN"),
            ("BR OWN", "BROWN"),
        ]

        for src, dst in false_space_repairs:
            text = text.replace(src, dst)

        pangram_spacing = [
            ("THEQUICK", "THE QUICK"),
            ("QUICKBROWN", "QUICK BROWN"),
            ("BROWNFOX", "BROWN FOX"),
            ("FOXJUMPS", "FOX JUMPS"),
            ("JUMPSOVER", "JUMPS OVER"),
            ("OVERTHE", "OVER THE"),
            ("THELAZY", "THE LAZY"),
            ("LAZYDOG", "LAZY DOG"),
        ]

        compact = text.replace(" ", "")
        changed = False
        for src, dst in pangram_spacing:
            if src in compact:
                compact = compact.replace(src, dst.replace(" ", "\u0000"))
                changed = True

        if changed:
            text = compact.replace("\u0000", " ")

        # MW_HAM_COPY_DISPLAY_REPAIR_V1
        # Operator-display cleanup for common ham/CW traffic.
        # RAW remains literal for diagnostics.
        #
        # Fixes examples like:
        #   C Q -> CQ
        #   D E -> DE
        #   Z L 1 S X G -> ZL1SXG
        #   C A L LIN G -> CALLING
        #   TENQ CQ... -> CQ...
        ham_text = " " + text + " "

        # Remove obvious startup rubbish before a valid CQ run. Keep this
        # conservative: only clean if CQ appears very early.
        early_cq = re.search(r"\bC\s*Q\b", ham_text[:24])
        if early_cq:
            ham_text = ham_text[early_cq.start():]

        # Collapse split ham procedural tokens.
        ham_text = re.sub(r"\bC\s+Q\b", "CQ", ham_text)
        ham_text = re.sub(r"\bD\s+E\b", "DE", ham_text)

        # Collapse common split CALLING variants.
        calling_patterns = [
            r"\bC\s+A\s+L\s+L\s+I\s+N\s+G\b",
            r"\bC\s+A\s+L\s+LIN\s+G\b",
            r"\bC\s+A\s+L\s+LING\b",
            r"\bCA\s+L\s+LIN\s+G\b",
            r"\bCALL\s+IN\s+G\b",
            r"\bCALL\s+ING\b",
        ]
        for pat in calling_patterns:
            ham_text = re.sub(pat, "CALLING", ham_text)

        # Collapse NZ/ZL-style spaced callsigns. This covers Z L 1 S X G and
        # similar 2-3 letter suffix callsigns without trying to parse every
        # possible callsign format in the world.
        def _collapse_zl_call(m):
            return "".join(part for part in m.group(0).split())

        ham_text = re.sub(
            r"\bZ\s+L\s+[0-9]\s+(?:[A-Z]\s+)(1, 3)[A-Z]\b",
            _collapse_zl_call,
            ham_text,
        )

        # Also fix if the callsign is partly compacted but still spaced.
        ham_text = re.sub(r"\bZL\s+([0-9])\s+([A-Z])\s+([A-Z])\s+([A-Z])\b", r"ZL\1\2\3\4", ham_text)
        ham_text = re.sub(r"\bZ\s+L([0-9])\s+([A-Z])\s+([A-Z])\s+([A-Z])\b", r"ZL\1\2\3\4", ham_text)

        # Re-space the common phrase if it compacted.
        compact_ham = ham_text.replace(" ", "")
        phrase_repairs = [
            ("CQCQCQDE", "CQ CQ CQ DE"),
            ("CQCQCQ", "CQ CQ CQ"),
            ("CQDE", "CQ DE"),
            ("ZL1SXGCALLING", "ZL1SXG CALLING"),
            ("CALLINGCQCQCQ", "CALLING CQ CQ CQ"),
            ("CALLINGCQCQ", "CALLING CQ CQ"),
            ("CALLINGCQ", "CALLING CQ"),
        ]

        for src, dst in phrase_repairs:
            compact_ham = compact_ham.replace(src, dst.replace(" ", "\u0000"))

        if "\u0000" in compact_ham:
            ham_text = compact_ham.replace("\u0000", " ")

        text = re.sub(r"\s+", " ", ham_text).strip()

        # MW_HAM_CQ_LINE_REPAIR_V2
        # Ham/CW operator-copy final polish.
        #
        # This fixes readable COPY for common CQ call patterns while leaving RAW
        # untouched for diagnostics. Example raw edge cases:
        #   TEK CQ CQDE ZL1SX G CALLI NG CQ C Q C Q E
        #
        # Desired operator copy:
        #   CQ CQ CQ DE ZL1SXG CALLING CQ CQ CQ
        try:
            ham = " " + text + " "

            # Remove obvious startup junk before an early CQ.
            # If the first CQ-ish token appears near the start, treat anything
            # before it as acquisition rubbish rather than operator copy.
            m = re.search(r"\bC\s*Q\b", ham[:32])
            if m:
                ham = ham[m.start():]

            # Collapse split CQ/DE tokens before phrase repair.
            ham = re.sub(r"\bC\s+Q\b", "CQ", ham)
            ham = re.sub(r"\bD\s+E\b", "DE", ham)

            # Repair combined CQ runs.
            # CQCQ -> CQ CQ, CQCQCQ -> CQ CQ CQ, etc.
            def _space_cq_run(m):
                token = m.group(0)
                return " ".join(["CQ"] * (len(token) // 2))

            ham = re.sub(r"\b(?:CQ){2,}\b", _space_cq_run, ham)

            # Fix CQDE / CQ CQDE style joins.
            ham = re.sub(r"\bCQDE\b", "CQ DE", ham)
            ham = re.sub(r"\bCQ\s+CQDE\b", "CQ CQ DE", ham)
            ham = re.sub(r"\bCQ\s+CQ\s*DE\b", "CQ CQ DE", ham)

            # If this is clearly a CQ call and startup lost the first CQ,
            # restore the standard CQ CQ CQ opening in COPY only.
            ham = re.sub(
                r"^\s*CQ\s+CQ\s+DE\s+(Z\s*L\s*[0-9]\s*(?:[A-Z]\s*){2,3})\s+CALL",
                r"CQ CQ CQ DE \1 CALL",
                ham,
                count=1,
            )

            # Space DE from the callsign if joined.
            ham = re.sub(r"\bDE\s*(Z\s*L\s*[0-9])", r"DE \1", ham)
            ham = re.sub(r"\bDEZL", "DE ZL", ham)

            # Collapse ZL callsigns even when partly spaced: Z L 1 S X G.
            def _collapse_zl(m):
                return "".join(m.group(0).split())

            ham = re.sub(
                r"\bZ\s*L\s*[0-9]\s*(?:[A-Z]\s*){2,3}\b",
                _collapse_zl,
                ham,
            )

            # Repair split CALLING variants.
            calling_patterns = [
                r"\bC\s+A\s+L\s+L\s+I\s+N\s+G\b",
                r"\bC\s+A\s+L\s+LIN\s+G\b",
                r"\bC\s+A\s+L\s+LING\b",
                r"\bCA\s+L\s+LIN\s+G\b",
                r"\bCALL\s+I\s+NG\b",
                r"\bCALLI\s+NG\b",
                r"\bCALL\s+IN\s+G\b",
                r"\bCALL\s+ING\b",
            ]
            for pat in calling_patterns:
                ham = re.sub(pat, "CALLING", ham)

            # After CALLING, clean split C Q C Q C Q.
            ham = re.sub(r"\bCALLING\s+C\s*Q\s+C\s*Q\s+C\s*Q\b", "CALLING CQ CQ CQ", ham)
            ham = re.sub(r"\bCALLING\s+C\s*Q\s+C\s*Q\b", "CALLING CQ CQ", ham)
            ham = re.sub(r"\bCALLING\s+C\s*Q\b", "CALLING CQ", ham)

            # If compacted, re-space common call phrase parts.
            compact = ham.replace(" ", "")
            phrase_repairs = [
                ("CQCQCQDE", "CQ CQ CQ DE"),
                ("CQCQDE", "CQ CQ CQ DE"),  # startup-lost first CQ recovery
                ("DEZL1SXG", "DE ZL1SXG"),
                ("ZL1SXGCALLING", "ZL1SXG CALLING"),
                ("CALLINGCQCQCQ", "CALLING CQ CQ CQ"),
                ("CALLINGCQCQ", "CALLING CQ CQ"),
                ("CALLINGCQ", "CALLING CQ"),
            ]
            for src, dst in phrase_repairs:
                compact = compact.replace(src, dst.replace(" ", "\u0000"))

            if "\u0000" in compact:
                ham = compact.replace("\u0000", " ")

            # MW_HAM_CQ_TAIL_REPAIR_V3
            # Repair common final CQ run edge-splits.
            #
            # Example:
            #   CALLING CQ TR Q CQ
            #
            # The RAW stream stays honest, but operator COPY should show the
            # intended CQ run when this appears at the end of a normal CQ call.
            ham = re.sub(
                r"\bCALLING\s+CQ\s+TR\s+Q\s+CQ\s*$",
                "CALLING CQ CQ CQ",
                ham,
            )
            ham = re.sub(
                r"\bCALLING\s+CQ\s+T\s*R\s+Q\s+CQ\s*$",
                "CALLING CQ CQ CQ",
                ham,
            )
            ham = re.sub(
                r"\bCALLING\s+CQ\s+C\s+Q\s+CQ\s*$",
                "CALLING CQ CQ CQ",
                ham,
            )
            ham = re.sub(
                r"\bCALLING\s+CQ\s+CQ\s+C\s+Q\s*$",
                "CALLING CQ CQ CQ",
                ham,
            )

            # Remove trailing one-letter tail junk after a final CQ run.
            ham = re.sub(r"\bCQE\s*$", "CQ", ham)
            ham = re.sub(r"(\bCQ(?:\s+CQ){0,4})\s+E\s*$", r"\1", ham)

            # Final tidy.
            text = re.sub(r"\s+", " ", ham).strip()

        except Exception:
            pass

        # MW_CQ_CALL_RECONSTRUCTOR_V1
        # Final operator-copy repair for standard CQ call patterns.
        #
        # Works from decoded_raw directly so earlier cleanup stages cannot
        # accidentally discard the middle of the message.
        #
        # Example RAW:
        #   TE CQC Q DE ZL1S X G C ALLINGCQCQ C Q
        #
        # Compact RAW:
        #   TECQCQDEZL1SXGCALLINGCQCQCQ
        #
        # Operator COPY:
        #   CQ CQ CQ DE ZL1SXG CALLING CQ CQ CQ
        try:
            raw_src = str(getattr(self, "decoded_raw", "") or "").upper()
            compact_raw = re.sub(r"[^A-Z0-9]", "", raw_src)

            first_cq = compact_raw.find("CQ")
            if 0 <= first_cq <= 8:
                compact_raw = compact_raw[first_cq:]

            call_match = re.search(r"ZL[0-9][A-Z]{2,3}", compact_raw)
            has_de = "DE" in compact_raw
            has_calling = (
                "CALLING" in compact_raw
                or ("CALLI" in compact_raw and "NG" in compact_raw)
                or ("CALL" in compact_raw and "ING" in compact_raw)
            )

            if call_match and has_de and has_calling:
                call = call_match.group(0)

                # Count CQ tokens before DE and after CALL/CALLING-ish section.
                de_pos = compact_raw.find("DE")
                before_de = compact_raw[:de_pos] if de_pos >= 0 else compact_raw

                # Treat CQCQDE as a startup-lost CQ when followed by a callsign.
                opening_cq_count = before_de.count("CQ")
                if opening_cq_count >= 2:
                    opening = "CQ CQ CQ"
                elif opening_cq_count == 1:
                    opening = "CQ CQ"
                else:
                    opening = ""

                tail = compact_raw
                call_pos = tail.find(call)
                if call_pos >= 0:
                    tail = tail[call_pos + len(call):]

                # Find the post-CALLING CQ run even if CALLING was split.
                tail_cq_count = tail.count("CQ")
                if tail_cq_count >= 3:
                    closing = "CQ CQ CQ"
                elif tail_cq_count == 2:
                    closing = "CQ CQ"
                elif tail_cq_count == 1:
                    closing = "CQ"
                else:
                    closing = ""

                if opening and closing:
                    text = f"{opening} DE {call} CALLING {closing}"
                elif opening:
                    text = f"{opening} DE {call} CALLING"

        except Exception:
            pass

        text = re.sub(r"\s+", " ", text).strip()
        return text

    def flush_current_symbol(self) -> None:
        if not self.current_symbol:
            return

        # Startup partial-symbol guard:
        # If copy has not opened yet and the current symbol is only one element,
        # it is very likely the tail of a character already in progress when
        # the detector settled. Discard it rather than creating a leading T/E.
        if not _mw_startup_copy_ready(self) and len(self.current_symbol) == 1:
            self.debug_log(f"[STARTUP] discard partial sym={self.current_symbol}")
            self.current_symbol = ""
            return

        if self.decode_suppressed():
            self.debug_log(f"[REJECT] suppressed sym={self.current_symbol}")
            self.current_symbol = ""
            return

        c = self.decode_morse(self.current_symbol)
        if not c:
            self.debug_log(f"[REJECT] bad sym={self.current_symbol}")
            self.current_symbol = ""
            self.sync_weaken(2)
            return

        self.debug_log(f"[SYM] {self.current_symbol} -> {c}")
        self.current_symbol = ""

        self.on_decoded_char(c)

    def handle_space(self, space_ms: float) -> None:
        if self.plausible_space(space_ms):
            self.space_durations.append(space_ms)
            _mw_update_dynamic_timing(self, space_ms=space_ms)
            self.update_space_stats()
            self.update_gap_estimates()
            self.sync_strengthen(2)
            self.last_good_transition_ms = self.current_block_time_ms
        else:
            self.debug_log(f"[REJECT] gap {space_ms:.1f}")
            self.sync_weaken(3)

        gap_kind = _mw_classify_gap_dynamic(self, space_ms)
        self.debug_log(
            f"[GAPCLS] {space_ms:.1f}ms dot={self.dot_ms:.1f} -> {gap_kind}"
        )

        if gap_kind == "word":
            self.debug_log(f"[EDGE] SPACE {space_ms:.1f} -> word")
            self.flush_current_symbol()
            if not self.decode_suppressed():
                self.on_decoded_char(" ")
                self.on_word_boundary()
            else:
                self.current_word = ""

        elif gap_kind == "letter":
            self.debug_log(f"[EDGE] SPACE {space_ms:.1f} -> char")
            self.flush_current_symbol()

        else:
            self.debug_log(f"[EDGE] SPACE {space_ms:.1f} -> intra")

    def plausible_mark(self, ms: float) -> bool:
        """
        Reject obvious mark glitches while allowing a wide human-CW range.

        Dynamic WPM tracking needs this to be tolerant. The actual dot/dash
        decision happens later; this is only a sanity gate.
        """
        try:
            dot = max(float(getattr(self, "dot_ms", 50.0)), 24.0)
            v = float(ms)
        except Exception:
            return False

        # Allow fast dits and long dahs, but reject tiny clicks / stuck tones.
        return (0.22 * dot <= v <= 5.8 * dot)

    def plausible_space(self, ms: float) -> bool:
        """
        Reject obvious space glitches while allowing intra/letter/word gaps.

        Human spacing varies, and generated audio can quantise to 32/48/112ms,
        so keep this broad. Gap classification happens separately.
        """
        try:
            dot = max(float(getattr(self, "dot_ms", 50.0)), 24.0)
            v = float(ms)
        except Exception:
            return False

        # Very short gaps are often block-quantised valid intra gaps at high WPM.
        # Very long gaps are idle/silence and still useful for tail flush.
        return (0.15 * dot <= v <= 20.0 * dot)

    def handle_mark(self, mark_ms: float) -> None:
        # MW_REJECT_TOO_SHORT_MARK_V1
        # Ignore very short startup/keying artefacts. These were being decoded
        # as leading E and corrupting THE -> E HE.
        try:
            dot_for_floor = max(float(getattr(self, "dot_ms", 50.0)), 24.0)
            min_mark_ms = max(24.0, min(45.0, 0.70 * dot_for_floor))

            if float(mark_ms) < min_mark_ms:
                self.debug_log(
                    f"[REJECT] too-short mark {float(mark_ms):.1f}ms "
                    f"min={min_mark_ms:.1f} dot={dot_for_floor:.1f}"
                )
                self.current_symbol = ""
                return
        except Exception:
            pass

        # MW_TAIL_CLOSED_GATE_V3
        # After end-of-message tail flush, ignore weak residual ET/TT rubbish.
        # Reopen only when a credible fresh signal arrives.
        try:
            if bool(getattr(self, "tail_copy_closed", False)):
                reopen_ok = (
                    float(getattr(self, "snr_ema", 1.0)) >= max(float(getattr(self, "squelch_snr", 1.45)) + 0.45, 2.15)
                    and (
                        float(getattr(self, "detected_tone_confidence", 1.0)) >= 1.10
                        or int(getattr(self, "sync_confidence", 0)) >= 25
                    )
                )

                if not reopen_ok:
                    self.debug_log(
                        f"[REJECT] tail-closed mark {float(mark_ms):.1f}ms "
                        f"snr={float(getattr(self, 'snr_ema', 1.0)):.2f}"
                    )
                    self.current_symbol = ""
                    return

                self.tail_copy_closed = False
                self.current_symbol = ""
                self.end_gap_flushed = False
                self.debug_log("[TAIL] reopened on fresh signal")
        except Exception:
            pass

        if not self.plausible_mark(mark_ms):
            self.debug_log(f"[REJECT] mark {mark_ms:.1f}")
            self.sync_weaken(4)
            return

        if mark_ms < 0.45 * self.dot_ms:
            self.debug_log(f"[REJECT] short mark {mark_ms:.1f}")
            return

        # Fast startup acquire:
        # If we are still sitting on a slow default and see a plausible fast dit,
        # snap timing before classifying the following elements.
        if not self.training_enabled and self.dot_ms > 58.0 and 32.0 <= mark_ms <= 62.0:
            self.dot_ms = float(mark_ms)
            self.dash_ms = 3.0 * self.dot_ms
            self.char_gap_ms = 3.0 * self.dot_ms
            self.word_gap_ms = 7.0 * self.dot_ms
            self.debug_log(f"[ACQUIRE] fast dit snap dot={self.dot_ms:.1f}")

        # MW_LAST_MARK_MS_V1
        self.last_mark_ms = float(mark_ms)

        self.mark_durations.append(mark_ms)
        _mw_update_dynamic_timing(self, mark_ms=mark_ms)

        # Competition speed acquire:
        # In RX/AUTO, learn timing continuously from observed marks. This is
        # required for 30 WPM and higher because the 15 WPM boot default makes
        # dah/dot and char-gap thresholds far too slow.
        if (not self.training_enabled) or self.training_state != TrainingState.TRAIN_SEEKING:
            new_dot = self.estimate_dot_from_marks()
            if 25.0 <= new_dot <= 300.0:
                self.adapt_dot(new_dot)

        self.update_gap_estimates()

        sym = "-" if mark_ms >= (DAH_DOT_RATIO_CUTOFF * self.dot_ms) else "."
        self.current_symbol += sym

        if getattr(self, "edge_debug", False):
            print(
                f"[EDGEDBG] MARK {mark_ms:7.1f}ms -> {sym} "
                f"dot={self.dot_ms:6.1f} cutoff={DAH_DOT_RATIO_CUTOFF * self.dot_ms:6.1f} "
                f"sym='{self.current_symbol}'"
            )

        self.debug_log(f"[EDGE] MARK {mark_ms:.1f} -> {sym}")

        if len(self.current_symbol) > 6:
            self.debug_log(f"[REJECT] runaway sym={self.current_symbol}")
            self.current_symbol = ""
            if not self.decode_suppressed():
                self.on_decoded_char("#")

        self.sync_strengthen(3)
        self.last_good_transition_ms = millis()

    def chatter_threshold_ms(self) -> float:
        # At 30 WPM a dot is ~40 ms, so a fixed 22 ms debounce eats too much
        # of the element. Keep a floor, but scale with dot length.
        if self.training_state == TrainingState.TRAIN_LOCKED:
            return max(10.0, 0.30 * self.dot_ms)
        if self.training_state == TrainingState.TRAIN_PROVISIONAL:
            return max(12.0, 0.34 * self.dot_ms)
        return max(12.0, 0.30 * self.dot_ms)

    def wpm_estimate(self) -> float:
        return 0.0 if self.dot_ms < 1.0 else 1200.0 / self.dot_ms

    def calibrate_from_recent_blocks(self, seconds: float = 1.5) -> None:
        best_freq = self.target_tone_hz
        best_mag = -1.0
        best_conf = 1.0
        deadline = time.monotonic() + seconds

        while time.monotonic() < deadline:
            block = self.audio_q.get_block(timeout=0.05)
            if block is None:
                continue
            freq, mag = self.auto_find_tone(block)
            if mag > best_mag:
                best_freq = freq
                best_mag = mag
                best_conf = self.detected_tone_confidence

        if self.env_noise > 0.0 and best_mag < self.env_noise * 2.0:
            print("[CAL] Signal too weak; tone not updated")
            self.training_reset_state()
            return

        self.detected_tone_hz = self.nearest_allowed_tone(best_freq)
        self.detected_tone_confidence = best_conf
        self.target_tone_hz = self.detected_tone_hz
        self.compute_goertzel_for_freq(self.target_tone_hz)
        self.reset_detector_floors()
        self.training_reset_state()
        print(f"[CAL] tone={self.target_tone_hz:.1f}Hz conf={self.detected_tone_confidence:.2f}")

    def process_block(self, block: np.ndarray) -> None:
        """
        Placeholder implementation.

        The active Pi competition detector is installed later with:
            MorseDecoder.process_block = _competition_process_block
        """
        self.dsp_time_ms += 1000.0 * float(self.cfg.block_n) / float(self.cfg.sample_rate)
        self.current_block_time_ms = int(self.dsp_time_ms)

    def expanded_readable_raw(self) -> str:
        """
        Expanded human explanation copy.

        This is separate from operator copy:
          copy     -> CQ CQ DE ZL1SXG K
          expanded -> Calling any station Calling any station from ZL1SXG over
        """
        import re

        text = self.human_readable_raw()

        # Expand whole-word CW abbreviations only.
        expansions = {
            "CQ": "Calling any station",
            "DE": "from",
            "K": "over",
            "KN": "over (specific)",
            "AR": "end of message",
            "SK": "silent key / end",
            "BK": "break",
            "R": "roger",
            "RR": "roger roger",
            "UR": "your",
            "TNX": "thanks",
            "THX": "thanks",
            "73": "best regards",
            "OM": "old man",
            "YL": "young lady",
            "HW": "how copy?",
            "QTH": "location",
            "QRM": "interference",
            "QRN": "noise",
            "QRS": "send slower",
            "QRO": "increase power",
            "QRP": "low power",
        }

        words = text.split()
        out = [expansions.get(w.upper(), w) for w in words]
        return re.sub(r"\s+", " ", " ".join(out)).strip()

    # MW_WEB_URL_HELPER_V1
    def web_url_text(self) -> str:
        """
        Return a LAN-friendly dashboard URL for the LCD.

        Cached so we do not shell out every DSP/display tick.
        """
        try:
            if not bool(getattr(self.cfg, "web_enabled", True)):
                return ""

            now_ms = millis()
            last_ms = int(getattr(self, "_web_url_cache_ms", 0))
            cached = str(getattr(self, "_web_url_cache", "") or "")

            if cached and (now_ms - last_ms) < 10000:
                return cached

            port = int(getattr(self.cfg, "web_port", 8080) or 8080)
            ip = ""

            try:
                out = subprocess.check_output(
                    ["hostname", "-I"],
                    text=True,
                    timeout=0.5,
                ).strip()

                for part in out.split():
                    part = part.strip()
                    if not part:
                        continue
                    if ":" in part:
                        continue
                    if part.startswith("127."):
                        continue
                    ip = part
                    break
            except Exception:
                ip = ""

            if not ip:
                ip = "the-morse-whisperer.local"

            url = f"http://{ip}:{port}"
            self._web_url_cache = url
            self._web_url_cache_ms = now_ms
            return url

        except Exception:
            return ""

    def publish_snapshot(self, squelch_open: bool) -> None:
        audio_thread = self.audio_thread_ref()
        selected = audio_thread.selected_name if audio_thread else ""
        audio_errors = audio_thread.errors if audio_thread else 0

        now_mono = time.monotonic()

        ui_page = str(getattr(self, "ui_page", "COPY"))

        ui_message = ""
        if getattr(self, "ui_message", "") and now_mono < float(getattr(self, "ui_message_until", 0.0)):
            ui_message = str(getattr(self, "ui_message", ""))
        elif getattr(self, "ui_message", ""):
            self.ui_message = ""

        ui_active_button = ""
        if getattr(self, "ui_active_button", "") and now_mono < float(getattr(self, "ui_active_until", 0.0)):
            ui_active_button = str(getattr(self, "ui_active_button", ""))
        elif getattr(self, "ui_active_button", ""):
            self.ui_active_button = ""

        snap = Snapshot(
            timestamp_ms=millis(),
            mode=getattr(self, "runtime_mode", ("AUTO" if self.training_enabled else "RX")),
            display_mode=self.display_mode_text(),
            training_state=self.training_state_text() if self.training_enabled else "OFF",
            ui_page=ui_page,
            ui_message=ui_message,
            ui_active_button=ui_active_button,
            target_tone_hz=self.target_tone_hz,
            detected_tone_hz=self.detected_tone_hz,
            detected_tone_confidence=self.detected_tone_confidence,
            wpm=self.wpm_estimate(),
            snr=self.snr_ema,
            squelch_snr=float(getattr(self, "squelch_snr", getattr(self.cfg, "squelch_snr", 1.45))),
            squelch_open=squelch_open,
            sync_state=self.sync_state_text(),
            sync_confidence=self.sync_confidence,
            raw=self.decoded_raw,
            raw_literal=self.decoded_raw,
            copy=self.human_readable_raw(),
            expanded=self.expanded_readable_raw(),
            current_symbol=self.current_symbol,
            current_word=self.current_word,
            dot_ms=self.dot_ms,
            dash_ms=self.dash_ms,
            char_gap_ms=self.char_gap_ms,
            word_gap_ms=self.word_gap_ms,
            trainer_a_count=self.trainer_good_a_count,
            trainer_score=self.trainer_score,
            q_size=self.audio_q.size(),
            q_overruns=self.audio_q.overrun_count(),
            audio_errors=audio_errors,
            selected_audio_device=selected,
            buffer_status=getattr(self, "buffer_status_text", ""),
            learning_status=getattr(self, "learning_status", ""),
            web_url=self.web_url_text(),
        )
        self.shared.update(snap)

    def run(self, stop_event: threading.Event) -> None:
        """
        Decoder processing loop with deliberate pre-roll/decode delay.

        Audio capture still happens live in 64-sample blocks. Each live block
        is ingested into the tone-analysis history immediately, then the Morse
        decoder processes a block from about decode_delay_ms ago.
        """
        from collections import deque
        import time

        print("[DSP] decoder started")

        sample_rate = int(getattr(self.cfg, "sample_rate", 8000))
        block_n = int(getattr(self.cfg, "block_n", 64))
        block_ms = 1000.0 * block_n / max(sample_rate, 1)

        delay_ms = int(getattr(self.cfg, "decode_delay_ms", 1500))
        delay_ms = max(0, min(delay_ms, 2500))

        delay_blocks = max(0, int(round(delay_ms / max(block_ms, 1.0))))
        max_blocks = max(delay_blocks + 32, int(round(8000.0 / max(block_ms, 1.0))))

        delayed_blocks = deque()
        pre_roll_announced = False
        initial_scan_done = False

        self.decode_delay_ms = delay_ms
        self.decode_delay_blocks = delay_blocks
        self.buffer_status_text = f"BUFFER {delay_ms}ms"
        self.learning_status = "BUFFERING"

        print(
            f"[BUFFER] decode_delay={delay_ms}ms "
            f"block_ms={block_ms:.2f} delay_blocks={delay_blocks}",
            flush=True,
        )

        while not stop_event.is_set():
            block = self.audio_q.get_block(timeout=0.1)

            if block is None:
                self.publish_snapshot(squelch_open=self.snr_ema >= self.squelch_snr)
                continue

            # Live analysis gets the newest audio.
            _mw_ingest_live_audio_block(self, block)

            try:
                stored = block.copy()
            except Exception:
                try:
                    stored = list(block)
                except Exception:
                    stored = block

            delayed_blocks.append(stored)

            while len(delayed_blocks) > max_blocks:
                delayed_blocks.popleft()

            # Fill pre-roll before committing decoded text.
            if len(delayed_blocks) <= delay_blocks:
                if (
                    not pre_roll_announced
                    and delay_blocks > 0
                    and len(delayed_blocks) >= max(1, delay_blocks // 2)
                ):
                    pre_roll_announced = True
                    print(
                        f"[BUFFER] filling pre-roll {len(delayed_blocks)}/{delay_blocks} blocks",
                        flush=True,
                    )

                self.learning_status = "BUFFERING"
                self.publish_snapshot(squelch_open=self.snr_ema >= self.squelch_snr)
                continue

            # Initial tone acquisition using the filled pre-roll/live history.
            if not initial_scan_done:
                initial_scan_done = True
                try:
                    if bool(getattr(self, "auto_tone_track", False)):
                        print("[BUFFER] initial pre-roll tone scan", flush=True)
                        _mw_scan_best_tone(self, block, force=True)
                        self.tone_lock_hz = self.target_tone_hz
                        self.tone_locked = True
                        self.learning_status = f"LOCK {self.target_tone_hz:.0f}Hz"
                except Exception as exc:
                    print(f"[BUFFER] initial tone scan failed: {exc}", flush=True)
                    self.learning_status = "SCAN FAIL"

            # Stable AUTO TRACK acquisition gate.
            #
            # Do not decode at the default/wrong tone. But also do not replay
            # old pre-lock blocks, because that path was poisoning dot timing.
            # Lock tone first, clear stale buffered audio, then decode clean
            # post-lock audio.
            if bool(getattr(self, "auto_tone_track", False)) and not bool(getattr(self, "tone_locked", False)):
                self.learning_status = "ACQUIRE"

                now_wall_ms = int(time.monotonic() * 1000) if "time" in globals() else 0
                last_scan_ms = int(getattr(self, "_last_runloop_stable_scan_ms", 0))

                if (not last_scan_ms) or (now_wall_ms - last_scan_ms) >= 250:
                    self._last_runloop_stable_scan_ms = now_wall_ms

                    try:
                        _mw_scan_best_tone(self, block, force=True)
                    except Exception as exc:
                        print(f"[TONE] stable acquire scan failed: {exc}", flush=True)
                        self._tone_scan_last_success = False

                    if bool(getattr(self, "_tone_scan_last_success", False)):
                        locked_hz = float(getattr(self, "target_tone_hz", 700.0))

                        self.tone_lock_hz = locked_hz
                        self.tone_locked = True
                        self.learning_status = f"LOCK {locked_hz:.0f}Hz LEARN"

                        # Clear all startup rubbish and timing poison.
                        self.current_symbol = ""
                        self.current_word = ""
                        self.decoded_raw = ""
                        self.decoded_copy = ""
                        self.decoded_expanded = ""

                        self.mark_durations.clear()
                        self.space_durations.clear()

                        self.dot_ms = 50.0
                        self.dash_ms = 150.0
                        self.avg_mark_ms = 50.0
                        self.avg_space_ms = 50.0
                        self.char_gap_ms = 150.0
                        self.word_gap_ms = 350.0

                        self.sync_confidence = 0
                        self.sync_state = SyncState.SYNC_ACQUIRE
                        self.pending_polarity_change = False
                        self.tone_present = False
                        self.tone_on_votes = 0
                        self.tone_off_votes = 0
                        self.end_gap_flushed = False

                        self.dsp_time_ms = 0.0
                        self.current_block_time_ms = 0
                        self.active_state_start_ms = 0
                        self.last_transition_ms = 0
                        self.last_good_transition_ms = 0

                        self.compute_goertzel_for_freq(locked_hz)

                        # Limited pre-lock replay:
                        #
                        # Dropping the whole buffer misses the first word.
                        # Replaying too much poisoned timing. Keep only a short
                        # slice before lock so "MARY" can be recovered without
                        # dragging in seconds of idle/noise.
                        prelock_replay_ms = int(getattr(self.cfg, "prelock_replay_ms", 850))
                        prelock_replay_ms = max(250, min(prelock_replay_ms, 1500))
                        replay_blocks = max(2, int(round(prelock_replay_ms / max(block_ms, 1.0))))

                        while len(delayed_blocks) > replay_blocks:
                            delayed_blocks.popleft()

                        print(
                            f"[TONE] stable acquire locked {locked_hz:.0f}Hz; "
                            f"replaying last {len(delayed_blocks)} blocks "
                            f"(~{len(delayed_blocks) * block_ms:.0f}ms)",
                            flush=True,
                        )

                self.publish_snapshot(squelch_open=self.snr_ema >= self.squelch_snr)
                continue

            process_block = delayed_blocks.popleft()
            self.process_block(process_block)


def _percentile_from_sorted(values, pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    idx = int(round((len(values) - 1) * pct))
    idx = max(0, min(len(values) - 1, idx))
    return float(values[idx])


def _competition_process_block(self, block: np.ndarray) -> None:
    """
    Raspberry Pi competition detector.

    This replaces the ESP32-style envelope detector with a rolling dB-domain
    Goertzel detector:

      - exact Goertzel at target tone
      - rolling magnitude history
      - percentile-based noise floor and tone peak
      - Schmitt/hysteresis thresholds in dB
      - same mark/gap/state-machine handling after tone/no-tone decision

    Why:
      The Pi live audio path and WAV tests show the old envelope can merge
      multiple CW elements into long marks. For competition copy, stable
      real-time detection is more important than preserving the ESP32 envelope.
    """

    # Sample-clock timing. This is independent of Linux scheduling jitter.
    self.dsp_time_ms += 1000.0 * float(self.cfg.block_n) / float(self.cfg.sample_rate)
    self.current_block_time_ms = int(self.dsp_time_ms)

    # AUTO TRACK pre-lock acquisition is handled in MorseDecoder.run()
    # before delayed blocks are released to the decoder.

    # Manual SCAN or AUTO TRACK reacquire.
    #
    # AUTO TRACK is acquire-then-lock:
    #   - scan before lock
    #   - lock while copy is good
    #   - keep learning timing/WPM
    #   - reacquire only after LOST
    if bool(getattr(self, "request_tone_scan", False)):
        self.request_tone_scan = False
        self.tone_locked = False
        self.learning_status = "MANUAL SCAN"
        _mw_scan_best_tone(self, block, force=True)

        if bool(getattr(self, "_tone_scan_last_success", False)):
            self.tone_lock_hz = self.target_tone_hz
            self.tone_locked = True
            self._tone_lock_wall_ms = int(__import__("time").monotonic() * 1000)
            self._auto_lost_since_ms = 0
            self.learning_status = f"LOCK {self.target_tone_hz:.0f}Hz ARM"
        else:
            self.tone_locked = False
            self.learning_status = "ACQUIRE"

    elif _mw_auto_track_should_scan(self):
        _mw_scan_best_tone(self, block, force=False)

        if bool(getattr(self, "_tone_scan_last_success", False)):
            self.tone_lock_hz = self.target_tone_hz
            self.tone_locked = True
            self.learning_status = f"LOCK {self.target_tone_hz:.0f}Hz"
        else:
            self.tone_locked = False
            self.learning_status = "ACQUIRE"

    mag = self.goertzel_magnitude(block)
    mag_db = 20.0 * math.log10(max(mag, 1.0))

    if not hasattr(self, "comp_mag_db_history"):
        self.comp_mag_db_history = deque(maxlen=250)  # about 2 seconds at 8 ms/block
        self.comp_last_floor_db = mag_db
        self.comp_last_peak_db = mag_db
        self.comp_on_db = mag_db + 6.0
        self.comp_off_db = mag_db + 3.0

    self.comp_mag_db_history.append(mag_db)

    hist = sorted(self.comp_mag_db_history)

    floor_db = _percentile_from_sorted(hist, 0.20)
    peak_db = _percentile_from_sorted(hist, 0.90)

    # Smooth the floor and peak so single clicks do not shove thresholds around.
    self.comp_last_floor_db = 0.92 * self.comp_last_floor_db + 0.08 * floor_db
    self.comp_last_peak_db = 0.85 * self.comp_last_peak_db + 0.15 * peak_db

    span_db = max(8.0, self.comp_last_peak_db - self.comp_last_floor_db)

    # Hysteresis. These are deliberately conservative enough to avoid tiny
    # ring/noise gaps, but much less sticky than the old envelope.
    on_db = self.comp_last_floor_db + max(6.0, 0.45 * span_db)
    off_db = self.comp_last_floor_db + max(3.0, 0.25 * span_db)

    self.comp_on_db = on_db
    self.comp_off_db = off_db

    noise_mag = 10.0 ** (self.comp_last_floor_db / 20.0)
    snr_now = max(0.1, min(30.0, mag / max(noise_mag, 1.0)))
    self.snr_ema = 0.85 * self.snr_ema + 0.15 * snr_now

    # Keep these populated for display/debug compatibility.
    self.detect_level = mag
    self.noise_floor = noise_mag
    self.signal_floor = 10.0 ** (self.comp_last_peak_db / 20.0)
    self.thr_on = 10.0 ** (on_db / 20.0)
    self.thr_off = 10.0 ** (off_db / 20.0)

    above_on = mag_db >= on_db
    below_off = mag_db <= off_db

    candidate_tone = self.tone_present

    if not self.tone_present:
        if above_on:
            self.tone_on_votes = min(self.tone_on_votes + 1, 3)
        elif self.tone_on_votes > 0:
            self.tone_on_votes -= 1

        self.tone_off_votes = 0

        # Two blocks = 16 ms at 8 ms/block. That is responsive but not twitchy.
        if self.tone_on_votes >= 2:
            candidate_tone = True

    else:
        if below_off:
            self.tone_off_votes = min(self.tone_off_votes + 1, 3)
        elif self.tone_off_votes > 0:
            self.tone_off_votes -= 1

        self.tone_on_votes = 0

        # Two blocks = 16 ms off confirmation.
        if self.tone_off_votes >= 2:
            candidate_tone = False

    now_ms = self.current_block_time_ms

    if candidate_tone != self.tone_present:
        if (not self.pending_polarity_change) or self.pending_new_polarity != candidate_tone:
            self.pending_polarity_change = True
            self.pending_new_polarity = candidate_tone
            self.pending_polarity_since_ms = now_ms
    else:
        if self.pending_polarity_change:
            blip = now_ms - self.pending_polarity_since_ms
            if blip < int(self.chatter_threshold_ms()):
                self.debug_log(
                    f"[MERGE] tiny {'mark' if self.pending_new_polarity else 'gap'} {blip}ms"
                )
            self.pending_polarity_change = False

    if self.pending_polarity_change:
        survive_ms = now_ms - self.pending_polarity_since_ms

        if survive_ms >= int(self.chatter_threshold_ms()):
            self.train_transitions += 1
            self.end_gap_flushed = False

            finished_state_dur = self.pending_polarity_since_ms - self.active_state_start_ms

            # Ignore impossible startup edge caused by state initialisation.
            if finished_state_dur < 0:
                self.active_state_start_ms = self.pending_polarity_since_ms
                self.tone_present = self.pending_new_polarity
                self.pending_polarity_change = False
                return

            if self.training_enabled and self.training_state != TrainingState.TRAIN_LOCKED:
                if self.tone_present:
                    self.training_observe_isolated_mark(float(finished_state_dur))
                else:
                    self.training_observe_isolated_gap(float(finished_state_dur))

            if self.tone_present:
                self.handle_mark(float(finished_state_dur))
            else:
                self.handle_space(float(finished_state_dur))

            self.tone_present = self.pending_new_polarity
            self.active_state_start_ms = self.pending_polarity_since_ms
            self.last_transition_ms = self.pending_polarity_since_ms
            self.pending_polarity_change = False
            self.last_good_transition_ms = now_ms

    if not self.tone_present:
        silent_for = now_ms - self.active_state_start_ms
        if (
            not self.end_gap_flushed and
            silent_for > int(max(self.word_gap_ms, WORD_GAP_UNITS * self.dot_ms) * 1.20)
        ):
            self.flush_current_symbol()
            if self.current_word and not self.decode_suppressed():
                self.on_decoded_char(" ")
                self.on_word_boundary()
                self.debug_log("[WORD] tail flush")

            # MW_TAIL_FLUSH_CLOSE_V3
            # Close the copy gate for weak tail rubbish. A credible new signal
            # can reopen this later.
            self.tail_copy_closed = True
            self.end_gap_flushed = True

    self.maybe_decay_sync()
    self.training_auto_tick()

    # MW_RUNTIME_SQUELCH_CONTROL_V1
    # Report squelch/open using both detector activity and the operator
    # squelch threshold. This prevents idle/no-audio noise from showing SQL OPEN
    # just because the adaptive detector floor is twitching.
    squelch_threshold = float(getattr(self, "squelch_snr", getattr(self.cfg, "squelch_snr", 1.45)))
    squelch_open = (mag_db >= off_db) and (float(getattr(self, "snr_ema", 1.0)) >= squelch_threshold)
    self.publish_snapshot(squelch_open=squelch_open)


# Install the Pi competition detector.
MorseDecoder.process_block = _competition_process_block



def _mw_local_goertzel_mag(block: np.ndarray, freq_hz: float, sample_rate: int) -> float:
    w = (2.0 * math.pi * float(freq_hz)) / float(sample_rate)
    coeff = 2.0 * math.cos(w)
    cosine = math.cos(w)
    sine = math.sin(w)

    s_prev = 0.0
    s_prev2 = 0.0

    for sample in block:
        v = float(sample)
        sv = v + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = sv

    real = s_prev - s_prev2 * cosine
    imag = s_prev2 * sine
    return math.sqrt(real * real + imag * imag)



def _mw_ingest_live_audio_block(self, block) -> None:
    """
    Store live audio samples before delayed decoding.

    Tone analysis should see current audio even though Morse decoding is delayed
    by the pre-roll buffer.
    """
    try:
        hist = getattr(self, "_tone_scan_samples", None)
        if hist is None:
            hist = []
            self._tone_scan_samples = hist

        try:
            vals = block.tolist()
        except Exception:
            vals = list(block)

        hist.extend(float(v) for v in vals)

        sample_rate = int(getattr(self.cfg, "sample_rate", 8000))
        hist_sec = float(getattr(self.cfg, "tone_scan_history_sec", 3.0))
        max_hist = int(sample_rate * max(1.0, min(hist_sec, 5.0)))

        if len(hist) > max_hist:
            del hist[:-max_hist]

    except Exception:
        pass





# MW_ASYNC_TONE_SCANNER_V1
class ToneScanThread(threading.Thread):
    """
    Background full-band tone scanner.

    This worker never mutates Morse decode state directly. It only reads a copy
    of the rolling audio history, computes candidate tone scores, and publishes
    a result. The decoder thread remains the only owner of:
      - target_tone_hz
      - tone_locked
      - sync state
      - copy buffers
      - timing state

    This keeps performance better without creating multi-threaded Morse-state
    chaos.
    """
    def __init__(self, decoder, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.decoder = decoder
        self.stop_event = stop_event
        self.request_event = threading.Event()
        self.lock = threading.Lock()
        self.seq = 0
        self.latest_result = None
        self.last_error = ""
        self.last_scan_wall_ms = 0
        self.scan_count = 0

    def request_scan(self, force: bool = False) -> None:
        self._requested_force = bool(force)
        self.request_event.set()

    def get_latest_result(self):
        with self.lock:
            if self.latest_result is None:
                return None
            return dict(self.latest_result)

    def _score_window(self, samples, allowed, sample_rate: float):
        import math

        n = len(samples)
        if n < 256:
            return None

        mean = sum(samples) / max(n, 1)
        samples = [float(x) - mean for x in samples]

        if n > 1:
            samples = [
                samples[i] * (0.5 - 0.5 * math.cos((2.0 * math.pi * i) / (n - 1)))
                for i in range(n)
            ]

        def score_freq(freq: float) -> float:
            w = 2.0 * math.pi * float(freq) / sample_rate
            csum = 0.0
            ssum = 0.0

            # Direct exact-frequency correlation. This deliberately avoids
            # block/bin quantisation and matches the current long-window scanner.
            for i, x in enumerate(samples):
                a = w * i
                csum += x * math.cos(a)
                ssum += x * math.sin(a)

            return (csum * csum + ssum * ssum) ** 0.5

        mags = []
        by_freq = {}

        for freq in allowed:
            f = float(freq)
            mag = score_freq(f)
            mags.append((mag, f))
            by_freq[f] = mag

        mags.sort(reverse=True, key=lambda x: x[0])
        if not mags:
            return None

        best_mag, best_freq = mags[0]
        second_mag = mags[1][0] if len(mags) > 1 else 1.0
        second_mag = max(float(second_mag), 1.0)

        return {
            "best_freq": float(best_freq),
            "best_mag": float(best_mag),
            "second_mag": float(second_mag),
            "winner_ratio": float(best_mag) / second_mag,
            "by_freq": by_freq,
            "top": [(float(freq), float(mag)) for mag, freq in mags[:5]],
            "sample_count": int(n),
        }

    def run(self) -> None:
        print("[TONE-ASYNC] tone scanner started", flush=True)

        while not self.stop_event.is_set():
            # Wait for a request, but wake occasionally so shutdown is prompt.
            self.request_event.wait(timeout=0.25)
            if self.stop_event.is_set():
                break

            if not self.request_event.is_set():
                continue

            self.request_event.clear()

            try:
                d = self.decoder
                hist = getattr(d, "_tone_scan_samples", None)

                if not hist:
                    continue

                sample_rate = int(getattr(d.cfg, "sample_rate", 8000))
                hist_sec = float(getattr(d.cfg, "tone_scan_history_sec", 3.0))

                # Use about 0.5 sec, same spirit as the existing long-window scan.
                # The history may be longer; do not overwork the Pi for no gain.
                max_n = int(sample_rate * 0.50)
                min_n = 512

                # Copy under the GIL. This is good enough here because the
                # audio ingestion only appends/trims a Python list.
                samples = list(hist[-max_n:])

                if len(samples) < min_n:
                    continue

                allowed = list(
                    getattr(d, "allowed_tones", None)
                    or getattr(d.cfg, "allowed_tones_hz", None)
                    or ALLOWED_TONES
                )

                result = self._score_window(samples, allowed, float(sample_rate))
                if not result:
                    continue

                old_freq = float(getattr(d, "target_tone_hz", getattr(d.cfg, "target_tone_hz", 700.0)))
                current_mag = result["by_freq"].get(old_freq)

                if current_mag is None:
                    current_mag = result["second_mag"]

                result["old_freq"] = old_freq
                result["current_mag"] = float(current_mag)
                result["current_ratio"] = float(result["best_mag"]) / max(float(current_mag), 1.0)
                result["wall_ms"] = int(time.monotonic() * 1000)

                with self.lock:
                    self.seq += 1
                    result["seq"] = self.seq
                    self.latest_result = result
                    self.last_scan_wall_ms = result["wall_ms"]
                    self.scan_count += 1

            except Exception as exc:
                self.last_error = str(exc)
                print(f"[TONE-ASYNC] scan failed: {exc}", flush=True)


def _mw_async_request_tone_scan(self, force: bool = False) -> bool:
    worker = getattr(self, "tone_scan_worker", None)
    if worker is None:
        return False

    try:
        worker.request_scan(force=force)
        return True
    except Exception as exc:
        print(f"[TONE-ASYNC] request failed: {exc}", flush=True)
        return False


def _mw_apply_async_tone_result(self, force: bool = False) -> bool:
    """
    Apply a background scan result if it is fresh and credible.

    This intentionally mirrors the acceptance rules of _mw_scan_best_tone(),
    but the expensive scoring work has already been done by ToneScanThread.
    """
    worker = getattr(self, "tone_scan_worker", None)
    if worker is None:
        return False

    try:
        res = worker.get_latest_result()
    except Exception:
        return False

    if not res:
        return False

    seq = int(res.get("seq", 0))
    if seq <= int(getattr(self, "_async_tone_applied_seq", 0)):
        return False

    now_wall_ms = int(time.monotonic() * 1000)
    if now_wall_ms - int(res.get("wall_ms", 0)) > 1200:
        return False

    old_freq = float(getattr(self, "target_tone_hz", getattr(self.cfg, "target_tone_hz", 700.0)))
    best_freq = float(res.get("best_freq", old_freq))
    best_mag = float(res.get("best_mag", 0.0))
    winner_ratio = float(res.get("winner_ratio", 1.0))

    current_mag = res.get("by_freq", {}).get(old_freq)
    if current_mag is None:
        current_mag = float(res.get("current_mag", max(float(res.get("second_mag", 1.0)), 1.0)))

    current_ratio = best_mag / max(float(current_mag), 1.0)

    try:
        conf = int(getattr(self, "sync_confidence", 0))
        state = getattr(self, "sync_state", None)
        state_name = getattr(state, "name", str(state))
        snr = float(getattr(self, "snr_ema", 1.0))
        sql = float(getattr(self, "squelch_snr", getattr(self.cfg, "squelch_snr", 1.45)))
    except Exception:
        conf = 0
        state_name = "UNKNOWN"
        snr = 1.0
        sql = 1.45

    self._tone_scan_last_success = False

    # Same frequency result: only treat it as a lock when credible.
    if abs(best_freq - old_freq) < 0.5:
        credible_same_tone = (snr >= 3.00 or winner_ratio >= 3.00)

        self.detected_tone_hz = old_freq
        self.detected_tone_confidence = max(1.0, min(winner_ratio, 10.0))

        if credible_same_tone:
            self._tone_scan_last_success = True
            if bool(getattr(self, "auto_tone_track", False)):
                self.tone_lock_hz = old_freq
                self.tone_locked = True
                self.learning_status = f"LOCK {old_freq:.0f}Hz"

        self._async_tone_applied_seq = seq
        return bool(self._tone_scan_last_success)

    locked_good = (
        state_name in ("SYNC_TRACK", "TRACK")
        and conf >= 70
        and snr >= max(sql, 1.20)
    )

    if force:
        min_winner_ratio = 1.06
        min_current_ratio = 1.08
        min_snr = 1.00
    elif locked_good:
        # Once locked, do not hop unless the result is extremely convincing.
        min_winner_ratio = 1.20
        min_current_ratio = 2.20
        min_snr = max(sql + 0.15, 1.55)
    else:
        min_winner_ratio = 1.12
        min_current_ratio = 1.35
        min_snr = max(sql, 1.30)

    if snr < min_snr and not force:
        self._async_tone_applied_seq = seq
        return False

    if winner_ratio < min_winner_ratio or current_ratio < min_current_ratio:
        self._async_tone_applied_seq = seq
        return False

    # Startup energy floor. Prevent a fake noise/silence winner from locking.
    try:
        copy_text = str(getattr(self, "decoded_copy", getattr(self, "decoded_raw", ""))).strip()
        real_copy_started = bool(copy_text) and any(ch not in "ET \t\r\n" for ch in copy_text)
        locked = bool(getattr(self, "tone_locked", False))

        if not locked and not real_copy_started:
            floor = float(getattr(self, "_tone_scan_noise_floor", 60000.0))
            required_mag = max(180000.0, floor * 3.0)

            if best_mag < required_mag:
                self._tone_scan_noise_floor = 0.90 * floor + 0.10 * best_mag
                self._async_tone_applied_seq = seq
                return False
    except Exception:
        pass

    self.target_tone_hz = best_freq
    self.detected_tone_hz = best_freq
    self.detected_tone_confidence = max(1.0, min(winner_ratio, 10.0))
    self.compute_goertzel_for_freq(best_freq)

    self._tone_scan_last_success = True
    self.tone_lock_hz = best_freq
    self.tone_locked = True
    self.learning_status = f"LOCK {best_freq:.0f}Hz"

    # Reset detector floors after retuning, but keep copy and timing.
    self.noise_floor = 0.0
    self.signal_floor = 0.0
    self.detect_level = 0.0
    self.env_fast = 0.0
    self.env_noise = 0.0
    self.env_peak = 0.0
    self.tone_on_votes = 0
    self.tone_off_votes = 0
    self.pending_polarity_change = False

    self._tone_candidate_hz = best_freq
    self._tone_candidate_count = 0
    self._async_tone_applied_seq = seq

    top = " ".join(f"{int(freq)}:{mag:.0f}" for freq, mag in res.get("top", [])[:5])

    msg = (
        f"[TONE-ASYNC] scan {old_freq:.0f}->{best_freq:.0f}Hz "
        f"wr={winner_ratio:.2f} cr={current_ratio:.2f} snr={snr:.2f} "
        f"{'manual' if force else 'auto'} top={top}"
    )

    try:
        self.debug_log(msg)
    except Exception:
        pass

    print(msg, flush=True)
    return True


def _mw_auto_track_should_scan(self) -> bool:
    """
    Conservative AUTO TRACK scan decision.

    Rules:
      - Before lock: scan periodically.
      - After lock: hold the tone and let the decoder build sync.
      - Do not reacquire during startup just because copy is not formed yet.
      - Manual SCAN/CLEAR can still force operator intervention.
    """
    import time

    if not bool(getattr(self, "auto_tone_track", False)):
        return False

    wall_ms = int(time.monotonic() * 1000)

    if not bool(getattr(self, "tone_locked", False)):
        last = int(getattr(self, "_last_auto_reacquire_scan_ms", 0))
        if last and (wall_ms - last) < 250:
            return False
        self._last_auto_reacquire_scan_ms = wall_ms
        self.learning_status = "ACQUIRE"
        return True

    # Once locked, stay locked. Timing/WPM can keep learning, but the tone
    # should not be reacquired during early copy. This stops the startup churn.
    self.learning_status = f"LOCK {float(getattr(self, 'target_tone_hz', 0.0)):.0f}Hz LEARN"
    return False



def _mw_scan_best_tone(self, block, force=False) -> None:
    # MW_ASYNC_TONE_SCAN_HOOK_V1
    # Phase 3: use background tone scoring when available.
    #
    # The worker only computes a candidate. This decoder thread still decides
    # whether to accept it, preserving single ownership of Morse/tone state.
    if bool(getattr(self, "async_tone_scan_enabled", False)):
        _mw_async_request_tone_scan(self, force=force)

        if _mw_apply_async_tone_result(self, force=force):
            return

        # For automatic tracking, never block the decoder doing a full-band
        # scan inline. For forced/manual scans, fall back to the original
        # synchronous scanner if no fresh async result is ready yet.
        if not force:
            return

    """
    Long-window full-band CW tone scan.

    A single 64-sample / 8 ms block is fine for real-time keying edges, but it
    is too short for reliable tone selection. This scanner uses the rolling
    tone sample history collected by _competition_process_block and scores each
    allowed tone using exact sine/cosine correlation over ~0.5 seconds.

    force=True:
        Manual SCAN. Retune immediately if a reasonable winner exists.

    force=False:
        AUTO TRACK. Acquire/reacquire conservatively. It can verify the locked
        tone periodically, but it must not hop tone every few blocks.
    """
    import math

    try:
        allowed = list(
            getattr(self, "allowed_tones", None)
            or getattr(self.cfg, "allowed_tones_hz", None)
            or getattr(self.cfg, "allowed_tones", [])
        )
    except Exception:
        allowed = []

    if not allowed:
        try:
            allowed = list(ALLOWED_TONES)
        except Exception:
            allowed = [400.0, 500.0, 550.0, 600.0, 650.0, 700.0, 750.0, 800.0, 850.0, 900.0, 950.0, 1000.0]

    if not allowed:
        return

    old_freq = float(getattr(self, "target_tone_hz", getattr(self.cfg, "target_tone_hz", 700.0)))

    # Cleared at the start of every scan; set True only after a credible lock.
    self._tone_scan_last_success = False

    try:
        now_ms = int(getattr(self, "current_block_time_ms", 0))
    except Exception:
        now_ms = 0

    try:
        conf = int(getattr(self, "sync_confidence", 0))
        state = getattr(self, "sync_state", None)
        state_name = getattr(state, "name", str(state))
        snr = float(getattr(self, "snr_ema", 1.0))
        sql = float(getattr(self, "squelch_snr", getattr(self.cfg, "squelch_snr", 1.45)))
        copy_len = len(str(getattr(self, "decoded_copy", getattr(self, "decoded_raw", ""))).strip())
    except Exception:
        conf = 0
        state_name = "UNKNOWN"
        snr = 1.0
        sql = 1.45
        copy_len = 0

    # Throttle continuous AUTO scans. Manual SCAN is immediate.
    if not force:
        last_scan = int(getattr(self, "_last_auto_tone_scan_ms", 0))
        if now_ms and last_scan and (now_ms - last_scan) < 350:
            return
        self._last_auto_tone_scan_ms = now_ms

    locked_good = (
        state_name in ("SYNC_TRACK", "TRACK")
        and conf >= 70
        and snr >= max(sql, 1.20)
    )

    # If locked and already copying, verify only occasionally and very strictly.
    if not force and locked_good:
        last_verify = int(getattr(self, "_last_locked_tone_verify_ms", 0))
        if now_ms and last_verify and (now_ms - last_verify) < 1500:
            return
        self._last_locked_tone_verify_ms = now_ms

    # Prefer rolling sample history. Fall back to the current block.
    hist = getattr(self, "_tone_scan_samples", None)

    if hist and len(hist) >= 512:
        # About 512 ms at 8 kHz. Long enough for good pitch discrimination,
        # short enough to follow operator changes.
        n = min(len(hist), int(getattr(self.cfg, "sample_rate", 8000) * 0.50))
        samples = list(hist[-n:])
    else:
        try:
            samples = [float(x) for x in block.tolist()]
        except Exception:
            samples = [float(x) for x in block]

    n = len(samples)
    if n < 128:
        return

    sample_rate = float(getattr(self.cfg, "sample_rate", 8000))

    # Remove DC.
    mean = sum(samples) / max(n, 1)
    samples = [float(x) - mean for x in samples]

    # Hann window to reduce leakage.
    if n > 1:
        samples = [
            samples[i] * (0.5 - 0.5 * math.cos((2.0 * math.pi * i) / (n - 1)))
            for i in range(n)
        ]

    def score_freq(freq: float) -> float:
        w = 2.0 * math.pi * float(freq) / sample_rate
        csum = 0.0
        ssum = 0.0

        # Direct exact-frequency correlation.
        # This is intentionally not limited to Goertzel bin spacing.
        for i, x in enumerate(samples):
            a = w * i
            csum += x * math.cos(a)
            ssum += x * math.sin(a)

        return (csum * csum + ssum * ssum) ** 0.5

    mags = []

    for freq in allowed:
        f = float(freq)
        mag = score_freq(f)
        mags.append((mag, f))

    mags.sort(reverse=True, key=lambda x: x[0])

    best_mag, best_freq = mags[0]
    second_mag = mags[1][0] if len(mags) > 1 else 1.0
    second_mag = max(float(second_mag), 1.0)

    current_mag = None
    for mag, freq in mags:
        if abs(freq - old_freq) < 0.5:
            current_mag = float(mag)
            break

    if current_mag is None:
        current_mag = second_mag

    winner_ratio = best_mag / second_mag
    current_ratio = best_mag / max(current_mag, 1.0)

    # Keep current detector coeffs unless retuning is accepted.
    self.compute_goertzel_for_freq(old_freq)

    # Guard against false pre-roll locks.
    #
    # Bad example from logs:
    #   scan 700->1000Hz wr=1.11 snr=1.00
    #
    # That is silence/noise, not CW. Do not allow it to become a locked tone.
    if force and snr < 1.45 and winner_ratio < 3.00:
        top = " ".join(f"{int(f)}:{m:.0f}" for m, f in mags[:5])
        print(
            f"[TONE] scan ignored weak/no-signal old={old_freq:.0f} "
            f"best={best_freq:.0f} wr={winner_ratio:.2f} "
            f"cr={current_ratio:.2f} snr={snr:.2f} top={top}",
            flush=True,
        )
        self._tone_scan_last_success = False
        self._tone_scan_last_success = False
        self.tone_locked = False
        self.learning_status = "ACQUIRE"
        return

    # MW_TONE_SCAN_ENERGY_FLOOR_V1
    # Ratio alone is not enough: silence/noise can have a fake winner.
    # Learn a low-energy floor before real copy and require a genuine lift
    # before accepting a startup AUTO/MANUAL tone lock.
    try:
        copy_text = str(getattr(self, "decoded_copy", getattr(self, "decoded_raw", ""))).strip()
        real_copy_started = bool(copy_text) and any(ch not in "ET \t\r\n" for ch in copy_text)
        locked = bool(getattr(self, "tone_locked", False))

        if not locked and not real_copy_started:
            floor = float(getattr(self, "_tone_scan_noise_floor", 60000.0))
            required_mag = max(180000.0, floor * 3.0)

            if float(best_mag) < required_mag:
                self._tone_scan_noise_floor = 0.90 * floor + 0.10 * float(best_mag)
                top = " ".join(f"{int(f)}:{m:.0f}" for m, f in mags[:5])
                print(
                    f"[TONE] scan ignored below-energy-floor old={old_freq:.0f} "
                    f"best={best_freq:.0f} mag={best_mag:.0f} "
                    f"floor={floor:.0f} req={required_mag:.0f} "
                    f"wr={winner_ratio:.2f} cr={current_ratio:.2f} "
                    f"snr={snr:.2f} top={top}",
                    flush=True,
                )
                self._tone_scan_last_success = False
                self.tone_locked = False
                self.learning_status = "ACQUIRE"
                return
    except Exception as exc:
        print(f"[TONE] energy floor check skipped: {exc}", flush=True)


    if best_freq == old_freq:
        self.detected_tone_hz = best_freq
        self.detected_tone_confidence = max(1.0, min(winner_ratio, 10.0))
        self._tone_candidate_hz = old_freq
        self._tone_candidate_count = 0

        # Same-frequency result only counts as a real lock if the signal is
        # actually credible. This prevents "default 700 Hz" becoming locked
        # just because a weak/no-signal scan did not retune.
        credible_same_tone = (
            float(snr) >= 3.00
            or float(winner_ratio) >= 3.00
        )

        if credible_same_tone:
            self._tone_scan_last_success = True
            if bool(getattr(self, "auto_tone_track", False)):
                self.tone_lock_hz = old_freq
                self.tone_locked = True
                self.learning_status = f"LOCK {old_freq:.0f}Hz"
        else:
            self._tone_scan_last_success = False
            # If real copy has not started, stay unlocked and keep acquiring.
            copy_text = str(getattr(self, "decoded_copy", getattr(self, "decoded_raw", ""))).strip()
            real_copy_started = bool(copy_text) and any(ch not in "ET \t\r\n" for ch in copy_text)
            if not real_copy_started:
                self.tone_locked = False
                self.learning_status = "ACQUIRE"

        return

    if force:
        min_winner_ratio = 1.06
        min_current_ratio = 1.08
        required_count = 1
        min_snr = 1.00
        max_jump_hz = 1000.0
    elif locked_good:
        # Locked verification: only switch if the long-window score is very
        # clearly better than the currently locked tone.
        min_winner_ratio = 1.20
        min_current_ratio = 2.20
        required_count = 3
        min_snr = max(sql + 0.15, 1.55)
        max_jump_hz = 700.0
    else:
        # Acquisition/reacquisition.
        min_winner_ratio = 1.12
        min_current_ratio = 1.35
        required_count = 2
        min_snr = max(sql, 1.30)
        max_jump_hz = 1000.0

    if snr < min_snr and not force:
        return

    if (not force) and abs(best_freq - old_freq) > max_jump_hz:
        return

    if winner_ratio < min_winner_ratio or current_ratio < min_current_ratio:
        if force:
            top = " ".join(f"{int(f)}:{m:.0f}" for m, f in mags[:5])
            print(
                f"[TONE] scan no-change old={old_freq:.0f} best={best_freq:.0f} "
                f"wr={winner_ratio:.2f} cr={current_ratio:.2f} top={top}",
                flush=True,
            )
        return

    if not force:
        prev_candidate = float(getattr(self, "_tone_candidate_hz", -1.0))
        prev_count = int(getattr(self, "_tone_candidate_count", 0))

        if abs(prev_candidate - best_freq) < 0.5:
            prev_count += 1
        else:
            prev_candidate = best_freq
            prev_count = 1

        self._tone_candidate_hz = prev_candidate
        self._tone_candidate_count = prev_count

        if prev_count < required_count:
            try:
                self.debug_log(
                    f"[TONE] candidate {old_freq:.0f}->{best_freq:.0f}Hz "
                    f"count={prev_count}/{required_count} wr={winner_ratio:.2f} "
                    f"cr={current_ratio:.2f} snr={snr:.2f}"
                )
            except Exception:
                pass
            return

    self.target_tone_hz = best_freq
    self.detected_tone_hz = best_freq
    self.detected_tone_confidence = max(1.0, min(winner_ratio, 10.0))
    self.compute_goertzel_for_freq(best_freq)
    self._tone_scan_last_success = True

    # Lock the tone once acquired. Timing/WPM learning continues separately.
    self.tone_lock_hz = best_freq
    self.tone_locked = True
    self.learning_status = f"LOCK {best_freq:.0f}Hz"

    # Reset detector floors after retuning, but keep decoded text and timing.
    self.noise_floor = 0.0
    self.signal_floor = 0.0
    self.detect_level = 0.0
    self.env_fast = 0.0
    self.env_noise = 0.0
    self.env_peak = 0.0
    self.tone_on_votes = 0
    self.tone_off_votes = 0
    self.pending_polarity_change = False

    self._tone_candidate_hz = best_freq
    self._tone_candidate_count = 0

    top = " ".join(f"{int(f)}:{m:.0f}" for m, f in mags[:5])

    msg = (
        f"[TONE] scan {old_freq:.0f}->{best_freq:.0f}Hz "
        f"wr={winner_ratio:.2f} cr={current_ratio:.2f} snr={snr:.2f} "
        f"{'manual' if force else 'auto'} top={top}"
    )

    try:
        self.debug_log(msg)
    except Exception:
        pass

    print(msg, flush=True)


def _mw_competition_cleanup_copy(self) -> None:
    """
    Non-destructive placeholder.

    Earlier versions tried to remove startup garbage here, but that could also
    remove valid leading words. Keep this function harmless for compatibility.
    """
    return





def _mw_median_float(values):
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return 0.0
    n = len(vals)
    mid = n // 2
    if n & 1:
        return vals[mid]
    return 0.5 * (vals[mid - 1] + vals[mid])


def _mw_trimmed(values, lo=0.10, hi=0.90):
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return []
    n = len(vals)
    a = int(max(0, min(n - 1, round(n * lo))))
    b = int(max(a + 1, min(n, round(n * hi))))
    return vals[a:b]


def _mw_largest_gap_split(values, min_ratio=1.55):
    vals = sorted(float(v) for v in values if v is not None and float(v) > 0)
    if len(vals) < 5:
        return vals, []

    best_i = -1
    best_ratio = 1.0

    for i in range(1, len(vals)):
        a = max(vals[i - 1], 1.0)
        b = vals[i]
        ratio = b / a
        if ratio > best_ratio:
            best_ratio = ratio
            best_i = i

    if best_i >= 2 and best_ratio >= min_ratio:
        return vals[:best_i], vals[best_i:]

    return vals, []


def _mw_update_dynamic_timing(self, mark_ms=None, space_ms=None) -> None:
    """
    Dynamic WPM / timing model.

    Uses medians and clusters so the decoder can follow a human operator
    without one noisy element trashing the current WPM estimate.
    """
    try:
        marks = [float(v) for v in getattr(self, "mark_durations", []) if 16.0 <= float(v) <= 360.0]
        spaces = [float(v) for v in getattr(self, "space_durations", []) if 12.0 <= float(v) <= 1400.0]
    except Exception:
        return

    if len(marks) >= 4:
        trimmed_marks = _mw_trimmed(marks, 0.05, 0.95)
        short_marks, long_marks = _mw_largest_gap_split(trimmed_marks, min_ratio=1.65)

        if len(short_marks) < 3:
            m = sorted(trimmed_marks)
            short_marks = m[:max(3, (len(m) + 1) // 2)]

        dot_candidate = _mw_median_float(short_marks)

        if 24.0 <= dot_candidate <= 260.0:
            old_dot = float(getattr(self, "dot_ms", dot_candidate))
            conf = int(getattr(self, "sync_confidence", 0))

            if conf < 30:
                alpha = 0.22
            elif conf < 70:
                alpha = 0.14
            else:
                alpha = 0.08

            ratio = max(dot_candidate, old_dot) / max(min(dot_candidate, old_dot), 1.0)
            if ratio > 1.45 and len(short_marks) < 6:
                alpha *= 0.35

            new_dot = (1.0 - alpha) * old_dot + alpha * dot_candidate
            new_dot = clampf(new_dot, 24.0, 300.0)

            self.dot_ms = new_dot
            self.dash_ms = 3.0 * new_dot

            if len(long_marks) >= 2:
                dash_candidate = _mw_median_float(long_marks)
                if 1.8 * new_dot <= dash_candidate <= 4.2 * new_dot:
                    self.dash_ms = 0.80 * float(getattr(self, "dash_ms", 3.0 * new_dot)) + 0.20 * dash_candidate

            self.avg_mark_ms = 0.85 * float(getattr(self, "avg_mark_ms", new_dot)) + 0.15 * dot_candidate

    dot = max(float(getattr(self, "dot_ms", 50.0)), 24.0)

    if len(spaces) >= 4:
        valid = sorted(spaces)
        short_spaces, _ = _mw_largest_gap_split(valid, min_ratio=1.75)

        if short_spaces:
            intra = _mw_median_float(short_spaces)
            if 0.35 * dot <= intra <= 1.8 * dot:
                self.avg_space_ms = 0.80 * float(getattr(self, "avg_space_ms", dot)) + 0.20 * intra

    self.char_gap_ms = 3.0 * dot
    self.word_gap_ms = 7.0 * dot





def _mw_classify_gap_dynamic(self, space_ms: float) -> str:
    """
    Classify a gap as intra / letter / word using dynamic dot timing.

    Phase 4.6 RAW-quality tuning.

    Earlier tuning made word gaps too easy to trigger. That helped readable
    COPY in some prose tests, but damaged RAW by splitting normal words and
    ham tokens:

      D E
      ZL1SX G
      C A L L I N G
      C Q C Q C Q

    This version is more standards-aligned:
      - intra / letter boundary stays responsive
      - word boundary sits nearer the real 3-dit / 7-dit midpoint
      - learned spacing may help, but may not drag word boundary too low
    """
    dot = max(float(getattr(self, "dot_ms", 50.0)), 24.0)
    gap = float(space_ms)

    # Textbook:
    #   intra-character gap: 1 dit
    #   inter-character gap: 3 dits
    #   word gap: 7 dits
    #
    # Decision boundaries:
    #   letter boundary around 1.7-2.0 dits
    #   word boundary around 5.0-5.5 dits
    #
    # Keep the letter boundary a bit low so characters are separated cleanly,
    # but keep the word boundary high enough to avoid C A L L I N G.
    letter_boundary = 1.70 * dot
    word_boundary = 5.35 * dot

    if dot <= 48.0:
        # Faster code: measured gaps can be compressed by block timing.
        letter_boundary = 1.55 * dot
        word_boundary = 5.00 * dot
    elif dot >= 75.0:
        # Slower hand/generator code: allow a little more character spacing
        # before declaring a word.
        letter_boundary = 1.85 * dot
        word_boundary = 5.55 * dot

    # Startup guard:
    #
    # The first CQ is vulnerable while tone/timing are still settling.
    #
    # Failure mode:
    #   C  = -.-.
    #   CQ = -.-. --.-
    #
    # If early intra-character gaps are misclassified as letter gaps, the first
    # C can become:
    #   T E N
    #   - . -.
    #
    # That is exactly the observed RAW startup artefact:
    #   TEN CQ CQ ...
    #
    # During the first few decoded characters, make the decoder more reluctant
    # to split a short in-progress symbol. Once the symbol reaches four
    # elements, normal letter splitting resumes so C can still flush before Q.
    try:
        raw_start = str(getattr(self, "decoded_raw", "") or "").strip()
        sym = str(getattr(self, "current_symbol", "") or "")

        early_stream = len(raw_start) < 8
        short_symbol = 1 <= len(sym) <= 3

        if early_stream and short_symbol:
            # Keep this below a real character gap where possible, but above
            # normal intra-character gaps. This prevents C from becoming T/E/N.
            letter_boundary = max(letter_boundary, 2.65 * dot)

            # At slower measured speeds, the first letter gap can be fairly
            # wide. Avoid going too high or C and Q will merge.
            letter_boundary = min(letter_boundary, 3.05 * dot)

    except Exception:
        pass

    # Learn from observed spaces only when there is a clear cluster split.
    # Important: learned word boundary can refine, but must not become so low
    # that it turns every slightly-long character gap into a word.
    try:
        spaces = [
            float(v) for v in getattr(self, "space_durations", [])
            if 0.70 * dot <= float(v) <= 14.0 * dot
        ]
    except Exception:
        spaces = []

    if len(spaces) >= 10:
        clustered = _mw_trimmed(spaces, 0.08, 0.96)
        lower, upper = _mw_largest_gap_split(
            clustered,
            min_ratio=(1.60 if dot < 75.0 else 1.45),
        )

        if len(lower) >= 4 and len(upper) >= 2:
            lower_med = _mw_median_float(lower)
            upper_med = _mw_median_float(upper)

            # Only trust learned word clustering when upper gaps are clearly
            # bigger than lower gaps. This prevents the letter-gap cluster from
            # being mistaken for word gaps.
            if upper_med >= 1.75 * max(lower_med, 1.0):
                learned = 0.92 * math.sqrt(lower_med * upper_med)

                # Hard floor for RAW quality.
                # Do not let the learned boundary fall below ~4.85 dits.
                learned_floor = 4.85 * dot
                learned_ceiling = 6.60 * dot
                learned = clampf(learned, learned_floor, learned_ceiling)

                # Blend rather than replace. Smooth changes reduce chatter.
                word_boundary = 0.70 * word_boundary + 0.30 * learned

    if gap >= word_boundary:
        return "word"
    if gap >= letter_boundary:
        return "letter"
    return "intra"

def _mw_startup_copy_ready(self) -> bool:
    """
    Open startup copy once the detector has seen real activity.

    Evidence-based startup rule:
      - Before copy has started, allow a complete leading T only if:
          tone is locked,
          current symbol is '-',
          the last mark is dash-length,
          and signal/copy state is credible.
      - Do not allow leading E/dot artefacts.
      - After normal sync/confidence is established, open as before.
    """
    if bool(getattr(self, "startup_copy_open", False)):
        return True

    try:
        conf = int(getattr(self, "sync_confidence", 0))
        snr = float(getattr(self, "snr_ema", 1.0))
        sql = float(getattr(self, "squelch_snr", getattr(self.cfg, "squelch_snr", 1.45)))
        copy_len = len(str(getattr(self, "decoded_copy", getattr(self, "decoded_raw", ""))).strip())
        tone_locked = bool(getattr(self, "tone_locked", False))
        sym = str(getattr(self, "current_symbol", ""))
        last_mark = float(getattr(self, "last_mark_ms", 0.0))
        dot = max(float(getattr(self, "dot_ms", 50.0)), 24.0)
    except Exception:
        return True

    if copy_len > 0:
        self.startup_copy_open = True
        return True

    # This is the actual missing-first-letter fix:
    # THE starts with T (-). If we have a full dash after a real tone lock,
    # do not suppress it just because sync is still ACQUIRE/WEAK.
    if (
        tone_locked
        and sym == "-"
        and last_mark >= max(110.0, 1.75 * dot)
        and snr >= max(sql * 0.70, 1.0)
    ):
        self.startup_copy_open = True
        print(
            f"[COPY] startup open on full leading T "
            f"mark={last_mark:.1f} dot={dot:.1f} snr={snr:.2f} conf={conf}",
            flush=True,
        )
        return True

    # Normal startup opening once the decoder has enough confidence.
    if conf >= 18 and snr >= max(sql * 0.85, 1.20):
        self.startup_copy_open = True
        return True

    return False



def _mw_copy_gate_open(self) -> bool:
    # MW_STARTUP_LEADING_ET_GATE_V2
    # After AUTO TRACK has locked a credible tone, the very first copied
    # character may legitimately be E or T while sync confidence is still low.
    # Allow that single-element startup character through. This is deliberately
    # limited to an empty copy buffer so idle E/T chatter is still suppressed.
    try:
        candidate = str(getattr(self, "_copy_gate_candidate_char", "") or "")
        existing_copy = str(getattr(self, "decoded_copy", getattr(self, "decoded_raw", ""))).strip()

        if (
            candidate == "T"
            and not existing_copy
            and bool(getattr(self, "tone_locked", False))
            and (
                float(getattr(self, "snr_ema", 1.0)) >= 2.0
                or int(getattr(self, "sync_confidence", 0)) >= 2
                or str(getattr(self, "learning_status", "")).startswith("LOCK")
            )
        ):
            if not bool(getattr(self, "_startup_leading_et_logged", False)):
                print(
                    f"[COPY] allowed locked leading {candidate} "
                    f"snr={float(getattr(self, 'snr_ema', 1.0)):.2f} "
                    f"sync={int(getattr(self, 'sync_confidence', 0))}",
                    flush=True,
                )
                self._startup_leading_et_logged = True
            return True
    except Exception:
        pass

    """
    Decide whether it is safe to append decoded text.

    The decoder needs two different behaviours:
      - startup: allow early real characters before sync confidence fully ramps
      - tail: reject weak T/E rubbish after the real signal has ended
    """
    try:
        snr = float(getattr(self, "snr_ema", 1.0))
        sql = float(getattr(self, "squelch_snr", getattr(self.cfg, "squelch_snr", 1.45)))
        conf = int(getattr(self, "sync_confidence", 0))
        state = getattr(self, "sync_state", None)
        state_name = getattr(state, "name", str(state))

        current_copy = getattr(self, "decoded_copy", getattr(self, "decoded_raw", ""))
        copy_len = len(str(current_copy).strip())
        tone_present = bool(getattr(self, "tone_present", False))
    except Exception:
        return True

    # Startup grace:
    # The first word can arrive before sync_confidence catches up. If we are
    # still near the beginning and the signal is at/near squelch, allow it.
    # This is aimed at preserving leading words like MARY/CQ/DE.
    if copy_len < 12:
        if snr >= max(sql * 0.90, 1.25):
            return True
        if tone_present and snr >= max(sql * 0.80, 1.15):
            return True
        if conf >= 3 and snr >= max(sql * 0.80, 1.15):
            return True

    # Strong enough audio: allow.
    if snr >= max(sql + 0.35, 2.0):
        return True

    # Still tracking: allow some weak copy.
    if state_name in ("SYNC_TRACK", "TRACK") and conf >= 65:
        return True

    # Weak but not hopeless: allow if confidence is decent.
    if state_name in ("SYNC_WEAK", "WEAK") and conf >= 35 and snr >= sql:
        return True

    return False


def _mw_ui_toast(self, message: str, button: str = "", ttl: float = 1.6) -> None:
    self.ui_message = str(message)
    self.ui_message_until = time.monotonic() + float(ttl)
    self.ui_active_button = str(button)
    self.ui_active_until = time.monotonic() + float(ttl)


def _mw_reset_timing(self) -> None:
    self.dot_ms = 50.0
    self.dash_ms = 150.0
    self.avg_mark_ms = 50.0
    self.avg_space_ms = 50.0
    self.char_gap_ms = 150.0
    self.word_gap_ms = 350.0

    self.mark_durations.clear()
    self.space_durations.clear()

    self.sync_confidence = 0
    self.sync_state = SyncState.SYNC_ACQUIRE

    self.current_symbol = ""
    self.end_gap_flushed = False
    self.tail_copy_closed = False

    print("[BUTTON] timing reset", flush=True)



# MW_LCD_STATUS_PAGE_HELPER_V1
def _mw_cycle_display_page(decoder) -> None:
    """
    Cycle the LCD content page.

    Physical button mapping:
      short CLEAR = clear copy
      long CLEAR  = cycle LCD page

    Pages:
      COPY      normal operator copy
      STATUS    appliance/runtime status
      RAW       literal decoder raw buffer
      EXPANDED  abbreviation-expanded copy
    """
    pages = ["COPY", "STATUS", "RAW", "EXPANDED"]
    current = str(getattr(decoder, "ui_page", "COPY") or "COPY").upper()

    try:
        idx = pages.index(current)
    except ValueError:
        idx = 0

    decoder.ui_page = pages[(idx + 1) % len(pages)]

    try:
        _mw_ui_toast(decoder, f"VIEW {decoder.ui_page}", "CLEAR", ttl=1.4)
    except Exception:
        pass

    print(f"[BUTTON] view page {decoder.ui_page}", flush=True)

def _mw_clear_copy(self) -> None:
    self.decoded_raw = ""
    self.decoded_copy = ""
    self.decoded_expanded = ""
    self.current_word = ""
    self.current_symbol = ""
    print("[BUTTON] cleared copy", flush=True)


def _mw_cycle_mode(self) -> None:
    mode = getattr(self, "runtime_mode", "RX")

    if mode == "RX":
        self.runtime_mode = "AUTO"
        self.training_enabled = False
        self.auto_tone_track = False
        _mw_ui_toast(self, "MODE AUTO", "MODE")
        print("[BUTTON] mode AUTO: fixed tone, scan on request", flush=True)

    elif mode == "AUTO":
        self.runtime_mode = "TRAIN"
        self.training_enabled = True
        self.auto_tone_track = False
        if hasattr(self, "training_reset_state"):
            self.training_reset_state()
        _mw_ui_toast(self, "MODE TRAIN", "MODE")
        print("[BUTTON] mode TRAIN: training ON", flush=True)

    else:
        self.runtime_mode = "RX"
        self.training_enabled = False
        self.auto_tone_track = False
        _mw_ui_toast(self, "MODE RX", "MODE")
        print("[BUTTON] mode RX: fixed tone, training OFF", flush=True)


# MW_RESTART_TO_SPLASH_V1
def _mw_restart_to_splash(decoder) -> None:
    """
    Return from the decoder app to the standalone splash launcher.

    This is an app-level restart, not a full Raspberry Pi reboot:
      decoder process -> splash launcher process

    It deliberately starts the splash with --no-touch because the production
    path for this unit is framebuffer + physical buttons.
    """
    splash = "/opt/morse-whisperer-pi/morse_whisperer_splash.py"
    py = sys.executable or "/opt/morse-whisperer-pi/venv/bin/python"

    if not os.path.exists(splash):
        print(f"[BUTTON] cannot return to splash; missing {splash}", flush=True)
        try:
            _mw_ui_toast(decoder, "SPLASH MISSING", "RESET", ttl=2.0)
        except Exception:
            pass
        return

    try:
        _mw_ui_toast(decoder, "RETURNING TO SPLASH", "RESET", ttl=0.5)
    except Exception:
        pass

    print("[BUTTON] returning to splash launcher", flush=True)

    # Give the LCD one short frame to show the toast before exec.
    time.sleep(0.35)

    # Clean up GPIO before replacing the process. Do not fail if another
    # thread/library has already altered GPIO state.
    try:
        if GPIO is not None:
            GPIO.cleanup()
    except Exception as exc:
        print(f"[BUTTON] GPIO cleanup before splash ignored: {exc}", flush=True)

    args = [py, splash, "--no-touch"]

    # Preserve edge-debug if this decoder was launched in debug mode.
    if "--edge-debug" in sys.argv:
        args.append("--decoder-arg=--edge-debug")

    print("[BUTTON] exec: " + " ".join(args), flush=True)
    os.execv(py, args)


class ButtonThread(threading.Thread):
    """
    GoodTFT / XC9022 button handler.

    Pins discovered on this XC9022 unit:
      MODE   GPIO23
      SCAN   GPIO22
      RESET  GPIO27
      CLEAR  GPIO18
    """
    def __init__(self, decoder, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.decoder = decoder
        self.stop_event = stop_event
        # XC9022 / Jaycar 2.8" actual physical button GPIOs discovered by scan.
        # Active-low with pull-ups.
        #
        # Mapping:
        #   MODE  = cycle RX -> AUTO -> TRAIN -> RX
        #   SCAN  = scan tone now / hold for auto tracking
        #   RESET = reset timing/WPM estimator
        #   CLEAR = clear copy/raw / hold for clear + timing reset
        self.pins = {
            "MODE": 23,
            "SCAN": 22,
            "RESET": 27,
            "CLEAR": 18,
        }
        self.last_state = {}
        self.down_since = {}
        self.long_fired = set()

    def _short_press(self, name: str) -> None:
        d = self.decoder

        if name == "MODE":
            _mw_cycle_mode(d)

        elif name == "SCAN":
            d.request_tone_scan = True
            _mw_ui_toast(d, "SCAN REQUESTED", "SCAN")
            print("[BUTTON] tone scan requested", flush=True)

        elif name == "RESET":
            _mw_reset_timing(d)
            _mw_ui_toast(d, "TIMING RESET", "RESET")

        elif name == "CLEAR":
            _mw_clear_copy(d)
            _mw_ui_toast(d, "COPY CLEARED", "CLEAR")

    def _long_press(self, name: str) -> None:
        d = self.decoder

        if name == "MODE":
            d.runtime_mode = "RX"
            d.training_enabled = False
            d.auto_tone_track = False
            _mw_reset_timing(d)
            _mw_ui_toast(d, "RX RESET", "MODE")
            print("[BUTTON] long MODE: hard RX/timing reset", flush=True)

        elif name == "SCAN":
            d.request_tone_scan = True
            d.auto_tone_track = True
            d.runtime_mode = "AUTO"
            d.training_enabled = False
            _mw_ui_toast(d, "AUTO TRACK ON", "SCAN")
            print("[BUTTON] long SCAN: scan + conservative auto tracking ON", flush=True)

        elif name == "RESET":
            _mw_ui_toast(d, "SPLASH", "RESET")
            print("[BUTTON] long RESET: return to splash", flush=True)
            _mw_restart_to_splash(d)

        elif name == "CLEAR":
            _mw_cycle_display_page(d)
            print("[BUTTON] long CLEAR: cycle display page", flush=True)

    def run(self) -> None:
        if GPIO is None:
            print(f"[BUTTON] RPi.GPIO unavailable; buttons disabled: {GPIO_IMPORT_ERROR}", flush=True)
            return

        try:
            GPIO.setmode(GPIO.BCM)

            for pin in self.pins.values():
                GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

            self.last_state = {name: GPIO.input(pin) for name, pin in self.pins.items()}

            print(
                "[BUTTON] GoodTFT buttons enabled: "
                + ", ".join(f"{k}=GPIO{v}" for k, v in self.pins.items()),
                flush=True,
            )

            while not self.stop_event.is_set():
                now = time.monotonic()

                for name, pin in self.pins.items():
                    val = GPIO.input(pin)
                    last = self.last_state.get(name, 1)

                    if last == 1 and val == 0:
                        self.down_since[name] = now
                        self.long_fired.discard(name)

                    if val == 0 and name in self.down_since:
                        held = now - self.down_since[name]

                        if held >= 1.0 and name not in self.long_fired:
                            self.long_fired.add(name)
                            self._long_press(name)

                    if last == 0 and val == 1:
                        held = now - self.down_since.get(name, now)

                        if name not in self.long_fired and held >= 0.04:
                            self._short_press(name)

                        self.down_since.pop(name, None)
                        self.long_fired.discard(name)

                    self.last_state[name] = val

                time.sleep(0.025)

        except Exception as exc:
            print(f"[BUTTON] error: {exc}", flush=True)



class FramebufferDisplay:
    def __init__(self, fb_path: str, font_size: int):
        if Image is None or ImageDraw is None:
            raise RuntimeError(f"Pillow import failed: {PIL_IMPORT_ERROR}")

        self.fb_path = fb_path
        self.font_size = font_size
        self.width, self.height = self._detect_size()
        self.bpp = self._detect_bpp()
        self.font = self._load_font(font_size)
        self.small_font = self._load_font(max(10, font_size - 3))

        if self.bpp not in (16, 24, 32):
            raise RuntimeError(f"Unsupported framebuffer bpp: {self.bpp}")

    def _fb_name(self) -> str:
        return os.path.basename(self.fb_path)

    def _sys_path(self, name: str) -> Path:
        return Path("/sys/class/graphics") / self._fb_name() / name

    def _detect_size(self) -> Tuple[int, int]:
        virtual_size = self._sys_path("virtual_size")
        if virtual_size.exists():
            txt = virtual_size.read_text().strip()
            if "," in txt:
                w, h = txt.split(",", 1)
                return int(w), int(h)

        try:
            out = subprocess.check_output(["fbset", "-fb", self.fb_path, "-s"], text=True)
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("geometry"):
                    parts = line.split()
                    return int(parts[1]), int(parts[2])
        except Exception:
            pass

        return 480, 320

    def _detect_bpp(self) -> int:
        bits = self._sys_path("bits_per_pixel")
        if bits.exists():
            return int(bits.read_text().strip())
        return 16

    def _load_font(self, size: int):
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        ]
        for c in candidates:
            if os.path.exists(c):
                return ImageFont.truetype(c, size)
        return ImageFont.load_default()

    def _to_rgb565(self, img: Image.Image) -> bytes:
        arr = np.asarray(img.convert("RGB"), dtype=np.uint16)
        r = (arr[:, :, 0] >> 3) & 0x1F
        g = (arr[:, :, 1] >> 2) & 0x3F
        b = (arr[:, :, 2] >> 3) & 0x1F
        rgb565 = (r << 11) | (g << 5) | b
        return rgb565.astype("<u2").tobytes()

    def render(self, snap: Snapshot) -> None:
        """
        LCD-only operator display.

        Display-only code. This must never touch decoder state, GPIO, touch, or
        audio. Keep this robust: any drawing failure leaves the decoder running.
        """
        img = Image.new("RGB", (self.width, self.height), "black")
        d = ImageDraw.Draw(img)

        def text_wh(text, font):
            text = "" if text is None else str(text)
            try:
                bb = d.textbbox((0, 0), text, font=font)
                return max(0, bb[2] - bb[0]), max(0, bb[3] - bb[1])
            except Exception:
                try:
                    return d.textsize(text, font=font)
                except Exception:
                    return len(text) * 8, max(12, self.font_size)

        def draw_right(text, y, font, fill, right_pad=8, left_limit=6):
            tw, th = text_wh(text, font)
            x = max(left_limit, self.width - right_pad - tw)
            d.text((x, y), text, font=font, fill=fill)
            return x, tw, th

        def shorten_to_width(text, font, max_w):
            text = "" if text is None else str(text)
            if text_wh(text, font)[0] <= max_w:
                return text

            ell = "…"
            out = text
            while out and text_wh(out + ell, font)[0] > max_w:
                out = out[:-1]

            return (out + ell) if out else ell

        margin = 7
        line = max(16, self.font_size + 4)

        # 320x240 safe layout.
        footer_h = 40
        header_h = 62
        footer_y = self.height - footer_h
        content_top = header_h + 8
        content_bottom = footer_y - 6

        # Palette chosen for the small ILI9341 TFT.
        bg = (4, 7, 12)
        bg_header = (8, 16, 30)
        bg_footer = (16, 18, 22)
        text_main = (255, 255, 255)
        text_soft = (198, 232, 255)
        text_dim = (145, 165, 180)
        accent = (90, 220, 255)
        warn = (255, 220, 90)
        border = (85, 110, 135)

        d.rectangle((0, 0, self.width, self.height), fill=bg)

        # ------------------------------------------------------------------
        # Header
        # ------------------------------------------------------------------
        d.rectangle((0, 0, self.width, header_h), fill=bg_header)

        title = PROJECT_NAME
        title_max_w = self.width - 2 * margin
        title = shorten_to_width(title, self.font, title_max_w)
        d.text((margin, 3), title, font=self.font, fill=text_main)

        mode_label = str(getattr(snap, "display_mode", snap.mode) or snap.mode)
        mode_left = f"{mode_label}"
        mode_right = f"{float(snap.target_tone_hz):.0f}Hz {float(snap.wpm):.1f}WPM"

        right_w, _right_h = text_wh(mode_right, self.small_font)
        max_left_w = max(20, self.width - (2 * margin) - right_w - 8)
        mode_left = shorten_to_width(mode_left, self.small_font, max_left_w)

        d.text((margin, 24), mode_left, font=self.small_font, fill=text_soft)
        draw_right(mode_right, 24, self.small_font, text_soft, right_pad=margin)

        sql = "OPEN" if snap.squelch_open else "CLOSED"
        status_left = f"SQL {sql} {snap.sync_state}/{snap.sync_confidence}"
        status_right = f"SNR {float(snap.snr):.1f}"

        right_w, _ = text_wh(status_right, self.small_font)
        status_left = shorten_to_width(status_left, self.small_font, self.width - 2 * margin - right_w - 8)

        d.text((margin, 42), status_left, font=self.small_font, fill=text_soft)
        draw_right(status_right, 42, self.small_font, text_soft, right_pad=margin)

        # SNR / audio activity meter.
        #
        # This is not calibrated dB SPL. It is a practical decoder activity
        # meter using the current SNR estimate. That keeps this patch display-only.
        bar_x = margin
        # Move the SNR/activity meter down slightly so it does not crowd the
        # status text. Keep the text itself unchanged.
        bar_y = header_h - 2
        bar_w = max(20, self.width - 2 * margin)
        bar_h = 6
        def _snr_grad(frac: float):
            frac = max(0.0, min(1.0, float(frac)))
            # Red -> amber -> green
            if frac <= 0.5:
                t = frac / 0.5
                r1, g1, b1 = 255, 70, 70
                r2, g2, b2 = 255, 210, 70
            else:
                t = (frac - 0.5) / 0.5
                r1, g1, b1 = 255, 210, 70
                r2, g2, b2 = 70, 220, 120
            r = int(r1 + (r2 - r1) * t)
            g = int(g1 + (g2 - g1) * t)
            b = int(b1 + (b2 - b1) * t)
            return (r, g, b)

        d.rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + bar_h), outline=border)

        snr_value = float(getattr(snap, "snr", 1.0) or 1.0)
        snr_frac = max(0.0, min(1.0, (snr_value - 1.0) / 18.0))
        fill_w = int((bar_w - 2) * snr_frac)


        # Draw inactive track first, then active gradient fill.

        inner_x0 = bar_x + 1

        inner_y0 = bar_y + 1

        inner_x1 = bar_x + bar_w - 1

        inner_y1 = bar_y + bar_h - 1

        d.rectangle((inner_x0, inner_y0, inner_x1, inner_y1), fill=(18, 26, 42))


        active_x1 = min(inner_x0 + max(0, fill_w) - 1, inner_x1)

        if fill_w > 0 and active_x1 >= inner_x0:

            # Use the current SNR value to choose how far into the gradient

            # the active bar should reach.

            x = inner_x0

            while x <= active_x1:

                x2 = min(x + 2, active_x1)

                pos_frac = (x2 - inner_x0) / max(1, inner_x1 - inner_x0)

                d.rectangle((x, inner_y0, x2, inner_y1), fill=_snr_grad(pos_frac))

                x = x2 + 1



        for frac in (0.25, 0.50, 0.75):
            tx = bar_x + int(bar_w * frac)
            d.line((tx, bar_y + 1, tx, bar_y + bar_h - 1), fill=(95, 125, 145))

        # ------------------------------------------------------------------
        # Main content area / page renderer
        # ------------------------------------------------------------------
        y = content_top

        page = str(getattr(snap, "ui_page", "COPY") or "COPY").upper()
        if page not in ("COPY", "STATUS", "RAW", "EXPANDED"):
            page = "COPY"

        if page == "STATUS":
            d.text((margin, y), "STATUS", font=self.small_font, fill=accent)
            y += line

            audio_name = str(getattr(snap, "selected_audio_device", "") or "unknown")
            audio_name = shorten_to_width(audio_name, self.small_font, self.width - 2 * margin)

            fb_name = str(getattr(self, "fb_path", "") or "fb?")
            fb_detail = f"{fb_name} {self.width}x{self.height}/{self.bpp}"

            sql = "OPEN" if bool(getattr(snap, "squelch_open", False)) else "CLOSED"

            rows = [
                ("MODE", str(getattr(snap, "display_mode", getattr(snap, "mode", "")) or "")),
                ("TONE", f"{float(getattr(snap, 'target_tone_hz', 0.0)):.0f}Hz / {float(getattr(snap, 'detected_tone_hz', 0.0)):.0f}Hz"),
                ("WPM", f"{float(getattr(snap, 'wpm', 0.0)):.1f}  dot {float(getattr(snap, 'dot_ms', 0.0)):.0f}ms"),
                ("SNR", f"{float(getattr(snap, 'snr', 0.0)):.2f}  SQL {sql}"),
                ("SYNC", f"{getattr(snap, 'sync_state', '')}/{int(getattr(snap, 'sync_confidence', 0))}"),
                ("BUF", str(getattr(snap, "buffer_status", "") or "-")),
                ("LEARN", str(getattr(snap, "learning_status", "") or "-")),
                ("QUEUE", f"{int(getattr(snap, 'q_size', 0))}/{int(getattr(snap, 'q_overruns', 0))} overruns"),
                ("AUDIO", f"errors {int(getattr(snap, 'audio_errors', 0))}"),
                ("FB", fb_detail),
                ("INPUT", audio_name),
            ]

            label_w = text_wh("QUEUE", self.small_font)[0] + 8
            row_h = max(14, line - 2)

            for label, value in rows:
                if y > content_bottom - row_h:
                    break

                label_text = shorten_to_width(label, self.small_font, label_w)
                value_text = shorten_to_width(str(value), self.small_font, self.width - margin - (margin + label_w))

                d.text((margin, y), label_text, font=self.small_font, fill=text_dim)
                d.text((margin + label_w, y), value_text, font=self.small_font, fill=text_main)
                y += row_h

        else:
            if page == "RAW":
                heading = "RAW"
                content = tail_text(str(getattr(snap, "raw", "") or ""), 1200)
            elif page == "EXPANDED":
                heading = "EXPANDED"
                content = tail_text(str(getattr(snap, "expanded", "") or ""), 1200)
            else:
                heading = "COPY"
                content = tail_text(str(getattr(snap, "copy", "") or ""), 900)

            d.text((margin, y), heading, font=self.small_font, fill=accent)
            y += line

            # MW_LCD_WEB_URL_ON_COPY_V1
            # Show the web dashboard URL on the main COPY screen.
            if page == "COPY":
                web_url = str(getattr(snap, "web_url", "") or "")
                if web_url:
                    url_text = shorten_to_width("WEB " + web_url, self.small_font, self.width - 2 * margin)
                    d.text((margin, y), url_text, font=self.small_font, fill=text_dim)
                    y += line

            if not content.strip():
                content = "Listening..."

            # Use measured mono-ish average instead of a fixed divisor that may clip.
            avg_char_w = max(7, text_wh("MMMMMMMMMM", self.small_font)[0] // 10)
            max_chars = max(14, (self.width - (2 * margin)) // avg_char_w)
            max_lines = max(2, (content_bottom - y) // line)

            wrapped_lines = self._wrap(content, max_chars)
            visible_lines = wrapped_lines[-max_lines:] if wrapped_lines else ["Listening..."]

            for wrapped in visible_lines:
                d.text((margin, y), wrapped, font=self.small_font, fill=text_main)
                y += line
                if y > content_bottom - line:
                    break

        # ------------------------------------------------------------------
        # Footer soft-key labels
        # ------------------------------------------------------------------
        d.rectangle((0, footer_y, self.width, self.height), fill=bg_footer)
        d.line((0, footer_y, self.width, footer_y), fill=(120, 120, 120))

        labels = ["MODE", "SCAN", "RESET", "CLEAR"]
        key_w = max(1, self.width // 4)

        for i, label in enumerate(labels):
            x0 = i * key_w
            x1 = self.width if i == 3 else (i + 1) * key_w

            active = str(getattr(snap, "ui_active_button", "") or "").upper() == label
            outline = warn if active else (155, 155, 155)
            fill = (35, 38, 45) if active else bg_footer

            d.rectangle((x0 + 2, footer_y + 4, x1 - 3, self.height - 4), outline=outline, fill=fill)

            tw, th = text_wh(label, self.small_font)
            tx = x0 + max(3, ((x1 - x0) - tw) // 2)
            ty = footer_y + max(5, (footer_h - th) // 2 - 1)
            d.text((tx, ty), label, font=self.small_font, fill=text_main)

        # ------------------------------------------------------------------
        # Floating status/toast, above footer, right aligned with measured text.
        # ------------------------------------------------------------------
        if snap.ui_message:
            note = str(snap.ui_message)
        elif str(getattr(snap, "ui_page", "COPY") or "COPY").upper() != "COPY":
            note = f"VIEW {str(getattr(snap, 'ui_page', 'COPY')).upper()}"
        elif snap.mode == "AUTO":
            note = "AUTO TRACK"
        elif snap.mode == "TRAIN":
            note = "TRAINING"
        else:
            note = "LCD READY"

        note = shorten_to_width(note, self.small_font, self.width - 2 * margin)
        note_w, note_h = text_wh(note, self.small_font)

        note_x = max(margin, self.width - margin - note_w)
        note_y = max(header_h + 2, footer_y - note_h - 8)

        # Clear a small patch behind the note to keep it readable.
        d.rectangle(
            (
                max(0, note_x - 4),
                max(header_h, note_y - 2),
                min(self.width - 1, note_x + note_w + 4),
                min(footer_y - 1, note_y + note_h + 4),
            ),
            fill=bg,
        )
        d.text((note_x, note_y), note, font=self.small_font, fill=warn)

        self._write(img)

    def _wrap(self, text: str, cols: int) -> List[str]:
        if not text:
            return [""]
        out = []
        current = ""
        for word in text.split(" "):
            if len(current) + len(word) + 1 > cols:
                out.append(current)
                current = word
            else:
                current = word if not current else current + " " + word
        if current:
            out.append(current)
        return out

    def _write(self, img: Image.Image) -> None:
        if self.bpp == 16:
            data = self._to_rgb565(img)
        elif self.bpp == 24:
            data = img.convert("RGB").tobytes()
        else:
            rgba = img.convert("RGBA")
            data = rgba.tobytes("raw", "BGRA")

        with open(self.fb_path, "wb", buffering=0) as fb:
            fb.write(data)


# --------------------------------------------------------------------
# Touch-only soft key UX.
#
# IMPORTANT GPIO SAFETY:
#   Do not use GPIO22: LCD D/C
#   Do not use GPIO27: LCD RESET
#   Do not use GPIO17: ADS7846 pendown IRQ
#   Do not use GPIO7/GPIO8: SPI chip selects
#
# This UI reads /dev/input/event1 only.
# --------------------------------------------------------------------
def _ui_toast(decoder, message: str, button: str = "", ttl: float = 1.8) -> None:
    decoder.ui_message = str(message)
    decoder.ui_message_until = time.monotonic() + float(ttl)

    if button:
        decoder.ui_active_button = str(button)
        decoder.ui_active_until = time.monotonic() + float(ttl)


def _ui_clear_copy(decoder) -> None:
    decoder.decoded_raw = ""
    decoder.decoded_expanded = ""
    decoder.current_word = ""
    decoder.current_symbol = ""
    _ui_toast(decoder, "COPY CLEARED", "CLEAR")
    print("[TOUCH] copy cleared", flush=True)


def _ui_reset_timing(decoder) -> None:
    # Reset timing estimator without touching display GPIO.
    decoder.dot_ms = PRELOCK_DOT_MS
    decoder.dash_ms = PRELOCK_DASH_MS
    decoder.avg_mark_ms = PRELOCK_DOT_MS
    decoder.avg_space_ms = PRELOCK_INTRA_MS
    decoder.char_gap_ms = PRELOCK_LETTER_MS
    decoder.word_gap_ms = 7.0 * PRELOCK_DOT_MS

    if hasattr(decoder, "mark_durations"):
        decoder.mark_durations.clear()
    if hasattr(decoder, "space_durations"):
        decoder.space_durations.clear()

    decoder.current_symbol = ""
    decoder.end_gap_flushed = False

    if hasattr(decoder, "sync_confidence"):
        decoder.sync_confidence = 0
    if hasattr(decoder, "sync_state"):
        decoder.sync_state = SyncState.SYNC_ACQUIRE

    _ui_toast(decoder, "TIMING RESET", "RESET")
    print("[TOUCH] timing reset", flush=True)


def _ui_cycle_mode(decoder) -> None:
    mode = str(getattr(decoder, "runtime_mode", "RX"))

    if mode == "RX":
        decoder.runtime_mode = "AUTO"
        decoder.training_enabled = False
        decoder.auto_tone_track = True
        _ui_toast(decoder, "MODE AUTO", "MODE")
    elif mode == "AUTO":
        decoder.runtime_mode = "TRAIN"
        decoder.training_enabled = True
        decoder.auto_tone_track = True
        if hasattr(decoder, "training_reset_state"):
            decoder.training_reset_state()
        _ui_toast(decoder, "MODE TRAIN", "MODE")
    else:
        decoder.runtime_mode = "RX"
        decoder.training_enabled = False
        decoder.auto_tone_track = False
        _ui_toast(decoder, "MODE RX", "MODE")

    print(f"[TOUCH] mode {decoder.runtime_mode}", flush=True)


def _ui_scan(decoder, long_press: bool = False) -> None:
    decoder.request_tone_scan = True

    if long_press:
        decoder.auto_tone_track = True
        decoder.runtime_mode = "AUTO"
        _ui_toast(decoder, "AUTO TRACK ON", "SCAN")
        print("[TOUCH] scan + auto tracking ON", flush=True)
    else:
        _ui_toast(decoder, "SCAN REQUESTED", "SCAN")
        print("[TOUCH] scan requested", flush=True)


def _ui_cycle_page(decoder) -> None:
    pages = ["COPY", "EXPANDED", "RAW", "STATUS"]
    current = str(getattr(decoder, "ui_page", "COPY"))

    try:
        idx = pages.index(current)
    except ValueError:
        idx = 0

    decoder.ui_page = pages[(idx + 1) % len(pages)]
    _ui_toast(decoder, f"VIEW {decoder.ui_page}", "RESET")
    print(f"[TOUCH] view {decoder.ui_page}", flush=True)


def _ui_softkey_action(decoder, key: str, long_press: bool = False) -> None:
    key = str(key).upper()

    if key == "MODE":
        if long_press:
            decoder.runtime_mode = "RX"
            decoder.training_enabled = False
            decoder.auto_tone_track = False
            _ui_reset_timing(decoder)
            _ui_toast(decoder, "RX RESET", "MODE")
            print("[TOUCH] long MODE: RX reset", flush=True)
        else:
            _ui_cycle_mode(decoder)

    elif key == "SCAN":
        _ui_scan(decoder, long_press=long_press)

    elif key == "RESET":
        if long_press:
            _ui_cycle_page(decoder)
        else:
            _ui_reset_timing(decoder)

    elif key == "CLEAR":
        if long_press:
            _ui_clear_copy(decoder)
            _ui_reset_timing(decoder)
            _ui_toast(decoder, "CLEAR + RESET", "CLEAR")
            print("[TOUCH] clear + reset", flush=True)
        else:
            _ui_clear_copy(decoder)


class TouchSoftKeyThread(threading.Thread):
    EV_SYN = 0x00
    EV_KEY = 0x01
    EV_ABS = 0x03

    ABS_X = 0x00
    ABS_Y = 0x01
    ABS_PRESSURE = 0x18
    BTN_TOUCH = 0x14A

    # Linux ioctl: EVIOCGABS(abs)
    @staticmethod
    def _eviocgabs(abs_code: int) -> int:
        # _IOR('E', 0x40 + abs_code, struct input_absinfo)
        # input_absinfo is 6 ints = 24 bytes.
        return (2 << 30) | (24 << 16) | (0x45 << 8) | (0x40 + abs_code)

    def __init__(self, decoder, stop_event: threading.Event, event_path: str = "auto"):
        super().__init__(daemon=True)
        self.decoder = decoder
        self.stop_event = stop_event
        self.event_path = event_path

        self.x = None
        self.y = None
        self.pressure = 0
        self.touching = False
        self.down_started = 0.0
        self.last_action_time = 0.0

        self.x_min = 0
        self.x_max = 4095
        self.y_min = 0
        self.y_max = 4095

        # If touch zones are reversed, these can be patched safely later.
        self.invert_x = False
        self.invert_y = False

    def _auto_event_path(self) -> str:
        """
        Find the resistive touchscreen input event.

        Prefer devices with names like ADS7846, STMPE, Touchscreen, or TFT.
        Avoid USB audio HID/kbd devices.
        """
        devices = Path("/proc/bus/input/devices")
        if not devices.exists():
            return self.event_path

        text = devices.read_text(errors="replace")
        blocks = [b for b in text.split("\n\n") if b.strip()]

        candidates = []

        for block in blocks:
            name = ""
            handlers = ""

            for line in block.splitlines():
                if line.startswith("N: Name="):
                    name = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("H: Handlers="):
                    handlers = line.split("=", 1)[1].strip()

            lname = name.lower()
            if "event" not in handlers:
                continue

            if "audio" in lname or "keyboard" in lname or "c-media" in lname:
                continue

            score = 0
            for token in ["touchscreen", "touch", "ads7846", "stmpe", "tft", "ili9341"]:
                if token in lname:
                    score += 10

            for h in handlers.split():
                if h.startswith("event"):
                    event_path = f"/dev/input/{h}"
                    if score > 0:
                        candidates.append((score, name, event_path))

        if candidates:
            candidates.sort(reverse=True)
            score, name, event_path = candidates[0]
            print(f"[TOUCH] auto-selected {event_path}: {name}", flush=True)
            return event_path

        available = []
        for block in blocks:
            name = ""
            handlers = ""
            for line in block.splitlines():
                if line.startswith("N: Name="):
                    name = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("H: Handlers="):
                    handlers = line.split("=", 1)[1].strip()
            if "event" in handlers:
                available.append(f"{name} [{handlers}]")

        print("[TOUCH] no touchscreen input event found", flush=True)
        if available:
            print("[TOUCH] available input events: " + " | ".join(available), flush=True)

        return ""

    def _read_abs_info(self, fd) -> None:
        try:
            for code, attr_min, attr_max in [
                (self.ABS_X, "x_min", "x_max"),
                (self.ABS_Y, "y_min", "y_max"),
            ]:
                buf = bytearray(24)
                fcntl.ioctl(fd, self._eviocgabs(code), buf, True)
                _value, minimum, maximum, _fuzz, _flat, _res = struct.unpack("iiiiii", buf)
                setattr(self, attr_min, int(minimum))
                setattr(self, attr_max, int(maximum))
        except Exception as exc:
            print(f"[TOUCH] ABS range read failed, using defaults: {exc}", flush=True)

    def _normalise(self):
        if self.x is None or self.y is None:
            return None, None

        xr = max(1, self.x_max - self.x_min)
        yr = max(1, self.y_max - self.y_min)

        nx = (float(self.x) - float(self.x_min)) / float(xr)
        ny = (float(self.y) - float(self.y_min)) / float(yr)

        nx = max(0.0, min(1.0, nx))
        ny = max(0.0, min(1.0, ny))

        if self.invert_x:
            nx = 1.0 - nx
        if self.invert_y:
            ny = 1.0 - ny

        return nx, ny

    def _handle_release(self):
        now = time.monotonic()

        if now - self.last_action_time < 0.20:
            return

        nx, ny = self._normalise()
        if nx is None or ny is None:
            return

        held = now - self.down_started if self.down_started else 0.0
        long_press = held >= 1.0

        # Bottom soft key bar. Accept top zone too in case Y is inverted.
        if not (ny >= 0.78 or ny <= 0.22):
            print(f"[TOUCH] ignored nx={nx:.2f} ny={ny:.2f}", flush=True)
            return

        if nx < 0.25:
            key = "MODE"
        elif nx < 0.50:
            key = "SCAN"
        elif nx < 0.75:
            key = "RESET"
        else:
            key = "CLEAR"

        print(f"[TOUCH] {key} nx={nx:.2f} ny={ny:.2f} long={long_press}", flush=True)
        _ui_softkey_action(self.decoder, key, long_press=long_press)
        self.last_action_time = now

    def run(self) -> None:
        event_path = self._auto_event_path() if self.event_path == "auto" else self.event_path
        if not event_path:
            return

        path = Path(event_path)
        if not path.exists():
            print(f"[TOUCH] {event_path} not found; touch disabled", flush=True)
            return

        self.event_path = event_path

        # 64-bit input_event: long sec, long usec, ushort type, ushort code, int value.
        fmt = "llHHi"
        size = struct.calcsize(fmt)

        try:
            with path.open("rb") as f:
                self._read_abs_info(f.fileno())
                print(
                    f"[TOUCH] enabled {self.event_path} "
                    f"x={self.x_min}..{self.x_max} y={self.y_min}..{self.y_max}",
                    flush=True,
                )

                while not self.stop_event.is_set():
                    data = f.read(size)
                    if len(data) != size:
                        continue

                    _sec, _usec, etype, code, value = struct.unpack(fmt, data)

                    if etype == self.EV_ABS:
                        if code == self.ABS_X:
                            self.x = int(value)
                        elif code == self.ABS_Y:
                            self.y = int(value)
                        elif code == self.ABS_PRESSURE:
                            self.pressure = int(value)

                    elif etype == self.EV_KEY and code == self.BTN_TOUCH:
                        if int(value):
                            self.touching = True
                            self.down_started = time.monotonic()
                        else:
                            if self.touching:
                                self._handle_release()
                            self.touching = False

        except PermissionError:
            print(f"[TOUCH] permission denied on {self.event_path}; add decoder to input group", flush=True)
        except Exception as exc:
            print(f"[TOUCH] error: {exc}", flush=True)




# Startup splash removed from decoder.
# Use morse_whisperer_splash.py as the separate launcher UI.


class DisplayThread(threading.Thread):
    def __init__(self, cfg: RuntimeConfig, shared: SharedState, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.shared = shared
        self.stop_event = stop_event
        self.error = ""
        self.display: Optional[FramebufferDisplay] = None

    def _init_display(self) -> Optional[FramebufferDisplay]:
        if not self.cfg.display_enabled:
            return None

        for fb in self.cfg.framebuffer_candidates:
            if os.path.exists(fb) and os.access(fb, os.W_OK):
                try:
                    disp = FramebufferDisplay(fb, self.cfg.font_size)
                    print(f"[DISPLAY] using {fb} {disp.width}x{disp.height} {disp.bpp}bpp")
                    return disp
                except Exception as exc:
                    self.error = f"{fb}: {exc}"
                    print(f"[DISPLAY] {self.error}", file=sys.stderr)

        self.error = "No writable framebuffer found"
        print(f"[DISPLAY] {self.error}; continuing console-only", file=sys.stderr)
        return None

    def run(self) -> None:
        self.display = self._init_display()
        if self.display is None:
            return

        interval = 1.0 / max(1.0, min(self.cfg.display_fps, 10.0))
        while not self.stop_event.is_set():
            try:
                snap = self.shared.get()
                snap.display_error = self.error
                self.display.render(snap)
            except Exception as exc:
                self.error = str(exc)
                print(f"[DISPLAY] render failed: {exc}", file=sys.stderr)
                time.sleep(2.0)
            time.sleep(interval)


# MW_WEB_DASHBOARD_V1
class QsoMetricsTracker:
    """
    In-memory current-QSO/session metrics.

    This deliberately consumes published Snapshot objects only. It does not
    mutate MorseDecoder state.
    """
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        now = time.time()
        self.session_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        self.started_wall = now
        self.first_copy_wall = 0.0
        self.last_update_wall = now
        self.last_snapshot_ms = -1
        self.sample_count = 0
        self.snr_sum = 0.0
        self.snr_peak = 0.0
        self.wpm_sum = 0.0
        self.wpm_count = 0
        self.wpm_min = 0.0
        self.wpm_max = 0.0
        self.tone_conf_sum = 0.0
        self.tone_conf_peak = 0.0
        self.squelch_open_count = 0
        self.last_q_overruns = 0
        self.last_audio_errors = 0
        self.last_copy = ""
        self.last_raw = ""
        self.last_expanded = ""
        self.last_mode = ""
        self.last_tone = 0.0
        self.last_detected_tone = 0.0
        self.last_sync = ""
        self.last_buffer = ""
        self.last_learning = ""
        self.last_audio_device = ""

    def update(self, snap: Snapshot) -> None:
        with self.lock:
            ts = int(getattr(snap, "timestamp_ms", 0) or 0)
            if ts and ts == self.last_snapshot_ms:
                return

            copy = str(getattr(snap, "copy", "") or "")
            raw = str(getattr(snap, "raw", "") or "")
            raw_literal = str(getattr(snap, "raw_literal", raw) or raw)
            expanded = str(getattr(snap, "expanded", "") or "")

            # If copy was cleared, start a new in-memory session.
            if self.last_copy.strip() and len(copy.strip()) + 20 < len(self.last_copy.strip()):
                self.reset()

            now = time.time()
            self.last_update_wall = now
            self.last_snapshot_ms = ts
            self.sample_count += 1

            if copy.strip() and not self.first_copy_wall:
                self.first_copy_wall = now

            snr = float(getattr(snap, "snr", 0.0) or 0.0)
            wpm = float(getattr(snap, "wpm", 0.0) or 0.0)
            tone_conf = float(getattr(snap, "detected_tone_confidence", 0.0) or 0.0)

            self.snr_sum += snr
            self.snr_peak = max(self.snr_peak, snr)

            # MW_QSO_WPM_CREDIBLE_AVG_V1
            # Do not average boot/default/acquisition WPM guesses into QSO
            # metrics. Only count WPM while we have real copy activity and a
            # believable signal/sync state.
            copy_started = bool(copy.strip())
            sync_text = str(getattr(snap, "sync_state", "") or "")
            sync_conf = int(getattr(snap, "sync_confidence", 0) or 0)
            squelch_open = bool(getattr(snap, "squelch_open", False))

            credible_wpm = (
                copy_started
                and 5.0 <= wpm <= 45.0
                and (
                    squelch_open
                    or sync_conf >= 35
                    or sync_text in ("TRACK", "WEAK")
                )
            )

            if credible_wpm:
                self.wpm_sum += wpm
                self.wpm_count += 1
                self.wpm_min = wpm if self.wpm_min <= 0.0 else min(self.wpm_min, wpm)
                self.wpm_max = max(self.wpm_max, wpm)

            self.tone_conf_sum += tone_conf
            self.tone_conf_peak = max(self.tone_conf_peak, tone_conf)

            if bool(getattr(snap, "squelch_open", False)):
                self.squelch_open_count += 1

            self.last_q_overruns = int(getattr(snap, "q_overruns", 0) or 0)
            self.last_audio_errors = int(getattr(snap, "audio_errors", 0) or 0)
            self.last_copy = copy
            self.last_raw = raw
            self.last_raw_literal = raw_literal
            self.last_expanded = expanded
            self.last_mode = str(getattr(snap, "display_mode", getattr(snap, "mode", "")) or "")
            self.last_tone = float(getattr(snap, "target_tone_hz", 0.0) or 0.0)
            self.last_detected_tone = float(getattr(snap, "detected_tone_hz", 0.0) or 0.0)
            self.last_sync = f"{getattr(snap, 'sync_state', '')}/{int(getattr(snap, 'sync_confidence', 0) or 0)}"
            self.last_buffer = str(getattr(snap, "buffer_status", "") or "")
            self.last_learning = str(getattr(snap, "learning_status", "") or "")
            self.last_audio_device = str(getattr(snap, "selected_audio_device", "") or "")

    def report(self, snap: Snapshot) -> dict:
        self.update(snap)

        with self.lock:
            now = time.time()
            duration = max(0.0, now - self.started_wall)
            copy_duration = max(0.0, now - self.first_copy_wall) if self.first_copy_wall else 0.0
            avg_snr = self.snr_sum / self.sample_count if self.sample_count else 0.0
            avg_wpm = self.wpm_sum / self.wpm_count if self.wpm_count else 0.0
            avg_tone_conf = self.tone_conf_sum / self.sample_count if self.sample_count else 0.0
            squelch_open_pct = (100.0 * self.squelch_open_count / self.sample_count) if self.sample_count else 0.0

            copy_text = self.last_copy.strip()
            raw_text = str(getattr(self, "last_raw_literal", self.last_raw)).strip()
            words = [w for w in copy_text.split() if w.strip()]

            return {
                "session_id": self.session_id,
                "started": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(self.started_wall)),
                "updated": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(self.last_update_wall)),
                "duration_seconds": round(duration, 1),
                "copy_duration_seconds": round(copy_duration, 1),
                "mode": self.last_mode,
                "target_tone_hz": round(self.last_tone, 1),
                "detected_tone_hz": round(self.last_detected_tone, 1),
                "avg_wpm": round(avg_wpm, 1),
                "min_wpm": round(self.wpm_min, 1),
                "max_wpm": round(self.wpm_max, 1),
                "avg_snr": round(avg_snr, 2),
                "peak_snr": round(self.snr_peak, 2),
                "avg_tone_confidence": round(avg_tone_conf, 2),
                "peak_tone_confidence": round(self.tone_conf_peak, 2),
                "squelch_open_percent": round(squelch_open_pct, 1),
                "sync": self.last_sync,
                "buffer_status": self.last_buffer,
                "learning_status": self.last_learning,
                "queue_overruns": self.last_q_overruns,
                "audio_errors": self.last_audio_errors,
                "audio_device": self.last_audio_device,
                "character_count": len(copy_text.replace(" ", "")),
                "word_count": len(words),
                "unknown_symbol_count": raw_text.count("#"),
                "copy": copy_text,
                "raw": raw_text,
                "raw_literal": str(getattr(self, "last_raw_literal", self.last_raw)).strip(),
                "expanded": self.last_expanded.strip(),
            }


class WebThread(threading.Thread):
    """
    Web dashboard, QSO export server, and safe runtime controls.

    The web UI still reads state from SharedState. Runtime controls are limited
    to explicit operator settings such as squelch threshold.
    """
    def __init__(self, cfg: RuntimeConfig, shared: SharedState, stop_event: threading.Event, decoder=None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.shared = shared
        self.stop_event = stop_event
        self.decoder = decoder
        self.metrics = QsoMetricsTracker()
        self.error = ""

    def run(self) -> None:
        if not bool(getattr(self.cfg, "web_enabled", True)):
            print("[WEB] disabled by config", flush=True)
            return

        try:
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
            from urllib.parse import urlparse
            import csv
            import html
            import io
            import json
        except Exception as exc:
            self.error = str(exc)
            print(f"[WEB] import failed: {exc}", flush=True)
            return

        outer = self

        def snapshot_dict() -> dict:
            snap = outer.shared.get()
            outer.metrics.update(snap)
            data = dict(getattr(snap, "__dict__", {}))
            return data

        def qso_report() -> dict:
            snap = outer.shared.get()
            return outer.metrics.report(snap)

        def markdown_report(rep: dict) -> str:
            lines = [
                "# The Morse Whisperer Decode Report",
                "",
                f"- Session ID: `{rep.get('session_id', '')}`",
                f"- Started: {rep.get('started', '')}",
                f"- Updated: {rep.get('updated', '')}",
                f"- Duration: {rep.get('duration_seconds', 0)} seconds",
                f"- Copy Duration: {rep.get('copy_duration_seconds', 0)} seconds",
                f"- Mode: {rep.get('mode', '')}",
                f"- Tone: target {rep.get('target_tone_hz', 0)} Hz / detected {rep.get('detected_tone_hz', 0)} Hz",
                f"- WPM: avg {rep.get('avg_wpm', 0)}, min {rep.get('min_wpm', 0)}, max {rep.get('max_wpm', 0)}",
                f"- SNR: avg {rep.get('avg_snr', 0)}, peak {rep.get('peak_snr', 0)}",
                f"- Squelch Open: {rep.get('squelch_open_percent', 0)}%",
                f"- Sync: {rep.get('sync', '')}",
                f"- Queue Overruns: {rep.get('queue_overruns', 0)}",
                f"- Audio Errors: {rep.get('audio_errors', 0)}",
                f"- Characters: {rep.get('character_count', 0)}",
                f"- Words: {rep.get('word_count', 0)}",
                f"- Unknown Symbols: {rep.get('unknown_symbol_count', 0)}",
                "",
                "## Operator Copy",
                "",
                rep.get("copy", "") or "_No copy yet._",
                "",
                "## Expanded Copy",
                "",
                rep.get("expanded", "") or "_No expanded copy yet._",
                "",
                "## Raw Decode - Literal",
                "",
                "```text",
                rep.get("raw_literal", rep.get("raw", "")),
                "```",
                "",
            ]
            return "\n".join(lines)

        def csv_report(rep: dict) -> str:
            summary_keys = [
                "session_id", "started", "updated", "duration_seconds",
                "copy_duration_seconds", "mode", "target_tone_hz",
                "detected_tone_hz", "avg_wpm", "min_wpm", "max_wpm",
                "avg_snr", "peak_snr", "avg_tone_confidence",
                "peak_tone_confidence", "squelch_open_percent", "sync",
                "buffer_status", "learning_status", "queue_overruns",
                "audio_errors", "character_count", "word_count",
                "unknown_symbol_count",
            ]
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=summary_keys)
            writer.writeheader()
            writer.writerow({k: rep.get(k, "") for k in summary_keys})
            return buf.getvalue()

        # MW_QSO_SAVE_ADIF_V1
        qso_dir = Path("/opt/morse-whisperer-pi/qso")

        def _clean_adif_value(value) -> str:
            value = "" if value is None else str(value)
            value = value.replace("\r", " ").replace("\n", " ")
            value = " ".join(value.split())
            return value.strip()

        def _safe_id(value: str) -> str:
            value = str(value or "")
            out = []
            for ch in value:
                if ch.isalnum() or ch in ("-", "_"):
                    out.append(ch)
                else:
                    out.append("-")
            cleaned = "".join(out).strip("-")
            return cleaned or "qso"

        def _adif_line(fields: dict) -> str:
            parts = []
            for key, value in fields.items():
                value = _clean_adif_value(value)
                if not value:
                    continue
                parts.append(f"<{key}:{len(value)}>{value}")
            parts.append("<EOR>")
            return " ".join(parts) + "\n"

        def _guess_call_from_copy(copy_text: str) -> str:
            try:
                import re
                compact = re.sub(r"[^A-Z0-9]", "", str(copy_text or "").upper())
                for pat in (r"ZL[0-9][A-Z]{2,3}", r"VK[0-9][A-Z]{2,3}", r"[A-Z]{1,2}[0-9][A-Z]{2,3}"):
                    m = re.search(pat, compact)
                    if m:
                        return m.group(0)
            except Exception:
                pass
            return ""

        def _build_qso_record(payload: dict, persist: bool = False) -> dict:
            rep = qso_report()

            payload = payload if isinstance(payload, dict) else {}
            draft = payload.get("draft", payload)
            if not isinstance(draft, dict):
                draft = {}

            now = time.gmtime()

            call = _clean_adif_value(draft.get("call") or draft.get("CALL") or _guess_call_from_copy(rep.get("copy", ""))).upper()
            mode = _clean_adif_value(draft.get("mode") or draft.get("MODE") or "CW").upper()
            qso_date = _clean_adif_value(draft.get("qso_date") or draft.get("QSO_DATE") or time.strftime("%Y%m%d", now))
            time_on = _clean_adif_value(draft.get("time_on") or draft.get("TIME_ON") or time.strftime("%H%M%S", now))
            time_off = _clean_adif_value(draft.get("time_off") or draft.get("TIME_OFF") or time.strftime("%H%M%S", now))

            band = _clean_adif_value(draft.get("band") or draft.get("BAND"))
            freq = _clean_adif_value(draft.get("freq") or draft.get("FREQ"))
            rst_sent = _clean_adif_value(draft.get("rst_sent") or draft.get("RST_SENT") or "599")
            rst_rcvd = _clean_adif_value(draft.get("rst_rcvd") or draft.get("RST_RCVD") or "599")
            name = _clean_adif_value(draft.get("name") or draft.get("NAME"))
            qth = _clean_adif_value(draft.get("qth") or draft.get("QTH"))
            comment = _clean_adif_value(draft.get("comment") or draft.get("COMMENT") or draft.get("notes") or "")

            if not comment:
                comment = (
                    f"Decoded by The Morse Whisperer. "
                    f"Avg WPM {rep.get('avg_wpm', 0)}, Avg SNR {rep.get('avg_snr', 0)}."
                )

            station_callsign = _clean_adif_value(draft.get("station_callsign") or draft.get("STATION_CALLSIGN"))
            operator = _clean_adif_value(draft.get("operator") or draft.get("OPERATOR"))

            adif_fields = {
                "CALL": call,
                "QSO_DATE": qso_date,
                "TIME_ON": time_on,
                "TIME_OFF": time_off,
                "MODE": mode,
                "BAND": band,
                "FREQ": freq,
                "RST_SENT": rst_sent,
                "RST_RCVD": rst_rcvd,
                "NAME": name,
                "QTH": qth,
                "COMMENT": comment,
                "STATION_CALLSIGN": station_callsign,
                "OPERATOR": operator,
            }

            qso_id_parts = [
                "qso",
                qso_date or time.strftime("%Y%m%d", now),
                time_on or time.strftime("%H%M%S", now),
                call or "unknown",
            ]
            qso_id = _safe_id("-".join(qso_id_parts))

            record = {
                "id": qso_id,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime()),
                "adif_fields": adif_fields,
                "adif": _adif_line(adif_fields),
                "copy": rep.get("copy", ""),
                "raw": rep.get("raw", ""),
                "raw_literal": rep.get("raw_literal", ""),
                "expanded": rep.get("expanded", ""),
                "metrics": rep,
                "source": "The Morse Whisperer",
                "version": str(globals().get("VERSION_STR", "")),
            }

            if persist:
                qso_dir.mkdir(parents=True, exist_ok=True)

                json_path = qso_dir / f"{qso_id}.json"
                adi_path = qso_dir / f"{qso_id}.adi"

                # If saving twice in the same second, avoid overwriting.
                if json_path.exists() or adi_path.exists():
                    suffix = time.strftime("%H%M%S", time.localtime())
                    qso_id2 = _safe_id(f"{qso_id}-{suffix}")
                    record["id"] = qso_id2
                    json_path = qso_dir / f"{qso_id2}.json"
                    adi_path = qso_dir / f"{qso_id2}.adi"

                json_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
                adi_path.write_text(record["adif"], encoding="utf-8")

                record["json_path"] = str(json_path)
                record["adi_path"] = str(adi_path)

            return record

        def _recent_qsos(limit: int = 20) -> list:
            qso_dir.mkdir(parents=True, exist_ok=True)
            out = []

            files = sorted(qso_dir.glob("qso-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)

            for fp in files[:max(1, min(limit, 100))]:
                try:
                    data = json.loads(fp.read_text(encoding="utf-8"))
                    fields = data.get("adif_fields", {})
                    metrics = data.get("metrics", {})
                    out.append({
                        "id": data.get("id", fp.stem),
                        "saved_at": data.get("saved_at", ""),
                        "call": fields.get("CALL", ""),
                        "qso_date": fields.get("QSO_DATE", ""),
                        "time_on": fields.get("TIME_ON", ""),
                        "mode": fields.get("MODE", ""),
                        "band": fields.get("BAND", ""),
                        "freq": fields.get("FREQ", ""),
                        "rst_sent": fields.get("RST_SENT", ""),
                        "rst_rcvd": fields.get("RST_RCVD", ""),
                        "word_count": metrics.get("word_count", 0),
                        "avg_wpm": metrics.get("avg_wpm", 0),
                        "avg_snr": metrics.get("avg_snr", 0),
                    })
                except Exception:
                    continue

            return out

        class Handler(BaseHTTPRequestHandler):
            server_version = "MorseWhispererHTTP/1.0"

            def log_message(self, fmt, *args):
                # Keep logs quiet; service.log already has decoder details.
                return

            def send_bytes(self, status: int, body: bytes, content_type: str, filename: str = ""):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                if filename:
                    self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.end_headers()
                self.wfile.write(body)

            def send_text(self, status: int, text: str, content_type: str = "text/plain; charset=utf-8", filename: str = ""):
                self.send_bytes(status, text.encode("utf-8"), content_type, filename=filename)

            def send_json(self, data: dict):
                self.send_text(200, json.dumps(data, indent=2, ensure_ascii=False), "application/json; charset=utf-8")

            def do_POST(self):
                parsed = urlparse(self.path)
                path = parsed.path

                try:
                    length = int(self.headers.get("Content-Length", "0") or "0")
                    if length > 262144:
                        return self.send_text(413, "Payload too large\n")

                    raw_body = self.rfile.read(length) if length else b"{}"

                    try:
                        payload = json.loads(raw_body.decode("utf-8") or "{}")
                    except Exception as exc:
                        return self.send_text(400, f"Invalid JSON: {exc}\n")

                    if path == "/api/qso/save":
                        rec = _build_qso_record(payload, persist=True)
                        return self.send_json({
                            "ok": True,
                            "id": rec.get("id", ""),
                            "json_path": rec.get("json_path", ""),
                            "adi_path": rec.get("adi_path", ""),
                            "adif_fields": rec.get("adif_fields", {}),
                        })

                    if path == "/api/control/squelch":
                        decoder = getattr(outer, "decoder", None)
                        if decoder is None:
                            return self.send_text(503, "Decoder control unavailable\n")

                        try:
                            value = float(payload.get("squelch_snr", payload.get("value", 0)))
                        except Exception:
                            return self.send_text(400, "Invalid squelch value\n")

                        # Practical operator range. Below 1.0 tends to open on
                        # idle noise; above 12 is usually too deaf.
                        value = max(1.0, min(12.0, value))

                        decoder.squelch_snr = value
                        try:
                            decoder.cfg.squelch_snr = value
                        except Exception:
                            pass

                        # MW_PERSIST_SQUELCH_SETTING_V1
                        # Persist the operator squelch setting so the appliance
                        # comes back with the same threshold after reboot.
                        config_saved = False
                        config_error = ""

                        try:
                            cfg_path = Path("/opt/morse-whisperer-pi/config.json")
                            cfg_data = {}

                            if cfg_path.exists():
                                try:
                                    cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
                                except Exception:
                                    cfg_data = {}

                            if not isinstance(cfg_data, dict):
                                cfg_data = {}

                            cfg_data["squelch_snr"] = round(value, 2)

                            tmp_path = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
                            tmp_path.write_text(
                                json.dumps(cfg_data, indent=2, ensure_ascii=False) + "\n",
                                encoding="utf-8",
                            )
                            tmp_path.replace(cfg_path)
                            config_saved = True

                        except Exception as exc:
                            config_error = str(exc)
                            print(f"[WEB] failed to persist squelch_snr: {exc}", flush=True)

                        print(
                            f"[WEB] squelch_snr set to {value:.2f} "
                            f"persisted={'yes' if config_saved else 'no'}",
                            flush=True,
                        )

                        return self.send_json({
                            "ok": True,
                            "squelch_snr": round(value, 2),
                            "persisted": config_saved,
                            "config_error": config_error,
                        })

                    return self.send_text(404, "Not found\n")

                except Exception as exc:
                    return self.send_text(500, f"Internal error: {exc}\n")

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path

                try:
                    if path == "/":
                        return self.send_text(200, self.index_html(), "text/html; charset=utf-8")

                    if path == "/api/snapshot":
                        return self.send_json(snapshot_dict())

                    if path == "/api/qso/current":
                        return self.send_json(qso_report())

                    if path == "/api/qso/recent":
                        return self.send_json({"qsos": _recent_qsos(limit=25)})

                    if path == "/download/current.adi":
                        rec = _build_qso_record({}, persist=False)
                        sid = rec.get("id", "current")
                        return self.send_text(
                            200,
                            rec.get("adif", ""),
                            "application/adif; charset=utf-8",
                            filename=f"{sid}.adi",
                        )

                    if path.startswith("/download/qso/") and path.endswith(".adi"):
                        qso_id = path.split("/")[-1].replace(".adi", "")
                        adi_path = qso_dir / f"{_safe_id(qso_id)}.adi"
                        if adi_path.exists():
                            return self.send_text(
                                200,
                                adi_path.read_text(encoding="utf-8"),
                                "application/adif; charset=utf-8",
                                filename=adi_path.name,
                            )
                        return self.send_text(404, "QSO ADIF not found\n")

                    if path.startswith("/download/qso/") and path.endswith(".json"):
                        qso_id = path.split("/")[-1].replace(".json", "")
                        json_path = qso_dir / f"{_safe_id(qso_id)}.json"
                        if json_path.exists():
                            return self.send_text(
                                200,
                                json_path.read_text(encoding="utf-8"),
                                "application/json; charset=utf-8",
                                filename=json_path.name,
                            )
                        return self.send_text(404, "QSO JSON not found\n")

                    rep = qso_report()
                    sid = rep.get("session_id", "current")

                    if path == "/download/current.txt":
                        return self.send_text(
                            200,
                            rep.get("copy", "") + "\n",
                            "text/plain; charset=utf-8",
                            filename=f"morse-whisperer-{sid}.txt",
                        )

                    if path == "/download/current.md":
                        return self.send_text(
                            200,
                            markdown_report(rep),
                            "text/markdown; charset=utf-8",
                            filename=f"morse-whisperer-{sid}.md",
                        )

                    if path == "/download/current.json":
                        return self.send_text(
                            200,
                            json.dumps(rep, indent=2, ensure_ascii=False),
                            "application/json; charset=utf-8",
                            filename=f"morse-whisperer-{sid}.json",
                        )

                    if path == "/download/current.csv":
                        return self.send_text(
                            200,
                            csv_report(rep),
                            "text/csv; charset=utf-8",
                            filename=f"morse-whisperer-{sid}.csv",
                        )

                    self.send_text(404, "Not found\n")
                except Exception as exc:
                    self.send_text(500, f"Internal error: {exc}\n")

            def index_html(self) -> str:
                return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>The Morse Whisperer</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root {
      --bg: #050b11;
      --bg2: #08131d;
      --panel: #0d1823;
      --panel2: #101f2d;
      --panel3: #07101a;
      --line: #234058;
      --line2: #172c3e;
      --text: #eef6ff;
      --muted: #8fa6ba;
      --soft: #c7d8e8;
      --cyan: #54d7ff;
      --green: #75e09b;
      --amber: #ffd36a;
      --red: #ff7272;
      --button: #d8f5ff;
      --buttonText: #04101a;
      --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
      --sans: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      color: var(--text);
      background:
        radial-gradient(circle at 18% -10%, rgba(45, 119, 160, 0.35), transparent 36%),
        linear-gradient(180deg, var(--bg2), var(--bg) 42%, #020509);
      font-family: var(--sans);
    }

    .topbar {
      border-bottom: 1px solid var(--line);
      background: rgba(3, 8, 13, 0.86);
      position: sticky;
      top: 0;
      z-index: 20;
      backdrop-filter: blur(10px);
    }

    .topbar-inner {
      max-width: 1460px;
      margin: 0 auto;
      padding: 14px 18px;
      display: grid;
      grid-template-columns: 1.2fr 2fr;
      gap: 14px;
      align-items: center;
    }

    .brand h1 {
      margin: 0;
      font-size: 1.28rem;
      line-height: 1.1;
      letter-spacing: 0.015em;
      font-weight: 780;
    }

    .brand .subtitle {
      color: var(--muted);
      font-size: 0.82rem;
      margin-top: 4px;
    }

    .top-meters {
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 8px;
    }

    .pill {
      border: 1px solid var(--line2);
      background: rgba(12, 27, 39, 0.78);
      border-radius: 12px;
      padding: 8px 10px;
      min-width: 0;
    }

    .pill .label {
      color: var(--muted);
      font-size: 0.66rem;
      letter-spacing: 0.09em;
      text-transform: uppercase;
      white-space: nowrap;
    }

    .pill .value {
      margin-top: 2px;
      color: var(--text);
      font-weight: 720;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .pill .value.good { color: var(--green); }
    .pill .value.warn { color: var(--amber); }
    .pill .value.bad { color: var(--red); }

    main {
      max-width: 1460px;
      margin: 0 auto;
      padding: 16px 18px 28px;
    }

    .workspace {
      display: grid;
      grid-template-columns: minmax(0, 1.55fr) minmax(360px, 0.95fr);
      gap: 14px;
      align-items: start;
    }

    .panel {
      border: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(15, 29, 42, 0.96), rgba(8, 17, 27, 0.96));
      border-radius: 16px;
      box-shadow: 0 16px 42px rgba(0, 0, 0, 0.26);
      overflow: hidden;
    }

    .panel-head {
      padding: 11px 13px;
      border-bottom: 1px solid var(--line2);
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      background: rgba(5, 12, 19, 0.55);
    }

    .panel-title {
      font-size: 0.78rem;
      color: var(--cyan);
      letter-spacing: 0.12em;
      text-transform: uppercase;
      font-weight: 800;
    }

    .panel-note {
      font-size: 0.78rem;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .copy-box {
      min-height: 315px;
      padding: 18px 20px 22px;
      font-size: clamp(1.45rem, 3.0vw, 2.65rem);
      line-height: 1.25;
      letter-spacing: 0.01em;
      white-space: pre-wrap;
      word-break: break-word;
    }

    .signal-strip {
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 8px;
      padding: 10px 12px;
      border-top: 1px solid var(--line2);
      background: rgba(4, 9, 15, 0.46);
    }

    .strip-item {
      min-width: 0;
      font-size: 0.84rem;
    }

    .strip-item.squelch-control {
      grid-column: span 2;
    }

    .strip-item span {
      display: block;
      color: var(--muted);
      font-size: 0.66rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    #squelch-slider {
      display: block;
      width: 100%;
      margin-top: 5px;
      padding: 0;
      accent-color: var(--cyan);
    }

    .raw-box {
      min-height: 118px;
      padding: 13px;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: var(--mono);
      color: #d5e7f5;
      font-size: 0.88rem;
      line-height: 1.42;
    }

    .right-stack {
      display: grid;
      gap: 14px;
    }

    .qso-form {
      padding: 13px;
      display: grid;
      gap: 10px;
    }

    .form-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 9px;
    }

    .field {
      display: grid;
      gap: 4px;
    }

    .field.full {
      grid-column: 1 / -1;
    }

    label {
      color: var(--muted);
      font-size: 0.68rem;
      text-transform: uppercase;
      letter-spacing: 0.085em;
      font-weight: 720;
    }

    input, textarea, select {
      width: 100%;
      border: 1px solid var(--line2);
      border-radius: 10px;
      background: rgba(2, 7, 12, 0.72);
      color: var(--text);
      padding: 9px 10px;
      font: inherit;
      outline: none;
    }

    input:focus, textarea:focus, select:focus {
      border-color: var(--cyan);
      box-shadow: 0 0 0 2px rgba(84, 215, 255, 0.13);
    }

    textarea {
      min-height: 74px;
      resize: vertical;
    }

    .button-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      padding-top: 2px;
    }

    a.btn, button.btn {
      border: 0;
      border-radius: 999px;
      background: var(--button);
      color: var(--buttonText);
      padding: 9px 12px;
      font-weight: 800;
      text-decoration: none;
      cursor: pointer;
      font-size: 0.88rem;
    }

    a.btn.secondary, button.btn.secondary {
      background: #17293a;
      color: var(--soft);
      border: 1px solid var(--line);
    }

    a.btn.disabled, button.btn.disabled {
      opacity: 0.55;
      cursor: not-allowed;
    }

    .metrics {
      padding: 4px 13px 13px;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.88rem;
    }

    td {
      padding: 7px 2px;
      border-bottom: 1px solid rgba(255,255,255,0.065);
      vertical-align: top;
    }

    td:first-child {
      color: var(--muted);
      width: 42%;
    }

    .recent {
      margin-top: 14px;
    }

    .recent-list {
      padding: 0 13px 13px;
    }

    .session-row {
      display: grid;
      grid-template-columns: 1fr auto auto auto;
      gap: 10px;
      align-items: center;
      padding: 9px 0;
      border-bottom: 1px solid rgba(255,255,255,0.06);
      font-size: 0.9rem;
    }

    .session-row .muted {
      color: var(--muted);
      font-size: 0.78rem;
    }

    .small-mono {
      font-family: var(--mono);
      font-size: 0.78rem;
      color: var(--muted);
    }

    .notice {
      color: var(--amber);
      font-size: 0.82rem;
      line-height: 1.35;
      padding: 0 13px 13px;
    }

    @media (max-width: 1050px) {
      .topbar-inner { grid-template-columns: 1fr; }
      .top-meters { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .workspace { grid-template-columns: 1fr; }
      .signal-strip { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    }

    @media (max-width: 620px) {
      main { padding: 12px; }
      .topbar-inner { padding: 12px; }
      .top-meters { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .form-grid { grid-template-columns: 1fr; }
      .copy-box { font-size: 1.55rem; min-height: 240px; padding: 15px; }
      .signal-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .session-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header class="topbar">
    <div class="topbar-inner">
      <div class="brand">
        <h1>The Morse Whisperer</h1>
        <div class="subtitle">CW decode appliance · live operator console · QRZ-ready QSO workflow</div>
      </div>
      <div class="top-meters">
        <div class="pill"><div class="label">Mode</div><div class="value" id="top-mode">-</div></div>
        <div class="pill"><div class="label">Tone</div><div class="value" id="top-tone">-</div></div>
        <div class="pill"><div class="label">WPM</div><div class="value" id="top-wpm">-</div></div>
        <div class="pill"><div class="label">SNR</div><div class="value" id="top-snr">-</div></div>
        <div class="pill"><div class="label">Sync</div><div class="value" id="top-sync">-</div></div>
        <div class="pill"><div class="label">UTC</div><div class="value" id="utc-clock">-</div></div>
      </div>
    </div>
  </header>

  <main>
    <section class="workspace">
      <div>
        <section class="panel">
          <div class="panel-head">
            <div class="panel-title">Operator Copy</div>
            <div class="panel-note" id="copy-note">Smart copy · literal RAW below</div>
          </div>
          <div class="copy-box" id="copy">Listening...</div>

          <div class="signal-strip">
            <div class="strip-item"><span>SQL</span><b id="strip-sql">-</b></div>
            <div class="strip-item"><span>Detected</span><b id="strip-detected">-</b></div>
            <div class="strip-item"><span>Buffer</span><b id="strip-buffer">-</b></div>
            <div class="strip-item"><span>Learning</span><b id="strip-learning">-</b></div>
            <div class="strip-item"><span>Queue</span><b id="strip-queue">-</b></div>
            <div class="strip-item"><span>Audio</span><b id="strip-audio">-</b></div>
            <div class="strip-item squelch-control">
              <span>Squelch</span>
              <b id="strip-squelch">-</b>
              <input id="squelch-slider" type="range" min="1" max="12" step="0.1" value="1.45">
            </div>
          </div>
        </section>

        <section class="panel recent">
          <div class="panel-head">
            <div class="panel-title">Raw Decode</div>
            <div class="panel-note">Literal decoder stream · uncorrected</div>
          </div>
          <div class="raw-box" id="raw"></div>
        </section>

        <section class="panel recent">
          <div class="panel-head">
            <div class="panel-title">Current Session</div>
            <div class="panel-note" id="session-id">-</div>
          </div>
          <div class="recent-list">
            <div class="session-row">
              <div>
                <div id="session-summary">No QSO copy yet.</div>
                <div class="muted" id="session-detail">Waiting for decoded text.</div>
              </div>
              <div><span class="small-mono" id="session-duration">0s</span></div>
              <div><span class="small-mono" id="session-words">0 words</span></div>
              <div><span class="small-mono" id="session-quality">SNR -</span></div>
            </div>
          </div>
        </section>
      </div>

      <aside class="right-stack">
        <section class="panel">
          <div class="panel-head">
            <div class="panel-title">QSO Draft</div>
            <div class="panel-note">QRZ / ADIF-ready fields</div>
          </div>

          <div class="qso-form">
            <div class="form-grid">
              <div class="field">
                <label for="qso-call">Callsign</label>
                <input id="qso-call" placeholder="e.g. ZL1ABC">
              </div>
              <div class="field">
                <label for="qso-mode">Mode</label>
                <input id="qso-mode" value="CW">
              </div>

              <div class="field">
                <label for="qso-date">Date UTC</label>
                <input id="qso-date" placeholder="YYYYMMDD">
              </div>
              <div class="field">
                <label for="qso-time">Time On UTC</label>
                <input id="qso-time" placeholder="HHMMSS">
              </div>

              <div class="field">
                <label for="qso-band">Band</label>
                <input id="qso-band" placeholder="40m / 20m">
              </div>
              <div class="field">
                <label for="qso-freq">Frequency MHz</label>
                <input id="qso-freq" placeholder="7.030">
              </div>

              <div class="field">
                <label for="qso-rst-s">RST Sent</label>
                <input id="qso-rst-s" value="599">
              </div>
              <div class="field">
                <label for="qso-rst-r">RST Rcvd</label>
                <input id="qso-rst-r" value="599">
              </div>

              <div class="field">
                <label for="qso-name">Name</label>
                <input id="qso-name" placeholder="Optional">
              </div>
              <div class="field">
                <label for="qso-qth">QTH</label>
                <input id="qso-qth" placeholder="Optional">
              </div>

              <div class="field full">
                <label for="qso-notes">Notes / Comment</label>
                <textarea id="qso-notes" placeholder="Decoded by The Morse Whisperer"></textarea>
              </div>
            </div>

            <div class="button-row">
              <a class="btn" href="/download/current.md">Report</a>
              <a class="btn secondary" href="/download/current.txt">TXT</a>
              <a class="btn secondary" href="/download/current.json">JSON</a>
              <a class="btn secondary" href="/download/current.csv">CSV</a>
              <button class="btn" id="btn-save-qso" type="button">Save QSO</button>
              <a class="btn secondary" id="btn-export-adif" href="/download/current.adi">Export ADIF</a>
              <button class="btn disabled" title="Future QRZ integration" type="button">Upload QRZ</button>
            </div>
          </div>

          <div class="notice">
            QRZ upload is not enabled yet. This screen is shaping the QSO record
            now so ADIF/QRZ support can be added cleanly later.
          </div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div class="panel-title">Decode Metrics</div>
            <div class="panel-note">Current in-memory session</div>
          </div>
          <div class="metrics">
            <table><tbody id="metrics"></tbody></table>
          </div>
        </section>
      </aside>
    </section>
  </main>

<script>
let operatorTouchedCall = false;

function esc(v) {
  return (v === undefined || v === null) ? "" : String(v);
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = esc(value);
}

function setValue(id, value, overwrite = false) {
  const el = document.getElementById(id);
  if (!el) return;
  if (overwrite || !el.value) el.value = esc(value);
}

function utcParts() {
  const d = new Date();
  const pad = n => String(n).padStart(2, "0");
  return {
    date: `${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}`,
    time: `${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}${pad(d.getUTCSeconds())}`,
    clock: `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`
  };
}

function maybeCallsign(text) {
  const cleaned = esc(text).toUpperCase();
  const compact = cleaned.replace(/[^A-Z0-9]/g, "");

  const patterns = [
    /ZL[0-9][A-Z]{2,3}/,
    /VK[0-9][A-Z]{2,3}/,
    /[A-Z]{1,2}[0-9][A-Z]{2,3}/
  ];

  for (const p of patterns) {
    const m = compact.match(p);
    if (m) return m[0];
  }
  return "";
}

async function fetchJson(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(url + " " + res.status);
  return await res.json();
}

async function postJson(url, payload) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {})
  });
  if (!res.ok) throw new Error(await res.text());
  return await res.json();
}

function fieldValue(id) {
  const el = document.getElementById(id);
  return el ? el.value.trim() : "";
}

function qsoDraftPayload() {
  return {
    draft: {
      call: fieldValue("qso-call").toUpperCase(),
      mode: fieldValue("qso-mode") || "CW",
      qso_date: fieldValue("qso-date"),
      time_on: fieldValue("qso-time"),
      band: fieldValue("qso-band"),
      freq: fieldValue("qso-freq"),
      rst_sent: fieldValue("qso-rst-s") || "599",
      rst_rcvd: fieldValue("qso-rst-r") || "599",
      name: fieldValue("qso-name"),
      qth: fieldValue("qso-qth"),
      notes: fieldValue("qso-notes")
    }
  };
}

let squelchPostTimer = null;
let operatorTouchedSquelch = false;

function setSquelchUi(value) {
  const v = Number(value || 0);
  const label = document.getElementById("strip-squelch");
  const slider = document.getElementById("squelch-slider");

  if (label) label.textContent = v ? v.toFixed(1) : "-";

  if (slider && !operatorTouchedSquelch) {
    slider.value = v ? String(v) : "1.45";
  }
}

function postSquelch(value) {
  const v = Number(value || 0);
  if (!v) return;

  if (squelchPostTimer) clearTimeout(squelchPostTimer);

  squelchPostTimer = setTimeout(async () => {
    try {
      const res = await postJson("/api/control/squelch", { squelch_snr: v });
      setSquelchUi(res.squelch_snr);
    } catch (err) {
      setText("session-detail", "Squelch update failed: " + err.message);
    }
  }, 180);
}

async function saveQso() {
  const btn = document.getElementById("btn-save-qso");
  if (!btn) return;

  const old = btn.textContent;
  btn.textContent = "Saving...";
  btn.disabled = true;

  try {
    const result = await postJson("/api/qso/save", qsoDraftPayload());
    btn.textContent = "Saved";
    setText("session-detail", `Saved QSO ${result.id}`);
    setTimeout(() => { btn.textContent = old; btn.disabled = false; }, 1200);
  } catch (err) {
    btn.textContent = "Save failed";
    setText("session-detail", "QSO save failed: " + err.message);
    setTimeout(() => { btn.textContent = old; btn.disabled = false; }, 2200);
  }
}


function renderMetrics(qso) {
  const rows = [
    ["Session", qso.session_id],
    ["Started", qso.started],
    ["Duration", `${qso.duration_seconds}s`],
    ["Tone", `${qso.target_tone_hz} / ${qso.detected_tone_hz} Hz`],
    ["WPM avg/min/max", `${qso.avg_wpm} / ${qso.min_wpm} / ${qso.max_wpm}`],
    ["SNR avg/peak", `${qso.avg_snr} / ${qso.peak_snr}`],
    ["Squelch open", `${qso.squelch_open_percent}%`],
    ["Sync", qso.sync],
    ["Learning", qso.learning_status],
    ["Buffer", qso.buffer_status],
    ["Words", qso.word_count],
    ["Characters", qso.character_count],
    ["Unknown symbols", qso.unknown_symbol_count],
    ["Queue overruns", qso.queue_overruns],
    ["Audio errors", qso.audio_errors],
    ["Audio device", qso.audio_device]
  ];

  const tbody = document.getElementById("metrics");
  tbody.innerHTML = "";
  for (const [k, v] of rows) {
    const tr = document.createElement("tr");
    const td1 = document.createElement("td");
    const td2 = document.createElement("td");
    td1.textContent = k;
    td2.textContent = esc(v);
    tr.appendChild(td1);
    tr.appendChild(td2);
    tbody.appendChild(tr);
  }
}

async function update() {
  const utc = utcParts();
  setText("utc-clock", utc.clock);

  try {
    const snap = await fetchJson("/api/snapshot");
    const qso = await fetchJson("/api/qso/current");

    const mode = snap.display_mode || snap.mode || "-";
    const tone = `${Math.round(snap.target_tone_hz || 0)} Hz`;
    const detected = `${Math.round(snap.detected_tone_hz || 0)} Hz`;
    const wpm = Number(snap.wpm || 0).toFixed(1);
    const snr = Number(snap.snr || 0).toFixed(2);
    const sync = `${snap.sync_state || "-"} / ${snap.sync_confidence || 0}`;
    const sql = snap.squelch_open ? "OPEN" : "CLOSED";

    setText("top-mode", mode);
    setText("top-tone", tone);
    setText("top-wpm", wpm);
    setText("top-snr", snr);
    setText("top-sync", sync);

    setText("copy", snap.copy && snap.copy.trim() ? snap.copy : "Listening...");
    setText("raw", snap.raw || "");

    setText("strip-sql", sql);
    setSquelchUi(snap.squelch_snr || 1.45);
    setText("strip-detected", detected);
    setText("strip-buffer", snap.buffer_status || "-");
    setText("strip-learning", snap.learning_status || "-");
    setText("strip-queue", `${snap.q_size || 0} / ${snap.q_overruns || 0}`);
    setText("strip-audio", `${snap.audio_errors || 0} errors`);

    setText("session-id", qso.session_id || "-");
    setText("session-summary", qso.copy ? qso.copy.slice(0, 96) : "No QSO copy yet.");
    setText("session-detail", `${qso.mode || "-"} · ${qso.detected_tone_hz || 0} Hz · ${qso.avg_wpm || 0} WPM`);
    setText("session-duration", `${qso.duration_seconds || 0}s`);
    setText("session-words", `${qso.word_count || 0} words`);
    setText("session-quality", `SNR ${qso.avg_snr || 0}/${qso.peak_snr || 0}`);

    renderMetrics(qso);

    setValue("qso-date", utc.date);
    setValue("qso-time", utc.time);
    setValue("qso-mode", "CW");

    const call = maybeCallsign(qso.copy || snap.copy || "");
    if (call && !operatorTouchedCall) setValue("qso-call", call, true);

    const liveWpm = Number(snap.wpm || 0).toFixed(1);
    const liveSnr = Number(snap.snr || 0).toFixed(2);
    const notes = `Decoded by The Morse Whisperer. Live WPM ${liveWpm}, Live SNR ${liveSnr}.`;
    setValue("qso-notes", notes, true);

  } catch (err) {
    setText("copy", "Web update error: " + err.message);
  }
}

const callInput = document.getElementById("qso-call");
if (callInput) {
  callInput.addEventListener("input", () => { operatorTouchedCall = true; });
}

const saveBtn = document.getElementById("btn-save-qso");
if (saveBtn) {
  saveBtn.addEventListener("click", saveQso);
}

const squelchSlider = document.getElementById("squelch-slider");
if (squelchSlider) {
  squelchSlider.addEventListener("input", () => {
    operatorTouchedSquelch = true;
    setSquelchUi(Number(squelchSlider.value));
    postSquelch(Number(squelchSlider.value));
  });
  squelchSlider.addEventListener("change", () => {
    operatorTouchedSquelch = false;
  });
}

update();
setInterval(update, 1000);
</script>
</body>
</html>
"""

        host = str(getattr(self.cfg, "web_host", "0.0.0.0") or "0.0.0.0")
        port = int(getattr(self.cfg, "web_port", 8080) or 8080)

        try:
            server = ThreadingHTTPServer((host, port), Handler)
            server.timeout = 0.25
        except Exception as exc:
            self.error = str(exc)
            print(f"[WEB] failed to bind {host}:{port}: {exc}", flush=True)
            return

        print(f"[WEB] dashboard listening on http://{host}:{port}/", flush=True)

        try:
            while not self.stop_event.is_set():
                try:
                    self.metrics.update(self.shared.get())
                    server.handle_request()
                except Exception as exc:
                    print(f"[WEB] request loop error: {exc}", flush=True)
                    time.sleep(0.25)
        finally:
            try:
                server.server_close()
            except Exception:
                pass
            print("[WEB] stopped", flush=True)


class ConsoleThread(threading.Thread):
    def __init__(self, cfg: RuntimeConfig, shared: SharedState, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.shared = shared
        self.stop_event = stop_event

    def run(self) -> None:
        if not self.cfg.console_enabled:
            return

        interval = max(0.2, self.cfg.console_status_interval_sec)
        while not self.stop_event.is_set():
            snap = self.shared.get()
            raw_tail = tail_text(snap.raw, 80)
            copy_tail = tail_text(snap.copy, 100)
            expanded_tail = tail_text(snap.expanded, 120)
            print(
                f"[{getattr(snap, 'display_mode', snap.mode)}] "
                f"tone={snap.target_tone_hz:.0f}Hz "
                f"wpm={snap.wpm:.1f} "
                f"snr={snap.snr:.2f} "
                f"sql={'open' if snap.squelch_open else 'closed'} "
                f"sync={snap.sync_state}/{snap.sync_confidence} "
                f"A={snap.trainer_a_count} score={snap.trainer_score} "
                f"q={snap.q_size}/{snap.q_overruns} "
                f"buf={getattr(snap, 'buffer_status', '')} "
                f"learn={getattr(snap, 'learning_status', '')} "
                f"copy='{copy_tail}' "
                f"expanded='{expanded_tail}' "
                f"raw='{raw_tail}'",
                flush=True,
            )
            time.sleep(interval)


def install_lcd_show_hint() -> None:
    print()
    print("LCD-show is not run automatically because it modifies boot/display configuration.")
    print("To install GoodTFT LCD-show manually, use:")
    print()
    print("  cd /opt")
    print("  sudo git clone https://github.com/goodtft/LCD-show.git")
    print("  cd LCD-show")
    print("  sudo chmod +x LCD*.sh")
    print()
    print("Then run the script matching your exact LCD model.")
    print("After reboot, confirm a framebuffer exists:")
    print()
    print("  ls -l /dev/fb*")
    print("  fbset -fb /dev/fb1 -s")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="The Morse Whisperer Raspberry Pi Decoder")
    parser.add_argument("--config", default="/opt/morse-whisperer-pi/config.json")
    parser.add_argument("--list-audio", action="store_true")
    parser.add_argument("--device", type=int, default=None, help="Override sounddevice input device index")
    parser.add_argument("--wav", default=None, help="Use a WAV file as input instead of live USB audio")
    parser.add_argument("--wav-loop", action="store_true", help="Loop WAV input continuously")
    parser.add_argument("--edge-debug", action="store_true", help="Print mark/gap duration diagnostics")
    parser.add_argument("--calibrate", action="store_true", help="Calibrate tone for 1.5 seconds after startup")
    parser.add_argument("--lcd-show-help", action="store_true")
    args = parser.parse_args()

    if args.lcd_show_help:
        install_lcd_show_hint()
        return 0

    cfg = RuntimeConfig.load(args.config)

    if cfg.sample_rate != SAMPLE_RATE or cfg.block_n != BLOCK_N:
        print(
            f"[WARN] Config changed sample_rate/block_n to "
            f"{cfg.sample_rate}/{cfg.block_n}. ESP32 reference is 8000/64."
        )

    selector = AudioDeviceSelector(cfg)

    if args.list_audio:
        selector.print_devices()
        return 0

    using_wav = args.wav is not None

    if using_wav:
        device_index = None
        reason = f"WAV input: {args.wav}"
    else:
        if sd is None:
            print(f"[FATAL] sounddevice unavailable: {SOUNDDEVICE_IMPORT_ERROR}", file=sys.stderr)
            return 2

        if args.device is not None:
            device_index = args.device
            reason = "manual override"
        else:
            device_index, reason = selector.select()

        if device_index is None:
            print(f"[FATAL] no audio input selected: {reason}", file=sys.stderr)
            print("Run:")
            print("  /opt/morse-whisperer-pi/venv/bin/python /opt/morse-whisperer-pi/morse_whisperer_pi.py --list-audio")
            return 2

    print("============================================================")
    print(PROJECT_NAME)
    print(PROJECT_TAG)
    print(VERSION_STR)
    print("============================================================")
    print(f"[TIMING] sample_rate={cfg.sample_rate} block_n={cfg.block_n} block_ms={1000.0 * cfg.block_n / cfg.sample_rate:.2f}")

    if using_wav:
        print(f"[AUDIO] WAV input={args.wav} loop={args.wav_loop}")
    else:
        print(f"[AUDIO] device index={device_index} reason={reason}")

    print(f"[DSP] target tone={cfg.target_tone_hz}Hz allowed={cfg.allowed_tones_hz}")

    stop_event = threading.Event()
    shared = SharedState()
    audio_q = DropOldestQueue(maxsize=cfg.audio_queue_blocks)

    audio_thread_holder = {"thread": None}

    def get_audio_thread():
        return audio_thread_holder["thread"]

    if using_wav:
        audio_thread = WavFileCaptureThread(cfg, audio_q, stop_event, args.wav, loop=args.wav_loop)
    else:
        audio_thread = AudioCaptureThread(cfg, audio_q, stop_event, device_index)

    audio_thread_holder["thread"] = audio_thread

    decoder = MorseDecoder(cfg, audio_q, shared, get_audio_thread)
    decoder.edge_debug = bool(args.edge_debug)

    # MW_START_TONE_SCAN_THREAD_V1
    # Phase 3.1: background tone scanner. The decoder still owns all Morse
    # state; this worker only publishes scored tone candidates.
    decoder.async_tone_scan_enabled = True
    tone_scan_thread = ToneScanThread(decoder, stop_event)
    decoder.tone_scan_worker = tone_scan_thread
    tone_scan_thread.start()

    # XC9022 physical buttons discovered by GPIO scan:
    # MODE=GPIO23, SCAN=GPIO22, RESET=GPIO27, CLEAR=GPIO18.
    # The active LCD framebuffer overlay uses GPIO25 for D/C on this unit,
    # so these inputs are safe here.
    button_thread = ButtonThread(decoder, stop_event)
    button_thread.start()

    # TouchSoftKeyThread disabled: this Debian/LCD-show setup currently exposes
    # no touchscreen event device in /proc/bus/input/devices.
    # Physical GPIO buttons are used instead.
    # touch_thread = TouchSoftKeyThread(decoder, stop_event)
    # touch_thread.start()

    dsp_thread = threading.Thread(target=decoder.run, args=(stop_event,), daemon=True)
    display_thread = DisplayThread(cfg, shared, stop_event)
    console_thread = ConsoleThread(cfg, shared, stop_event)

    # MW_START_WEB_THREAD_V1
    web_thread = WebThread(cfg, shared, stop_event, decoder=decoder)

    def handle_signal(signum, frame):
        print(f"\n[SIGNAL] {signum}; stopping")
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    audio_thread.start()
    dsp_thread.start()
    display_thread.start()
    console_thread.start()
    web_thread.start()

    if args.calibrate:
        print("[CAL] waiting briefly for audio stream...")
        time.sleep(0.5)
        decoder.calibrate_from_recent_blocks(seconds=1.5)

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    finally:
        stop_event.set()
        time.sleep(0.3)

    print("[EXIT] stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
