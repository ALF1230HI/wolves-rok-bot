"""
Screenshot verification for /donate.

Uses OCR (pytesseract) to check that a donation proof screenshot actually
shows resources being sent to the alliance bank (not some other player).
"""

import os
import io
import re
import asyncio

import pytesseract
from PIL import Image

# The exact in-game name of the alliance bank governor, e.g. "KD Bank 53".
# Override via env var if the bank ever gets renamed.
BANK_NAME = os.getenv("BANK_NAME", "KD Bank 53")


def _normalize(text: str) -> str:
    """Lowercase and collapse whitespace/punctuation noise so minor OCR
    misreads (extra spaces, stray punctuation) don't cause false negatives."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _contains_bank_name_sync(image_bytes: bytes) -> tuple[bool, str]:
    """Run OCR on the image and check whether BANK_NAME appears in the text.

    Returns (found, raw_ocr_text) so callers can log/debug if needed.
    """
    image = Image.open(io.BytesIO(image_bytes))
    raw_text = pytesseract.image_to_string(image)

    normalized_text = _normalize(raw_text)
    normalized_target = _normalize(BANK_NAME)

    return normalized_target in normalized_text, raw_text


async def screenshot_shows_bank_donation(image_bytes: bytes) -> bool:
    """Async wrapper: True if the screenshot's OCR text contains BANK_NAME."""
    found, _raw_text = await asyncio.to_thread(_contains_bank_name_sync, image_bytes)
    return found
