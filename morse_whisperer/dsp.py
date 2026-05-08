from __future__ import annotations
import math
from dataclasses import dataclass, asdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
import numpy as np

MORSE_TABLE = {
    ".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E", "..-.": "F",
    "--.": "G", "....": "H", "..": "I", ".---": "J", "-.-": "K", ".-..": "L",
    "--": "M", "-.": "N", "---": "O", ".--.": "P", "--.-": "Q", ".-.": "R",
    "...": "S", "-": "T", "..-": "U", "...-": "V", ".--": "W", "-..-": "X",
    "-.--": "Y", "--..": "Z",
    ".----": "1", "..---": "2", "...--": "3", "....-": "4", ".....": "5",
    "-....": "6", "--...": "7", "---..": "8", "----.": "9", "-----": "0",
    ".-.-.-": ".", "--..--": ",", "..--..": "?", ".----.": "'", "-.-.--": "!",
    "-..-.": "/", "-.--.": "(", "-.--.-": ")", ".-...": "&", "---...": ":",
    "-.-.-.": ";", "-...-": "=", ".-.-.": "+", "-....-": "-", "..--.-": "_",
    ".-..-.": '"', "...-..-": "$", ".--.-.": "@", "...---...": "SOS",
}

@dataclass
class AudioMetrics:
    rms: float
    peak: float
    clipping_percent: float
    dc_offset: float
    level_status: str

@dataclass
class ToneScore:
    tone_hz: int
    score: float

@dataclass
class Event:
    kind: str  # mark or space
    ms: float
    start_ms: float
    end_ms: float

@dataclass
class DecodeResult:
    raw: str
    copy: str
    selected_tone_hz: int
    target_tone_hz: int
    tone_mode: str
    target_detected_mismatch: bool
    winner_ratio: float
    snr_db: float
    confidence: float
    dot_ms: float
    wpm: float
    threshold: float
    low_threshold: float
    high_threshold: float
    audio: Dict[str, float | str]
    tone_ranking: List[Dict[str, float]]
    events: List[Dict[str, float | str]]
    marks: int
    spaces: int
    decoded_symbols: int
    failed_symbols: int
    reason: str = ""

def _as_float_audio(samples: np.ndarray) -> np.ndarray:
    arr = np.asarray(samples)
    if arr.ndim > 1:
        arr = arr[:, 0]
    if arr.dtype.kind in "iu":
        info = np.iinfo(arr.dtype)
        scale = float(max(abs(info.min), info.max))
        arr = arr.astype(np.float32) / scale
    else:
        arr = arr.astype(np.float32)
    if arr.size == 0:
        return np.zeros(0, dtype=np.float32)
    arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
    return np.clip(arr, -1.0, 1.0)

def audio_metrics(samples: np.ndarray) -> AudioMetrics:
    x = _as_float_audio(samples)
    if x.size == 0:
        return AudioMetrics(0.0, 0.0, 0.0, "IDLE")
    peak = float(np.max(np.abs(x)))
    rms = float(np.sqrt(np.mean(x * x)))
    clip = float(np.mean(np.abs(x) >= 0.985) * 100.0)
    dc = float(np.mean(x))
    if clip > 0.05:
        status = "CLIP"
    elif peak >= 0.92 or rms >= 0.35:
        status = "HOT"
    elif rms >= 0.010 and peak >= 0.035:
        status = "GOOD"
    elif rms >= 0.003 or peak >= 0.012:
        status = "LOW"
    else:
        status = "IDLE"
    return AudioMetrics(rms=rms, peak=peak, clipping_percent=clip, dc_offset=dc, level_status=status)

def tone_power(samples: np.ndarray, sample_rate: int, tone_hz: float) -> float:
    x = _as_float_audio(samples)
    if x.size == 0:
        return 0.0
    x = x - float(np.mean(x))
    n = x.size
    t = np.arange(n, dtype=np.float32) / float(sample_rate)
    win = np.hanning(n).astype(np.float32) if n > 8 else np.ones(n, dtype=np.float32)
    xw = x * win
    c = float(np.dot(xw, np.cos(2.0 * math.pi * tone_hz * t)))
    s = float(np.dot(xw, np.sin(2.0 * math.pi * tone_hz * t)))
    return (c * c + s * s) / max(1.0, float(np.dot(win, win)))

def rank_tones(samples: np.ndarray, sample_rate: int, tones: Sequence[int]) -> List[ToneScore]:
    scores = [ToneScore(int(t), float(tone_power(samples, sample_rate, t))) for t in tones]
    return sorted(scores, key=lambda z: z.score, reverse=True)

def frame_envelope(samples: np.ndarray, sample_rate: int, tone_hz: int, window_ms: float, hop_ms: float) -> Tuple[np.ndarray, np.ndarray]:
    x = _as_float_audio(samples)
    if x.size == 0:
        return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)
    x = x - float(np.mean(x))
    win_n = max(16, int(round(sample_rate * window_ms / 1000.0)))
    hop_n = max(1, int(round(sample_rate * hop_ms / 1000.0)))
    if x.size < win_n:
        return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)
    win = np.hanning(win_n).astype(np.float32)
    n_frames = 1 + (x.size - win_n) // hop_n
    env = np.empty(n_frames, dtype=np.float32)
    times = np.empty(n_frames, dtype=np.float32)
    base_t = np.arange(win_n, dtype=np.float32) / float(sample_rate)
    cosv = np.cos(2.0 * math.pi * tone_hz * base_t).astype(np.float32) * win
    sinv = np.sin(2.0 * math.pi * tone_hz * base_t).astype(np.float32) * win
    norm = max(1e-9, float(np.sum(win)))
    for i in range(n_frames):
        start = i * hop_n
        seg = x[start:start + win_n]
        c = float(np.dot(seg, cosv))
        s = float(np.dot(seg, sinv))
        env[i] = math.sqrt(c*c + s*s) / norm
        times[i] = (start + win_n / 2.0) * 1000.0 / float(sample_rate)
    if env.size >= 3:
        env = np.convolve(env, np.array([0.25, 0.5, 0.25], dtype=np.float32), mode="same")
    return env, times

def schmitt_events(env: np.ndarray, times_ms: np.ndarray, threshold_bias: float, hop_ms: float, event_comp_ms: float = 0.0) -> Tuple[List[Event], float, float, float]:
    if env.size == 0:
        return [], 0.0, 0.0, 0.0
    floor = float(np.percentile(env, 20))
    ceil = float(np.percentile(env, 92))
    span = max(1e-9, ceil - floor)
    threshold = floor + float(threshold_bias) * span
    low_th = floor + max(0.05, float(threshold_bias) * 0.70) * span
    high_th = floor + min(0.95, float(threshold_bias) * 1.25) * span
    state = False
    bits: List[bool] = []
    for v in env:
        fv = float(v)
        if state:
            if fv < low_th:
                state = False
        else:
            if fv > high_th:
                state = True
        bits.append(state)
    events: List[Event] = []
    if not bits:
        return events, threshold, low_th, high_th
    start_i = 0
    cur = bits[0]
    for i in range(1, len(bits)):
        if bits[i] != cur:
            start_ms = float(times_ms[start_i] - hop_ms / 2.0)
            end_ms = float(times_ms[i - 1] + hop_ms / 2.0)
            events.append(Event("mark" if cur else "space", max(0.0, end_ms - start_ms), max(0.0, start_ms), max(0.0, end_ms)))
            start_i = i
            cur = bits[i]
    start_ms = float(times_ms[start_i] - hop_ms / 2.0)
    end_ms = float(times_ms[-1] + hop_ms / 2.0)
    events.append(Event("mark" if cur else "space", max(0.0, end_ms - start_ms), max(0.0, start_ms), max(0.0, end_ms)))
    if event_comp_ms > 0:
        events = compensate_events(events, event_comp_ms)
    events = merge_tiny_glitches(events, min_ms=max(2.0, hop_ms * 0.75))
    return events, threshold, low_th, high_th

def compensate_events(events: List[Event], comp_ms: float) -> List[Event]:
    out: List[Event] = []
    for e in events:
        ms = e.ms - comp_ms if e.kind == "mark" else e.ms + comp_ms
        out.append(Event(e.kind, max(1.0, ms), e.start_ms, e.end_ms))
    return out

def merge_tiny_glitches(events: List[Event], min_ms: float) -> List[Event]:
    if len(events) < 3:
        return events
    out = events[:]
    changed = True
    while changed:
        changed = False
        new: List[Event] = []
        i = 0
        while i < len(out):
            if 0 < i < len(out)-1 and out[i].ms < min_ms and out[i-1].kind == out[i+1].kind:
                merged = Event(out[i-1].kind, out[i-1].ms + out[i].ms + out[i+1].ms, out[i-1].start_ms, out[i+1].end_ms)
                if new:
                    new[-1] = merged
                else:
                    new.append(merged)
                i += 2
                changed = True
            else:
                new.append(out[i])
            i += 1
        out = new
    return out


def clean_events_by_dot(events: List[Event], dot_ms: float, fraction: float = 0.45) -> List[Event]:
    if len(events) < 3:
        return events
    dot = max(20.0, float(dot_ms))
    min_ms = max(12.0, dot * float(fraction))
    out: List[Event] = []
    for e in events:
        if e.ms < min_ms:
            if out and out[-1].kind != e.kind:
                prev = out[-1]
                out[-1] = Event(prev.kind, prev.ms + e.ms, prev.start_ms, e.end_ms)
            else:
                out.append(e)
        else:
            out.append(e)
    merged: List[Event] = []
    for e in out:
        if merged and merged[-1].kind == e.kind:
            prev = merged[-1]
            merged[-1] = Event(prev.kind, prev.ms + e.ms, prev.start_ms, e.end_ms)
        else:
            merged.append(e)
    changed = True
    while changed and len(merged) >= 3:
        changed = False
        new: List[Event] = []
        i = 0
        while i < len(merged):
            if 0 < i < len(merged) - 1 and merged[i].ms < min_ms and merged[i-1].kind == merged[i+1].kind:
                prev = merged[i-1]
                nxt = merged[i+1]
                combined = Event(prev.kind, prev.ms + merged[i].ms + nxt.ms, prev.start_ms, nxt.end_ms)
                if new:
                    new[-1] = combined
                else:
                    new.append(combined)
                i += 2
                changed = True
            else:
                new.append(merged[i])
            i += 1
        merged = new
    return merged

def estimate_dot_ms(events: List[Event], initial_wpm: Optional[float] = None) -> float:
    default_dot = 1200.0 / float(initial_wpm or 18.75)
    marks = np.array([e.ms for e in events if e.kind == "mark" and 35.0 <= e.ms <= 950.0], dtype=np.float32)
    if marks.size == 0:
        return default_dot
    marks.sort()
    if marks.size < 3:
        shortest = float(marks[0])
        if 45.0 <= shortest <= 360.0:
            return 0.90 * shortest + 0.10 * default_dot
        return default_dot
    lower_count = max(1, int(math.ceil(marks.size * 0.45)))
    lower = marks[:lower_count]
    med = float(np.median(lower))
    if med < 45.0:
        med = float(np.percentile(marks, 35))
    if 45.0 <= med <= 360.0:
        observed_wpm = 1200.0 / max(1.0, med)
        if observed_wpm <= 10.0:
            return 0.90 * med + 0.10 * default_dot
        return 0.75 * med + 0.25 * default_dot
    return default_dot

def decode_events(events: List[Event], dot_ms: float, char_gap_units: float = 2.25, word_gap_units: float = 5.0) -> Tuple[str, int, int, List[str]]:
    chars: List[str] = []
    cur = ""
    decoded = 0
    failed = 0
    symbols_debug: List[str] = []
    dot = max(20.0, float(dot_ms))
    char_gap = max(1.8, float(char_gap_units))
    word_gap = max(char_gap + 1.5, float(word_gap_units))

    ev = list(events)
    while ev and ev[0].kind == "space":
        ev.pop(0)
    while ev and ev[-1].kind == "space":
        ev.pop()

    def flush_char() -> None:
        nonlocal cur, decoded, failed
        if cur:
            ch = MORSE_TABLE.get(cur)
            if ch is None:
                chars.append("#")
                failed += 1
            else:
                chars.append(ch)
                decoded += 1
            symbols_debug.append(cur)
            cur = ""

    for e in ev:
        units = e.ms / dot
        if e.kind == "mark":
            if units <= 2.15:
                cur += "."
            else:
                cur += "-"
        else:
            if units >= word_gap:
                flush_char()
                if chars and chars[-1] != " ":
                    chars.append(" ")
            elif units >= char_gap:
                flush_char()
            else:
                pass
    flush_char()
    raw = "".join(chars)
    raw = " ".join(raw.split())
    return raw, decoded, failed, symbols_debug

def timing_confidence(events: List[Event], dot_ms: float) -> float:
    dot = max(20.0, dot_ms)
    scores: List[float] = []
    for e in events:
        u = e.ms / dot
        if e.kind == "mark":
            nearest = 1.0 if u < 2.0 else 3.0
        else:
            nearest = min([1.0, 3.0, 7.0], key=lambda v: abs(v-u))
        err = abs(u - nearest) / max(1.0, nearest)
        scores.append(max(0.0, 1.0 - err))
    if not scores:
        return 0.0
    return float(np.mean(scores))

def analyse_samples(samples: np.ndarray, config: Dict, tone_override: Optional[str | int] = None, wpm_override: Optional[float] = None) -> DecodeResult:
    from .formatter import format_copy

    sr = int(config.get("sample_rate", 8000))
    allowed = [int(x) for x in config.get("allowed_tones_hz", [700])]
    target = int(config.get("target_tone_hz", 700))
    tone_mode = str(config.get("tone_mode", "auto")).lower()
    if tone_override is not None:
        if str(tone_override).lower() == "auto":
            tone_mode = "auto"
        else:
            tone_mode = "fixed"
            target = int(float(tone_override))
    metrics = audio_metrics(samples)
    ranking = rank_tones(samples, sr, allowed)
    if ranking:
        selected = int(ranking[0].tone_hz) if tone_mode == "auto" else target
        winner = float(ranking[0].score)
        second = float(ranking[1].score) if len(ranking) > 1 else 1e-12
        median = float(np.median([r.score for r in ranking[1:]])) if len(ranking) > 1 else second
    else:
        selected = target
        winner = second = median = 0.0
    selected_score = float(tone_power(samples, sr, selected))
    noise_score = max(1e-12, median)
    winner_ratio = float((winner + 1e-12) / (second + 1e-12))
    snr_db = float(10.0 * math.log10((selected_score + 1e-12) / noise_score))
    mismatch = bool(tone_mode != "auto" and ranking and abs(ranking[0].tone_hz - target) >= 75 and winner_ratio >= 2.0)

    env, times = frame_envelope(samples, sr, selected, float(config.get("window_ms", 12)), float(config.get("hop_ms", 8)))
    events_raw, th, lo, hi = schmitt_events(env, times, float(config.get("threshold_bias", 0.48)), float(config.get("hop_ms", 8)), float(config.get("event_comp_ms", 0.0)))
    dot_ms = 1200.0 / float(wpm_override) if wpm_override else estimate_dot_ms(events_raw, float(config.get("initial_wpm", 18.75)))
    events = clean_events_by_dot(events_raw, dot_ms, float(config.get("min_element_fraction", 0.45)))
    if len(events) != len(events_raw):
        dot_ms = 1200.0 / float(wpm_override) if wpm_override else estimate_dot_ms(events, float(config.get("initial_wpm", 18.75)))
    wpm = 1200.0 / max(1.0, dot_ms)
    raw, decoded, failed, _symbols = decode_events(
        events,
        dot_ms,
        float(config.get("char_gap_units", 2.25)),
        float(config.get("word_gap_units", 5.0)),
    )
    copy = format_copy(raw)
    marks = sum(1 for e in events if e.kind == "mark")
    spaces = sum(1 for e in events if e.kind == "space")

    if metrics.level_status in ("IDLE", "LOW") and snr_db < float(config.get("squelch_snr", 3.5)):
        reason = "low_or_no_signal"
    elif mismatch:
        reason = "target_tone_mismatch"
    elif not raw:
        reason = "no_decodable_events"
    else:
        reason = "ok"

    decode_success = decoded / max(1, decoded + failed)
    tconf = timing_confidence(events, dot_ms)
    snr_score = max(0.0, min(1.0, (snr_db - 1.0) / 12.0))
    level_score = {"IDLE":0.0,"LOW":0.35,"GOOD":1.0,"HOT":0.65,"CLIP":0.15}.get(metrics.level_status, 0.4)
    confidence = float(max(0.0, min(1.0, 0.35*snr_score + 0.35*tconf + 0.20*decode_success + 0.10*level_score)))
    if mismatch:
        confidence *= 0.35
    if metrics.level_status == "CLIP":
        confidence *= 0.45

    max_events = int(config.get("max_events_in_snapshot", 250))
    event_dicts = [asdict(e) for e in events[-max_events:]]
    return DecodeResult(
        raw=raw,
        copy=copy,
        selected_tone_hz=selected,
        target_tone_hz=target,
        tone_mode=tone_mode,
        target_detected_mismatch=mismatch,
        winner_ratio=winner_ratio,
        snr_db=snr_db,
        confidence=confidence,
        dot_ms=dot_ms,
        wpm=wpm,
        threshold=th,
        low_threshold=lo,
        high_threshold=hi,
        audio=asdict(metrics),
        tone_ranking=[asdict(r) for r in ranking],
        events=event_dicts,
        marks=marks,
        spaces=spaces,
        decoded_symbols=decoded,
        failed_symbols=failed,
        reason=reason,
    )
