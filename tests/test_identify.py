"""Identifier classification matrix + a barcode write->decode roundtrip.

The roundtrip generates its own barcode image with zxing-cpp, so there are no
binary fixtures committed to the repo.
"""
from __future__ import annotations

import pytest

from binval.identify import classify, decode_barcode
from binval.models import IdentifierType


def test_classify_valid_upc():
    # 036000291452 is a textbook valid UPC-A
    ident = classify("036000291452")
    assert ident.type == IdentifierType.UPC
    assert ident.value == "036000291452"
    assert ident.confidence == 1.0


def test_classify_upc_strips_dashes_and_spaces():
    ident = classify("0-36000-29145-2")
    assert ident.type == IdentifierType.UPC
    assert ident.value == "036000291452"


def test_classify_bad_upc_check_digit_is_text():
    ident = classify("036000291453")  # wrong check digit
    assert ident.type == IdentifierType.TEXT
    assert ident.confidence == 0.5


def test_classify_valid_ean13():
    # 4006381333931 is a valid EAN-13
    ident = classify("4006381333931")
    assert ident.type == IdentifierType.EAN
    assert ident.confidence == 1.0


def test_classify_strong_asin():
    ident = classify("B07FZ8S74R")
    assert ident.type == IdentifierType.ASIN
    assert ident.confidence == 1.0


def test_classify_loose_asin_reduced_confidence():
    # 10-char alnum not starting with B0
    ident = classify("0306406152")  # ISBN-10 style
    # all digits + length 10 -> NOT a UPC/EAN length, falls to text? No:
    # digit-only of length 10 is not 12/13 -> TEXT
    assert ident.type == IdentifierType.TEXT


def test_classify_loose_asin_alnum():
    ident = classify("X12AB34CD9")
    assert ident.type == IdentifierType.ASIN
    assert ident.confidence == 0.8


def test_classify_free_text():
    ident = classify("Sony WH-1000XM4 Headphones")
    assert ident.type == IdentifierType.TEXT
    assert ident.raw_title == "Sony WH-1000XM4 Headphones"
    assert ident.confidence == 0.6


def test_barcode_roundtrip_upc():
    zxingcpp = pytest.importorskip("zxingcpp")
    from PIL import Image
    import io

    # zxing-cpp can write barcodes; render a valid UPC-A and decode it back.
    try:
        if hasattr(zxingcpp, "create_barcode"):
            bc = zxingcpp.create_barcode("036000291452", zxingcpp.BarcodeFormat.UPCA)
            img = zxingcpp.write_barcode_to_image(bc)
        else:
            img = zxingcpp.write_barcode(zxingcpp.BarcodeFormat.UPCA, "036000291452")
    except Exception:
        pytest.skip("zxing-cpp build without barcode writer")

    # writer returns a numpy-like array / PIL image depending on version.
    if not isinstance(img, Image.Image):
        img = Image.fromarray(img)
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    # UPC-A decodes via the EAN-13 symbology; decode_barcode normalizes the
    # leading-zero EAN-13 back to the printed 12-digit UPC.
    ident = decode_barcode(buf.getvalue())
    assert ident is not None
    assert ident.type == IdentifierType.UPC
    assert ident.value == "036000291452"


def test_decode_barcode_garbage_returns_none():
    assert decode_barcode(b"not an image") is None
