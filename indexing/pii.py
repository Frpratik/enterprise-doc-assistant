"""
Personal data in a question (Phase 6, Stage 2): find it, replace it with a placeholder, and say WHAT KIND was found (never the value).

  "my PAN is abcde1234f, mail me at priya@acme.com"   ->   "my PAN is [PAN], mail me at [EMAIL]"      kinds: ['email', 'pan']

Where it is applied (api/main.py): at the door, before the question goes anywhere. The LLM provider, the logs and the audit table
never see the raw value. The person is told what was removed (`masked` in the response), so nothing is hidden from them.
The structured logger masks every log line the same way (defence in depth: an error message or a stack trace can carry a pasted value).

What is detected (every number is VALIDATED, so ordinary numbers are left alone):
  EMAIL     name@domain.tld
  PAN       5 letters, 4 digits, 1 letter, with a valid holder-type letter in 4th place (P, C, H, F, A, T, B, L, J, G)
  AADHAAR   12 digits, first digit 2-9, with a valid Verhoeff check digit (a random 12-digit number passes only 1 time in 10)
  CARD      13-19 digits with a valid Luhn check digit (a random one passes 1 time in 10)
  PHONE     an Indian mobile number (10 digits starting 6-9, with an optional +91 / 91 / 0 in front)
Digits may be grouped with spaces, dots or dashes ("98765 43210", "9-8-7-6-...", "4111 1111 1111 1111"): a run of digits is
joined first and classified by its length and its check digit. Full-width digits and zero-width characters are normalised first.

What is NOT detected (honest limits): names, addresses, bank account numbers, passport and voter ids, dates of birth, anything written as words
("nine eight seven...") or obfuscated ("priya at acme dot com"), and personal data in other languages' scripts. It reduces what leaves the
building; it is not a guarantee.
"""
import re
import unicodedata
from dataclasses import dataclass, field

PLACEHOLDER = {"email": "[EMAIL]", "pan": "[PAN]", "aadhaar": "[AADHAAR]", "card": "[CARD]", "phone": "[PHONE]"}
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)

_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}")
_PAN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{3}[PCHFATBLJGpchfatbljg][A-Za-z]\d{4}[A-Za-z](?![A-Za-z0-9])")
_DIGIT_RUN = re.compile(r"(?<!\d)\+?\d(?:[ .\-]?\d){9,18}(?!\d)")        # 10-19 digits (with an optional + in front), singly separated by a space, dot or dash


# ---------------- check digits ----------------
_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
      [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1], [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
      [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
      [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1], [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]


def verhoeff_valid(number: str) -> bool:
    """The check used by Aadhaar numbers (it catches every single-digit error and nearly all swaps of neighbours)."""
    c = 0
    for i, ch in enumerate(reversed(number)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def luhn_valid(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


def _classify_digits(digits: str) -> str | None:
    """What kind of personal number is this run of digits (already stripped of separators)? None = an ordinary number."""
    n = len(digits)
    if n == 12 and digits[0] in "23456789" and verhoeff_valid(digits):
        return "aadhaar"
    if 13 <= n <= 19 and luhn_valid(digits):
        return "card"
    if n == 10 and digits[0] in "6789":
        return "phone"
    if n == 11 and digits[0] == "0" and digits[1] in "6789":
        return "phone"
    if n == 12 and digits.startswith("91") and digits[2] in "6789":
        return "phone"
    if n == 13 and digits.startswith("091") and digits[3] in "6789":
        return "phone"
    return None


# ---------------- finding and masking ----------------
def value_of(raw: str, kind: str) -> str:
    """The identity of a found value, so two spellings of the same one match ('98765 43210' = '+91-9876543210', 'A@B.co' = 'a@b.co')."""
    if kind == "email":
        return raw.strip().lower()
    if kind == "pan":
        return raw.strip().upper()
    digits = re.sub(r"\D", "", raw)
    return digits[-10:] if kind == "phone" else digits


@dataclass
class Masked:
    text: str
    counts: dict = field(default_factory=dict)          # kind -> how many were replaced

    @property
    def kinds(self) -> list[str]:
        return sorted(self.counts)

    @property
    def found(self) -> bool:
        return bool(self.counts)


def normalise(text: str) -> str:
    """Full-width digits and letters become normal ones, and zero-width characters (used to hide a number from a pattern) are removed."""
    return unicodedata.normalize("NFKC", text or "").translate(_ZERO_WIDTH)


def find(text: str) -> list[tuple[int, int, str]]:
    """[(start, end, kind)] in a normalised text, no overlaps."""
    spans = []
    for m in _EMAIL.finditer(text):
        spans.append((m.start(), m.end(), "email"))
    for m in _PAN.finditer(text):
        spans.append((m.start(), m.end(), "pan"))
    for m in _DIGIT_RUN.finditer(text):
        kind = _classify_digits(re.sub(r"\D", "", m.group()))
        if kind:
            spans.append((m.start(), m.end(), kind))
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    out, end = [], -1
    for start, stop, kind in spans:                       # on an overlap the earlier and longer span wins
        if start >= end:
            out.append((start, stop, kind))
            end = stop
    return out


def values(text: str) -> set[tuple[str, str]]:
    """{(kind, identity)} of every personal value in a text (not masked: for comparing an answer with its sources)."""
    folded = normalise(text)
    return {(kind, value_of(folded[start:stop], kind)) for start, stop, kind in find(folded)}


def mask(text: str) -> Masked:
    """The text with every personal value replaced by a placeholder, and how many of each kind were found."""
    text = normalise(text)
    spans = find(text)
    if not spans:
        return Masked(text)
    parts, last, counts = [], 0, {}
    for start, stop, kind in spans:
        parts.append(text[last:start])
        parts.append(PLACEHOLDER[kind])
        counts[kind] = counts.get(kind, 0) + 1
        last = stop
    parts.append(text[last:])
    return Masked("".join(parts), counts)


# ---------------- the logger ----------------
# Fields that are identifiers made by us, never typed by a person: they are left alone (a 12-digit request id must not be mistaken for a number).
_LOG_KEEP = {"event", "level", "timestamp", "request_id", "user_id", "doc_id", "chunk_id", "scope", "audit_event", "status", "model"}


def mask_log_event(logger, method_name, event_dict):
    """A structlog processor: every text value of every log line passes through the masker (error messages and stack traces included)."""
    for key, value in list(event_dict.items()):
        if key in _LOG_KEEP:
            continue
        if isinstance(value, str):
            event_dict[key] = mask(value).text
        elif isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
            event_dict[key] = [mask(v).text for v in value]
    return event_dict
