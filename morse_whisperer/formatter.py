import re

CALL_PREFIXES = r"(?:ZL|VK|K|N|W|G|M|JA|VE|VA|DL|F|I|EA|ZS)"
CALL_RE = re.compile(
    rf"\b(({CALL_PREFIXES})[0-9][A-Z0-9]{{1,4}})(({CALL_PREFIXES})[0-9][A-Z0-9]{{1,4}})\b"
)

# Conservative COPY-only spacing cleanup.
# This is not phrase repair. RAW remains untouched.
JOINED_WORD_PAIRS = [
    ("GOOD", "TO"),
    ("TO", "HEAR"),
    ("HEAR", "YOU"),
    ("YOU", "73"),
    ("TNX", "FER"),
    ("THANKS", "FOR"),
    ("HW", "CPY"),
    ("UR", "RST"),
    ("RST", "599"),
    ("RST", "579"),
    ("RST", "559"),
    ("NAME", "IS"),
    ("QTH", "IS"),
]


def normalise_raw_text(text: str) -> str:
    text = (text or "").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def strip_leading_v_preamble(s: str) -> str:
    # Strip tuning/test V preamble only at the start.
    # Handles:
    #   VVV ZL1ABC
    #   VVVVV ZL1ABC
    #   V V V ZL1ABC
    #   VVV VVVZL1ABC
    original = s
    s = s.strip()

    # Groups of V with or without spaces, only if followed by plausible traffic.
    s = re.sub(
        rf"^(?:(?:V+\s*){{1,8}})(?=({CALL_PREFIXES}|CQ|DE|[A-Z0-9]))",
        "",
        s,
    ).strip()

    # One more pass for "VVV VVVZL..."
    s = re.sub(
        rf"^(?:V{{2,}}\s*)+(?=({CALL_PREFIXES}|CQ|DE|[A-Z0-9]))",
        "",
        s,
    ).strip()

    return s or original.strip()


def split_joined_callsigns(s: str) -> str:
    for _ in range(6):
        ns = CALL_RE.sub(r"\1 \3", s)
        if ns == s:
            break
        s = ns
    return s


def split_common_joined_words(s: str) -> str:
    # Repeat because GOODTOHEAR may need GOOD TOHEAR, then TO HEAR.
    for _ in range(4):
        before = s
        for left, right in JOINED_WORD_PAIRS:
            s = re.sub(rf"\b{re.escape(left)}{re.escape(right)}\b", f"{left} {right}", s)
        if s == before:
            break
    return s


def format_copy(raw: str) -> str:
    """Operator-friendly display cleanup without phrase-specific repair."""
    s = normalise_raw_text(raw).upper()
    if not s:
        return ""

    s = strip_leading_v_preamble(s)
    s = split_joined_callsigns(s)
    s = split_common_joined_words(s)

    # Conservative generic spacing fixes.
    s = re.sub(r"([A-Z])73\b", r"\1 73", s)
    s = re.sub(r"\b73([A-Z])", r"73 \1", s)
    s = re.sub(r"(?<=\w)/(?:\s+)?(?=\w)", "/", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s
