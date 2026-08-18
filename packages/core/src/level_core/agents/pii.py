"""PII stripping for prompts sent to Gemini."""

from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\-\s().]{7,}\d")
_STREET_RE = re.compile(
    r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s?){1,4}(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Ln|Lane|Dr|Drive|Ct|Court|Way)\b",
    re.IGNORECASE,
)


def strip_pii(text: str) -> str:
    """Replace emails, phone numbers, and street addresses with tokens.

    Names ARE intentionally preserved (agent needs them to link to care_people
    aliases). Sensitive contact info is not.
    """
    text = _EMAIL_RE.sub("<email>", text)
    text = _PHONE_RE.sub("<phone>", text)
    text = _STREET_RE.sub("<address>", text)
    return text
