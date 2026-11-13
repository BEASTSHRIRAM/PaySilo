"""House style for every PaySilo email, as a checker.

The rules: short and professional, no em or en dashes, no hyphen used as punctuation or in compound words
(hyphens inside URLs, email addresses, ids, masks and dates are fine), a subject of at most 60 characters
without ALL CAPS words, exclamation marks or emojis, and a body of at most 90 words (tables of figures,
URLs and the greeting and closing lines are not counted).

Used by the unit test over every template and by the funding draft's LLM polish guard, which discards any
reworded text that breaks the style.
"""

from __future__ import annotations

import re

MAX_SUBJECT = 60
MAX_BODY_WORDS = 90
ALLOWED_CAPS = {"USD", "EUR", "GBP", "PDF", "IBAN", "SWIFT", "SEPA", "ACH", "OTP", "W9", "W8BEN", "AUD", "CAD", "SGD"}
_URL = re.compile(r"https?://\S+")
_EMAIL = re.compile(r"\S+@\S+\.\S+")
_DASHES = ("—", "–", "‒", "―", "−")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️]")
_CAPS_WORD = re.compile(r"\b[A-Z]{2,}\b")


def _strip_links(text: str) -> str:
    return _EMAIL.sub(" ", _URL.sub(" ", text))


def hyphen_problems(text: str) -> list[str]:
    """Hyphens used as punctuation (bullets, spaced dashes) or inside compound words made of letters only."""
    out = []
    for token in _strip_links(text).split():
        if "-" not in token:
            continue
        core = token.strip(".,;:()\"'")
        if core.strip("-") == "" or core.startswith("-") or core.endswith("-"):
            out.append(f"hyphen as punctuation: {token!r}")
        elif not any(ch.isdigit() for ch in core):
            out.append(f"hyphenated word: {token!r}")
    return out


def char_problems(text: str) -> list[str]:
    out = []
    if any(d in text for d in _DASHES):
        out.append("em or en dash")
    if "!" in text:
        out.append("exclamation mark")
    if _EMOJI.search(text):
        out.append("emoji")
    return out


def subject_problems(subject: str) -> list[str]:
    out = char_problems(subject) + hyphen_problems(subject)
    if len(subject) > MAX_SUBJECT:
        out.append(f"subject is {len(subject)} characters (max {MAX_SUBJECT})")
    for word in _CAPS_WORD.findall(subject):
        if word not in ALLOWED_CAPS:
            out.append(f"ALL CAPS word {word!r} in subject")
    return out


def body_words(text: str) -> int:
    """Words in the prose of the body: no greeting, no closing, no footer, no indented figure rows, no URLs."""
    lines = text.split("\n")
    count = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Thank you,"):
            break
        if not stripped or line.startswith("  ") or stripped.startswith(("Hello", "http")):
            continue
        count += len(_URL.sub(" ", stripped).split())
    return count


def text_problems(text: str, *, full: bool = True) -> list[str]:
    out = char_problems(text) + hyphen_problems(text)
    if full:
        if not re.match(r"Hello( [^\n]+)?,\n", text):
            out.append("must start with 'Hello <name>,' or 'Hello,'")
        if "Thank you,\n" not in text:
            out.append("missing the 'Thank you,' closing")
        words = body_words(text)
        if words > MAX_BODY_WORDS:
            out.append(f"body is {words} words (max {MAX_BODY_WORDS})")
    return out
