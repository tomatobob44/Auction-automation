"""Module 1 — Identify.

Classify a manual string (UPC / EAN / ASIN / free-text) or decode a barcode
image LOCALLY (zxing-cpp, no API call — keep it in the local loop, free).
"""
from __future__ import annotations

import re

from .models import Identifier, IdentifierType

_ASIN_STRONG = re.compile(r"^B0[A-Z0-9]{8}$")
_ASIN_LOOSE = re.compile(r"^[A-Z0-9]{10}$")
_DIGITS_ONLY = re.compile(r"^\d+$")


def _upc_check_digit_ok(digits: str) -> bool:
    """Validate a UPC-A (12) or EAN-13 (13) check digit (mod-10).

    For both formats: sum digits with alternating weights from the right
    (excluding the check digit), the check digit makes the total a multiple
    of 10. UPC-A weights odd positions x3; EAN-13 weights even positions x3 —
    handled by anchoring weights to the right.
    """
    body = digits[:-1]
    check = int(digits[-1])
    total = 0
    # Walk right-to-left over the body; rightmost body digit gets weight 3.
    for i, ch in enumerate(reversed(body)):
        weight = 3 if i % 2 == 0 else 1
        total += int(ch) * weight
    calc = (10 - (total % 10)) % 10
    return calc == check


def classify(text: str) -> Identifier:
    """Classify a raw input string into a typed identifier.

    - 12 digits with valid check digit -> UPC
    - 13 digits with valid check digit -> EAN
    - digits with a bad check digit    -> TEXT (confidence 0.5), kept as title
    - B0XXXXXXXX (or 10-char alnum)    -> ASIN
    - anything else                    -> TEXT (confidence 0.6)
    """
    raw = text.strip()
    compact = raw.replace("-", "").replace(" ", "")

    if _DIGITS_ONLY.match(compact):
        if len(compact) == 12:
            if _upc_check_digit_ok(compact):
                return Identifier(IdentifierType.UPC, compact, confidence=1.0)
            return Identifier(IdentifierType.TEXT, compact, raw_title=raw,
                              confidence=0.5)
        if len(compact) == 13:
            if _upc_check_digit_ok(compact):
                return Identifier(IdentifierType.EAN, compact, confidence=1.0)
            return Identifier(IdentifierType.TEXT, compact, raw_title=raw,
                              confidence=0.5)
        # UPC-E (8) and other digit strings fall through to text.
        return Identifier(IdentifierType.TEXT, compact, raw_title=raw,
                          confidence=0.6)

    upper = compact.upper()
    if _ASIN_STRONG.match(upper):
        return Identifier(IdentifierType.ASIN, upper, confidence=1.0)
    if _ASIN_LOOSE.match(upper):
        # Older ASINs / ISBN-10s aren't all B0...; accept with reduced confidence.
        return Identifier(IdentifierType.ASIN, upper, confidence=0.8)

    return Identifier(IdentifierType.TEXT, raw, raw_title=raw, confidence=0.6)


def decode_barcode(image_bytes: bytes) -> Identifier | None:
    """Decode a UPC/EAN barcode from image bytes, locally. Returns the
    classified identifier, or None if nothing decodes. No network."""
    import io

    import zxingcpp
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except Exception:
        return None

    results = zxingcpp.read_barcodes(img)
    if not results:
        return None

    # Prefer product barcode symbologies (UPC-A / EAN-13 / EAN-8 / UPC-E).
    product_formats = {"UPCA", "EAN13", "EAN8", "UPCE"}
    chosen = None
    for r in results:
        fmt = str(r.format).split(".")[-1].replace("_", "").upper()
        if fmt in product_formats:
            chosen = r
            break
    if chosen is None:
        chosen = results[0]

    decoded = (chosen.text or "").strip()
    if not decoded:
        return None

    # zxing-cpp reports UPC-A as EAN-13 with a leading zero (they share the
    # symbology). A 13-digit code in the "0" number system IS the 12-digit
    # UPC-A printed on the box — strip the leading zero so it classifies as
    # UPC and matches what comp sources expect.
    if decoded.isdigit() and len(decoded) == 13 and decoded.startswith("0"):
        decoded = decoded[1:]

    return classify(decoded)
