from __future__ import annotations

import socket
import threading
import time
import traceback
from dataclasses import asdict
from typing import Dict, List, Optional

import numpy as np

from .audio import AudioRing, start_capture
from .buttons import SafeTwoButtonMonitor
from .config import load_config
from .display import FramebufferDisplay
from .dsp import DecodeResult, analyse_samples
from .state import SharedState
from .web import create_app


class MorseWhisperer:
    def __init__(self, config: Dict):
        self.config = config
        self.sample_rate = int(config.get("sample_rate", 8000))
        self.state = SharedState()
        self.state.update(config=config)

        self.ring = AudioRing(self.sample_rate, float(config.get("audio_queue_seconds", 20)))
        self.capture = None
        self.stop_event = threading.Event()

        self.last_good_copy = ""
        self.last_good_raw = ""
        self.last_good_at = 0.0

        self.session_chunks: List[np.ndarray] = []
        self.session_samples = 0
        self.last_total_samples: Optional[int] = None
        self.last_activity_at = 0.0
        self.session_started_at = 0.0

        self.squelch_open = False
        self.session_tone_hz: Optional[int] = None
        self.session_tone_reason = "none"

        self.last_candidate_copy = ""
        self.last_candidate_raw = ""
        self.last_good_events = []
        self.last_good_quality = {}

        self.display = None
        self.buttons = None

        # Updated by /api/reset in the web UI. The decoder loop watches this
        # so a reset clears internal stable copy/session state, not just HTML.
        self.last_reset_requested_at = 0.0

        # Updated by /api/tone/scan or Button 2. The decoder loop watches this
        # and forces the next live session to reacquire tone from current audio.
        self.last_tone_scan_requested_at = 0.0

    @staticmethod
    def local_ip() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def tone_mode(self) -> str:
        return str(self.config.get("tone_mode", "fixed")).lower()

    def dynamic_tone_enabled(self) -> bool:
        return self.tone_mode() in ("auto", "session_auto", "dynamic", "session")

    def session_config(self) -> Dict:
        cfg = dict(self.config)
        if self.session_tone_hz is not None:
            cfg["tone_mode"] = "fixed"
            cfg["target_tone_hz"] = int(self.session_tone_hz)
        return cfg

    def next_display_page(self) -> None:
        if getattr(self, "display", None) is not None:
            self.display.next_page()
        else:
            self.state.append_status("TFT page requested but display is not active")

    def request_full_reset(self) -> None:
        self.clear_live_session(clear_tone=True)
        self.clear_accepted_copy()
        self.last_candidate_copy = ""
        self.last_candidate_raw = ""

        self.display = None
        self.buttons = None
        self.last_total_samples = None
        self.ring.clear()

        dec = {
            "raw": "",
            "copy": "",
            "stable_copy": "",
            "stable_raw": "",
            "candidate_raw": "",
            "candidate_copy": "",
            "events": [],
            "accepted": False,
            "live_mode": "button_reset",
        }

        q = self.result_quality_dict(None)
        q["reason"] = "button_reset"
        q["recent_activity"] = False
        q["quiet_for_sec"] = 0.0

        self.state.update(mode="running", decode=dec, quality=q)
        self.state.append_status("Decoder reset: copy/session/buffer cleared")

    def toggle_display_freeze(self) -> None:
        if getattr(self, "display", None) is not None:
            self.display.toggle_freeze()
        else:
            self.state.append_status("TFT freeze requested but display is not active")

    def start(self) -> None:
        self.state.update(mode="starting")
        self.capture = start_capture(self.ring, self.config)
        ai = asdict(self.capture.info())
        self.state.merge("audio", ai)
        self.state.append_status(f"Audio capture started: {ai}")

        decoder_thread = threading.Thread(target=self.decode_loop, daemon=True)
        decoder_thread.start()

        self.display = FramebufferDisplay(self.config, self.state)
        self.display.start()

        self.buttons = SafeTwoButtonMonitor(
            self.config,
            self.state,
            on_reset=self.request_full_reset,
            on_restart=lambda: None,
            on_next_page=self.next_display_page,
            on_toggle_freeze=self.toggle_display_freeze,
        )
        self.buttons.start()

        app = create_app(self.state, self.ring, self.config)
        host = str(self.config.get("web_host", "0.0.0.0"))
        port = int(self.config.get("web_port", 8080))

        self.state.update(mode="running")
        self.state.append_status(f"Web UI ready: http://{self.local_ip()}:{port}")
        app.run(host=host, port=port, threaded=True, use_reloader=False)

    def clear_live_session(self, clear_tone: bool = True) -> None:
        self.session_chunks.clear()
        self.session_samples = 0
        self.last_activity_at = 0.0
        self.session_started_at = 0.0
        self.squelch_open = False
        if clear_tone:
            self.session_tone_hz = None
            self.session_tone_reason = "none"

    def clear_accepted_copy(self) -> None:
        self.last_good_copy = ""
        self.last_good_raw = ""
        self.last_good_at = 0.0
        self.last_good_events = []
        self.last_good_quality = {}

    def lock_session_tone(self, result: DecodeResult | None) -> None:
        fallback = int(self.config.get("target_tone_hz", 700))

        if not self.dynamic_tone_enabled():
            self.session_tone_hz = fallback
            self.session_tone_reason = "fixed"
            return

        if result is None:
            return

        ratio = float(result.winner_ratio or 0.0)
        snr = float(result.snr_db or 0.0)
        min_ratio = float(self.config.get("session_auto_min_ratio", 4.0))
        min_snr = float(self.config.get("session_auto_min_snr", 8.0))

        if result.selected_tone_hz and ratio >= min_ratio and snr >= min_snr:
            self.session_tone_hz = int(result.selected_tone_hz)
            self.session_tone_reason = f"auto ratio={ratio:.1f} snr={snr:.1f}"
        else:
            self.session_tone_hz = fallback
            self.session_tone_reason = f"fallback target ratio={ratio:.1f} snr={snr:.1f}"

    def live_session_array(self) -> np.ndarray:
        if not self.session_chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self.session_chunks).astype(np.float32, copy=False)

    def append_live_audio(self, new_audio: np.ndarray) -> None:
        if new_audio.size == 0:
            return
        if self.session_samples == 0:
            self.session_started_at = time.time()
        self.session_chunks.append(new_audio.astype(np.float32, copy=False).copy())
        self.session_samples += int(new_audio.size)

        max_sec = float(self.config.get("live_session_max_sec", 45))
        max_samples = int(max_sec * self.sample_rate)
        while self.session_samples > max_samples and self.session_chunks:
            old = self.session_chunks.pop(0)
            self.session_samples -= int(old.size)

    def new_audio_since_last_loop(self) -> np.ndarray:
        stats = self.ring.stats()
        total = int(stats.get("total_samples", 0))
        buffered = int(stats.get("buffered_samples", 0))

        if buffered == 0:
            self.clear_live_session()
            self.clear_accepted_copy()
            self.last_total_samples = total
            return np.zeros(0, dtype=np.float32)

        if self.last_total_samples is None:
            new_count = min(
                buffered,
                int(float(self.config.get("decode_window_sec", 10)) * self.sample_rate),
            )
        else:
            delta = max(0, total - int(self.last_total_samples))
            new_count = min(buffered, delta)

        self.last_total_samples = total

        if new_count <= 0:
            return np.zeros(0, dtype=np.float32)

        return self.ring.last(new_count / float(self.sample_rate))

    def audio_is_active(self, result: DecodeResult, audio: Dict) -> bool:
        if not bool(self.config.get("live_noise_gate_enabled", True)):
            return True

        min_rms = float(self.config.get("live_active_min_rms", 0.012))
        min_peak = float(self.config.get("live_active_min_peak", 0.05))
        min_snr = float(self.config.get("live_active_snr", 10.0))
        min_marks = int(self.config.get("live_active_min_marks", 2))

        rms = float(audio.get("rms") or 0.0)
        peak = float(audio.get("peak") or 0.0)

        if rms < min_rms and peak < min_peak:
            return False
        if result.snr_db < min_snr:
            return False
        if result.marks < min_marks:
            return False
        return True

    def is_publishable(self, result: DecodeResult) -> bool:
        min_conf = float(self.config.get("copy_min_confidence", 0.85))
        min_snr = float(self.config.get("copy_min_snr", self.config.get("squelch_snr", 3.5)))
        min_symbols = int(self.config.get("copy_min_decoded_symbols", 5))
        max_failed = int(self.config.get("copy_max_failed_symbols", 0))

        if not result.copy:
            return False
        if result.reason not in ("ok", "target_tone_mismatch"):
            return False
        if result.target_detected_mismatch and not self.dynamic_tone_enabled():
            return False
        if result.confidence < min_conf:
            return False
        if result.snr_db < min_snr:
            return False
        if result.decoded_symbols < min_symbols:
            return False
        if result.failed_symbols > max_failed:
            return False
        if "#" in result.copy and max_failed == 0:
            return False
        return True

    def should_replace_copy(self, candidate: str) -> bool:
        candidate = (candidate or "").strip()
        old = (self.last_good_copy or "").strip()

        if not old:
            return True

        if bool(self.config.get("live_publish_growth_only", True)):
            compact_candidate = candidate.replace(" ", "")
            compact_old = old.replace(" ", "")
            if compact_candidate.startswith(compact_old) and len(compact_candidate) >= len(compact_old):
                return True
            if len(compact_candidate) >= len(compact_old) + 3:
                return True
            return False

        return True

    def result_quality_dict(self, result: DecodeResult | None) -> Dict:
        base = {
            "live_mode": "session_auto_tone" if self.dynamic_tone_enabled() else "session_fixed_tone",
            "squelch_open": self.squelch_open,
            "live_tone_lock_hz": self.session_tone_hz,
            "live_tone_lock_reason": self.session_tone_reason,
            "live_session_seconds": self.session_samples / float(self.sample_rate),
            "live_session_samples": self.session_samples,
        }

        if result is None:
            base.update({
                "selected_tone_hz": None,
                "target_tone_hz": int(self.config.get("target_tone_hz", 700)),
                "tone_mode": self.tone_mode(),
                "target_detected_mismatch": False,
                "winner_ratio": 0.0,
                "snr_db": 0.0,
                "confidence": 0.0,
                "dot_ms": 0.0,
                "wpm": 0.0,
                "threshold": 0.0,
                "low_threshold": 0.0,
                "high_threshold": 0.0,
                "tone_ranking": [],
                "marks": 0,
                "spaces": 0,
                "events": [],
                "decoded_symbols": 0,
                "failed_symbols": 0,
                "reason": "no_audio",
            })
            return base

        base.update({
            "selected_tone_hz": result.selected_tone_hz,
            "target_tone_hz": result.target_tone_hz,
            "tone_mode": result.tone_mode,
            "target_detected_mismatch": result.target_detected_mismatch,
            "winner_ratio": result.winner_ratio,
            "snr_db": result.snr_db,
            "confidence": result.confidence,
            "dot_ms": result.dot_ms,
            "wpm": result.wpm,
            "threshold": result.threshold,
            "low_threshold": result.low_threshold,
            "high_threshold": result.high_threshold,
            "tone_ranking": result.tone_ranking,
            "marks": result.marks,
            "spaces": result.spaces,
            "events": result.events,
            "decoded_symbols": result.decoded_symbols,
            "failed_symbols": result.failed_symbols,
            "reason": result.reason,
        })
        return base

    def update_state_blank_or_stable(self, audio: Dict, quality: Dict, candidate: DecodeResult | None = None) -> None:
        show_candidates = bool(self.config.get("show_rejected_candidates", False))

        dec = {
            "raw": self.last_good_raw,
            "copy": self.last_good_copy,
            "stable_copy": self.last_good_copy,
            "stable_raw": self.last_good_raw,
            "candidate_raw": candidate.raw if show_candidates and candidate is not None else "",
            "candidate_copy": candidate.copy if show_candidates and candidate is not None else "",
            "events": list(getattr(self, "last_good_events", [])),
            "accepted_events": list(getattr(self, "last_good_events", [])),
            "stable_quality": dict(getattr(self, "last_good_quality", {})),
            "accepted": False,
            "live_mode": quality.get("live_mode", ""),
        }
        self.state.update(mode="running", decode=dec, quality=quality, audio=audio)

    def check_external_tone_scan(self) -> None:
        snap = self.state.snapshot()
        control = snap.get("control", {}) if isinstance(snap, dict) else {}

        try:
            requested_at = float(control.get("tone_scan_requested_at") or 0.0)
        except Exception:
            requested_at = 0.0

        if requested_at <= 0.0 or requested_at <= self.last_tone_scan_requested_at:
            return

        self.last_tone_scan_requested_at = requested_at

        # Force a fresh tone acquisition from current live audio.
        # Do not clear accepted COPY unless the operator explicitly presses
        # reset. Manual scan should retune, not nuke useful copy.
        self.clear_live_session(clear_tone=True)
        self.session_tone_hz = None
        self.session_tone_reason = "manual_scan_pending"
        self.last_total_samples = None

        # Drop old buffered audio so stale tone/noise does not poison the scan.
        self.ring.clear()

        snap = self.state.snapshot()
        q = snap.get("quality", {})
        if not isinstance(q, dict):
            q = {}

        q.update({
            "live_tone_lock_hz": None,
            "live_tone_lock_reason": "manual_scan_pending",
            "selected_tone_hz": None,
            "tone_mode": "scan_pending",
            "live_session_seconds": 0,
            "live_session_samples": 0,
        })

        self.state.update(mode="running", quality=q)
        self.state.append_status("Manual tone scan acknowledged by decoder loop")

    def check_external_reset(self) -> None:
        snap = self.state.snapshot()
        control = snap.get("control", {}) if isinstance(snap, dict) else {}
        try:
            requested_at = float(control.get("reset_requested_at") or 0.0)
        except Exception:
            requested_at = 0.0

        if requested_at <= 0.0 or requested_at <= self.last_reset_requested_at:
            return

        self.last_reset_requested_at = requested_at

        # Clear all decoder-side memory. This is the bit the old web reset
        # missed, which let previous copy reappear after one refresh.
        self.clear_live_session(clear_tone=True)
        self.clear_accepted_copy()
        self.last_candidate_copy = ""
        self.last_candidate_raw = ""

        self.display = None
        self.buttons = None
        self.last_total_samples = None

        # Clear the audio ring again from the decoder side as well.
        self.ring.clear()

        dec = {
            "raw": "",
            "copy": "",
            "stable_copy": "",
            "stable_raw": "",
            "candidate_raw": "",
            "candidate_copy": "",
            "events": [],
            "accepted": False,
            "live_mode": "reset",
        }

        q = self.result_quality_dict(None)
        q["reason"] = "reset"
        q["recent_activity"] = False
        q["quiet_for_sec"] = 0.0

        self.state.update(mode="running", decode=dec, quality=q)
        self.state.append_status("Reset acknowledged by decoder loop")

    def decode_loop(self) -> None:
        update = float(self.config.get("update_interval_sec", 2.0))
        end_silence = float(self.config.get("live_end_silence_sec", 2.5))
        clear_silence = float(self.config.get("clear_after_silence_sec", 18.0))

        while not self.stop_event.is_set():
            try:
                self.check_external_reset()
                self.check_external_tone_scan()

                new_audio = self.new_audio_since_last_loop()
                stats = self.ring.stats()
                audio = dict(stats)
                session_snapshot = new_audio if new_audio.size else self.ring.last(1.5)
                recent_result = analyse_samples(session_snapshot, self.session_config()) if session_snapshot.size else None

                if recent_result is not None:
                    audio.update(recent_result.audio)

                activity = False
                if recent_result is not None:
                    self.lock_session_tone(recent_result)
                    activity = self.audio_is_active(recent_result, audio)

                now = time.time()
                if activity:
                    self.squelch_open = True
                    self.last_activity_at = now
                    self.append_live_audio(new_audio)
                else:
                    self.squelch_open = False

                if (
                    self.last_good_copy
                    and self.last_good_at
                    and (now - self.last_good_at) > clear_silence
                    and not self.squelch_open
                    and audio.get("level_status") in ("IDLE", "LOW")
                ):
                    self.clear_accepted_copy()

                session = self.live_session_array()

                if session.size < int(self.sample_rate * 0.5):
                    q = self.result_quality_dict(recent_result)
                    q["recent_activity"] = activity
                    q["quiet_for_sec"] = now - self.last_activity_at if self.last_activity_at else 0.0
                    self.update_state_blank_or_stable(audio, q, recent_result)
                    time.sleep(update)
                    continue

                result = analyse_samples(session, self.session_config())
                accepted = self.is_publishable(result)

                self.last_candidate_copy = result.copy
                self.last_candidate_raw = result.raw

                max_events = int(self.config.get("max_events_in_snapshot", 80))
                events = result.events[-max_events:] if max_events > 0 else []

                if accepted and self.should_replace_copy(result.copy):
                    self.last_good_copy = result.copy
                    self.last_good_raw = result.raw
                    self.last_good_at = now
                    self.last_good_events = list(events)
                    self.last_good_quality = {
                        "dot_ms": result.dot_ms,
                        "wpm": result.wpm,
                        "selected_tone_hz": result.selected_tone_hz,
                        "target_tone_hz": result.target_tone_hz,
                        "tone_mode": result.tone_mode,
                        "winner_ratio": result.winner_ratio,
                        "snr_db": result.snr_db,
                        "confidence": result.confidence,
                        "marks": result.marks,
                        "spaces": result.spaces,
                        "decoded_symbols": result.decoded_symbols,
                        "failed_symbols": result.failed_symbols,
                        "reason": result.reason,
                    }

                q = self.result_quality_dict(result)
                q["recent_activity"] = activity
                q["quiet_for_sec"] = now - self.last_activity_at if self.last_activity_at else 0.0

                show_candidates = bool(self.config.get("show_rejected_candidates", False))

                dec = {
                    "raw": self.last_good_raw,
                    "copy": self.last_good_copy,
                    "stable_copy": self.last_good_copy,
                    "stable_raw": self.last_good_raw,
                    "candidate_raw": result.raw if show_candidates else "",
                    "candidate_copy": result.copy if show_candidates else "",
                    "events": events if accepted else list(getattr(self, "last_good_events", [])),
                    "accepted_events": list(getattr(self, "last_good_events", [])),
                    "stable_quality": dict(getattr(self, "last_good_quality", {})),
                    "accepted": accepted,
                    "live_mode": q.get("live_mode", ""),
                }

                self.state.update(mode="running", decode=dec, quality=q, audio=audio)

            except Exception as e:
                self.state.append_status(f"Decode loop error: {e}")
                self.state.append_status(traceback.format_exc().splitlines()[-1])

            time.sleep(update)


def main() -> None:
    cfg = load_config()
    mw = MorseWhisperer(cfg)
    mw.start()


if __name__ == "__main__":
    main()
