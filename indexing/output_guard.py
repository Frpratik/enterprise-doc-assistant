"""
Output guardrails (Phase 6, Stage 4): what the model wrote is checked BEFORE a person sees it. The model is not trusted, even when it is
not under attack: a poisoned excerpt, a clever question or plain model error can make it write something we must not show.

BLOCKED (the person gets the normal "not found" message; the reason goes to the log, never to the person):
  hidden_characters     Unicode tag characters or text-direction overrides in the answer (a way to smuggle data out invisibly)
  system_prompt_leak    8 or more words in a row copied from our system prompt
  instruction_echo      the answer itself scores as prompt injection (it repeats a command, a forged delimiter, ...)
  too_long              more than settings.output_max_chars characters (a one-to-three-sentence answer never gets near this)

SANITISED (the answer is shown with the offending part removed, and the person is told what kinds were removed):
  image / html          markdown images and risky HTML tags (img, script, iframe, form, ...): a rendered image link sends the
                        address, and anything written in it, to a stranger. Removed.
  link                  a web address (markdown link or bare) that does NOT appear in the excerpts the model was given. A link the
                        documents themselves contain (the company portal) stays; a link the model made up or was told to write goes.
  pii:aadhaar/pan/card  an Aadhaar, PAN or card number is never shown in an answer, even if a document contains it.
  pii:email/phone       a contact detail is shown ONLY if the excerpts contain that same value (the HR helpdesk address from the
                        documents is fine; a number the model invented or recalled from somewhere else is not).

What this does not do: it cannot know whether a fact is TRUE (that is the citation check and the eval), it does not look inside the
person's own question (Stage 2), and it cannot stop a model from writing a harmful sentence in plain words.
"""
import re
from dataclasses import dataclass, field

from indexing import injection, pii
from ingestion.config import settings

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)

_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\(\s*([^)\s]*)[^)]*\)")
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"')\]]+", re.IGNORECASE)
_BAD_SCHEME = re.compile(r"\b(?:javascript|data|vbscript|file):[^\s)]*", re.IGNORECASE)
_HTML = re.compile(r"</?\s*(?:a|img|script|iframe|style|svg|link|meta|form|input|object|embed|video|audio|source|body|html|base|button)\b[^>]*>", re.IGNORECASE)
_TRAILING = ".,;:!?)]}'\""
SENTENCE_END = re.compile(r"[.!?]\s|\n")


@dataclass
class Guarded:
    text: str
    blocked: bool = False
    reason: str = ""
    removed: list = field(default_factory=list)       # what was taken out, by kind (never the values)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _shingles(words: list[str], n: int) -> set[tuple]:
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


_PROMPT_SHINGLES: dict = {}


def _prompt_shingles() -> set[tuple]:
    from indexing.answer import SYSTEM_PROMPT                 # imported late: answer.py imports this module
    n = settings.output_leak_shingle_words
    if n not in _PROMPT_SHINGLES:
        _PROMPT_SHINGLES[n] = _shingles(_words(SYSTEM_PROMPT), n)
    return _PROMPT_SHINGLES[n]


def leaks_the_prompt(text: str) -> bool:
    return bool(_shingles(_words(text), settings.output_leak_shingle_words) & _prompt_shingles())


def _strip_url(url: str) -> str:
    return url.rstrip(_TRAILING).lower()


def _allowed_urls(excerpts: list[str]) -> set[str]:
    return {_strip_url(m.group(0)) for e in excerpts for m in _URL.finditer(e)}


def check(text: str) -> tuple[bool, str]:
    """The blocking checks. (blocked, reason). Applied to the whole answer so far (also by the stream)."""
    if injection.HIDDEN.search(text):
        return True, "hidden_characters"
    if leaks_the_prompt(text):
        return True, "system_prompt_leak"
    if injection.scan(text).hostile:
        return True, "instruction_echo"
    if len(text) > settings.output_max_chars:
        return True, "too_long"
    return False, ""


def sanitise(text: str, excerpts: list[str]) -> tuple[str, list[str]]:
    """Remove what must not be shown. Returns (text, kinds removed). Text with nothing to remove comes back as the same string."""
    removed: list[str] = []
    out = text.translate(_ZERO_WIDTH)

    def note(kind):
        if kind not in removed:
            removed.append(kind)

    if _MD_IMAGE.search(out):
        out = _MD_IMAGE.sub("", out)
        note("image")
    if _HTML.search(out):
        out = _HTML.sub("", out)
        note("html")
    allowed = _allowed_urls(excerpts)

    def md_link(m):
        label, url = m.group(1), m.group(2)
        if _strip_url(url) in allowed:
            return m.group(0)
        note("link")
        return label

    out = _MD_LINK.sub(md_link, out)

    def bare(m):
        if _strip_url(m.group(0)) in allowed:
            return m.group(0)
        note("link")
        return "[link removed]"

    out = _URL.sub(bare, out)
    if _BAD_SCHEME.search(out):
        out = _BAD_SCHEME.sub("[link removed]", out)
        note("link")

    # personal data: judged on the folded text (full-width digits must not hide a number), applied to the text as written when nothing is found
    folded = pii.normalise(out)
    spans = pii.find(folded)
    if spans:
        known = {v for e in excerpts for v in pii.values(e)}
        parts, last, changed = [], 0, False
        for start, stop, kind in spans:
            value = pii.value_of(folded[start:stop], kind)
            if kind in ("email", "phone") and (kind, value) in known:
                continue                                              # a contact detail the documents themselves contain
            parts.append(folded[last:start])
            parts.append(pii.PLACEHOLDER[kind])
            last, changed = stop, True
            note(f"pii:{kind}")
        if changed:
            parts.append(folded[last:])
            out = "".join(parts)
    return out, removed


def guard(text: str, excerpts: list[str]) -> Guarded:
    """The whole output check of a finished answer."""
    blocked, reason = check(text)
    if blocked:
        return Guarded(text="", blocked=True, reason=reason)
    out, removed = sanitise(text, excerpts)
    return Guarded(text=out.strip() if removed else text, removed=removed)


class StreamGate:
    """
    The same checks for text that is being streamed. A sentence is shown only after the whole answer so far has passed the blocking checks and
    the sentence itself has been sanitised: a link, a number or a leaked rule is never shown half-written. (The price: the person sees the
    answer a sentence at a time, not a word at a time.) If a check fails, nothing more is shown and `blocked` says why; the caller tells the
    client to clear what it already showed.
    """
    FORCE_AFTER = 400                                   # a sentence this long is released at the next space anyway

    def __init__(self, excerpts: list[str]):
        self.excerpts, self.held, self.released, self.blocked, self.removed = excerpts, "", "", "", []

    def reset(self):
        self.held, self.released, self.blocked = "", "", ""

    def _release(self, chunk: str) -> str:
        if not chunk:
            return ""
        blocked, reason = check(self.released + chunk)
        if blocked:
            self.blocked, self.held = reason, ""
            return ""
        clean, removed = sanitise(chunk, self.excerpts)
        for kind in removed:
            if kind not in self.removed:
                self.removed.append(kind)
        self.released += chunk
        return clean

    def feed(self, text: str) -> str:
        if self.blocked or not text:
            return ""
        self.held += text
        cut = 0
        for m in SENTENCE_END.finditer(self.held):
            cut = m.end()
        if not cut and len(self.held) > self.FORCE_AFTER:
            cut = self.held.rfind(" ") + 1
        chunk, self.held = self.held[:cut], self.held[cut:]
        return self._release(chunk)

    def flush(self) -> str:
        if self.blocked:
            return ""
        chunk, self.held = self.held, ""
        return self._release(chunk)
