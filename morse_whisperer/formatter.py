import re


def normalise_raw_text(text: str) -> str:
    """Normalise display whitespace only. Do not infer missing words."""
    text = (text or "").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def format_copy(raw: str) -> str:
    """
    Honest COPY formatter.

    This deliberately avoids semantic repair. It must not:
      - invent words
      - join broken words because they look familiar
      - remove a V/VVV preamble
      - repair callsigns from context
      - turn spaced digits into prosigns or numbers

    RAW remains the source of truth, and COPY is only a cleaner display of the
    decoder's actual symbol output.
    """
    return normalise_raw_text(raw).upper()
