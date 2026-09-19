"""
Screenshot verification for /donate.

Uses OCR (pytesseract) + simple icon-color classification to check that a
donation proof screenshot (an in-game "Assistance Report") shows the exact
resource(s) and amount(s) the member typed being sent to the alliance bank
— not some other player, and not a different/mismatched amount.
"""

import os
import io
import re
import asyncio
import colorsys

import numpy as np
import pytesseract
from PIL import Image

# The exact in-game name of the alliance bank governor, e.g. "KD Bank 53".
# Override via env var if the bank ever gets renamed.
BANK_NAME = os.getenv("BANK_NAME", "KD Bank 53")

# How much an amount is allowed to differ (as a fraction) from what the member
# typed and still count as a match. Small tolerance absorbs OCR/tax-rounding
# noise without letting wildly different amounts through.
AMOUNT_TOLERANCE = 0.02  # 2%


def _normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _classify_icon_color(rgb) -> str | None:
    """Classify an average icon pixel color into one of the four resources,
    based on RoK's standard icon colors (Food=green, Wood=brown/orange,
    Stone=grey/blue-grey, Gold=saturated yellow/orange)."""
    r, g, b = [c / 255 for c in rgb]
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    h *= 360

    if s < 0.35 and v > 0.3:
        return "Stone"
    if 35 <= h <= 60 and s >= 0.55:
        return "Gold"
    if 60 < h <= 170:
        return "Food"
    if h < 35 or h > 340:
        return "Wood"
    return None


def _parse_amount_text(text: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", text)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _extract_report_rows(image: Image.Image) -> list[dict]:
    """Parse an Assistance Report screenshot into a list of rows, each with
    the transport target name, the row's y-position, and (if found) a
    resource type + amount for that row."""
    rgb_image = image.convert("RGB")
    w, h = rgb_image.size
    data = pytesseract.image_to_data(rgb_image, output_type=pytesseract.Output.DICT)
    n = len(data["text"])

    # Group words by their line, so we can rebuild each "Transport Target: X"
    # line's full text and position regardless of how tesseract split words.
    lines = {}
    for i in range(n):
        word = data["text"][i].strip()
        if not word:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, {"words": [], "top": data["top"][i], "left": data["left"][i]})
        lines[key]["words"].append(word)
        lines[key]["top"] = min(lines[key]["top"], data["top"][i])

    target_rows = []
    for key, info in lines.items():
        line_text = " ".join(info["words"])
        if "Transport" in line_text and "Target" in line_text:
            # Text after the colon is the target name (may include a prefix
            # icon/rank tag that OCR sometimes mangles, e.g. "WP Zykastro").
            match = re.search(r"Target:?\s*(.+)", line_text)
            target_name = match.group(1).strip() if match else line_text
            target_rows.append({"top": info["top"], "target": target_name})

    target_rows.sort(key=lambda r: r["top"])

    rows = []
    for idx, row in enumerate(target_rows):
        top = row["top"]
        # The icon + amount for this entry sits below the header line, above
        # the next entry's header (or the bottom of the image for the last one).
        next_top = target_rows[idx + 1]["top"] if idx + 1 < len(target_rows) else h
        y0 = min(top + 45, h - 1)
        y1 = min(max(top + 95, y0 + 1), next_top, h)
        if y1 <= y0:
            continue

        crop = rgb_image.crop((0, y0, int(w * 0.45), y1))
        arr = np.array(crop)
        ch, cw, _ = arr.shape
        if ch < 5 or cw < 100:
            rows.append({**row, "resource": None, "amount": None})
            continue

        # Icon sits at a roughly fixed relative position within the row.
        iy0, iy1 = int(ch * 0.2), int(ch * 0.8)
        ix0, ix1 = int(cw * 0.17), int(cw * 0.27)
        icon_box = arr[iy0:iy1, ix0:ix1]
        resource = None
        if icon_box.size:
            median_color = np.median(icon_box.reshape(-1, 3), axis=0)
            resource = _classify_icon_color(median_color)

        # Amount text: white bold digits on the colored card — threshold to
        # isolate them, then OCR with a digit-only whitelist.
        mask = (arr[:, :, 0] > 200) & (arr[:, :, 1] > 200) & (arr[:, :, 2] > 200)
        thresholded = np.where(mask, 0, 255).astype(np.uint8)
        thresh_img = Image.fromarray(thresholded).resize((cw * 3, ch * 3))
        amount_text = pytesseract.image_to_string(
            thresh_img, config="--psm 7 -c tessedit_char_whitelist=0123456789,"
        ).strip()
        amount = _parse_amount_text(amount_text)

        rows.append({**row, "resource": resource, "amount": amount})

    return rows


def _verify_sync(image_bytes: bytes, expected_resources: dict) -> tuple[bool, str]:
    """Check that the Assistance Report screenshot contains, somewhere in its
    history (not just the newest entry), a transport to BANK_NAME for each
    resource/amount in `expected_resources` (e.g. {"Food": 5000000}).

    Each report row only shows one resource type, so for every resource the
    member typed we look for any bank-targeted row of that resource type
    whose amount matches (within tolerance) — it doesn't have to be the most
    recent entry. Returns (ok, reason); `reason` explains a rejection.
    """
    image = Image.open(io.BytesIO(image_bytes))
    rows = _extract_report_rows(image)

    if not rows:
        return False, "Couldn't read any 'Transport Target' entries from the screenshot."

    normalized_bank = _normalize(BANK_NAME)
    bank_rows = [r for r in rows if normalized_bank in _normalize(r["target"])]

    if not bank_rows:
        return False, (
            f"No entry in your screenshot shows a transport to **{BANK_NAME}**. "
            "Please attach a screenshot of an Assistance Report entry where the "
            f"Transport Target is **{BANK_NAME}**."
        )

    readable_bank_rows = [r for r in bank_rows if r["resource"] is not None and r["amount"] is not None]
    if not readable_bank_rows:
        return False, (
            f"Found a transport to **{BANK_NAME}** but couldn't read its resource/amount clearly. "
            "Try a clearer, uncropped screenshot."
        )

    # For each resource the member typed, look for ANY bank row of that
    # resource type with a matching amount, anywhere in the screenshot.
    mismatches = []
    for resource, expected_amount in expected_resources.items():
        if not expected_amount:
            continue
        candidates = [r["amount"] for r in readable_bank_rows if r["resource"] == resource]
        if not candidates:
            mismatches.append(
                f"you entered {expected_amount:,} {resource}, but the screenshot doesn't show any {resource} sent to {BANK_NAME}"
            )
            continue
        match_found = any(
            abs(amount - expected_amount) <= max(1, expected_amount * AMOUNT_TOLERANCE)
            for amount in candidates
        )
        if not match_found:
            shown = ", ".join(f"{a:,}" for a in candidates)
            mismatches.append(
                f"you entered {expected_amount:,} {resource}, but the screenshot shows {shown} {resource} sent to {BANK_NAME}"
            )

    if mismatches:
        return False, "The amounts don't match: " + "; ".join(mismatches) + "."

    return True, ""




async def verify_donation_screenshot(image_bytes: bytes, expected_resources: dict) -> tuple[bool, str]:
    """Async wrapper around _verify_sync."""
    return await asyncio.to_thread(_verify_sync, image_bytes, expected_resources)


async def screenshot_shows_bank_donation(image_bytes: bytes) -> bool:
    """Legacy helper kept for compatibility: True if screenshot text mentions
    the bank name anywhere (no amount matching)."""
    def _check(data: bytes) -> bool:
        image = Image.open(io.BytesIO(data))
        rows = _extract_report_rows(image)
        normalized_bank = _normalize(BANK_NAME)
        return any(normalized_bank in _normalize(r["target"]) for r in rows)

    return await asyncio.to_thread(_check, image_bytes)
