"""
OCR helper (Tesseract). Used by the image, Word, PowerPoint and PDF parsers.

Returns text grouped into paragraphs, each with a confidence score (0..1),
so the quality stage can flag bad scans.
"""
from pathlib import Path
from typing import Optional

import pytesseract
from PIL import Image, ImageOps

from ingestion.config import settings

# Use the configured tesseract.exe only if it really exists on this machine,
# otherwise fall back to "tesseract" on PATH (same code works on Windows and Linux).
if settings.tesseract_cmd and Path(settings.tesseract_cmd).exists():
    pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd


class OcrError(Exception):
    pass


def _prepare(img: Image.Image) -> Image.Image:
    """Simple clean-up that helps Tesseract: grayscale, and enlarge small images."""
    img = ImageOps.exif_transpose(img)          # phone photos: respect rotation
    img = img.convert("L")
    if min(img.size) < 1000:
        img = img.resize((img.width * 2, img.height * 2))
    return img


def ocr_paragraphs(img: Image.Image) -> list[tuple[str, float]]:
    """OCR an image -> [(paragraph_text, confidence), ...] in reading order."""
    try:
        data = pytesseract.image_to_data(
            _prepare(img), lang=settings.ocr_lang, timeout=settings.ocr_timeout_sec,
            output_type=pytesseract.Output.DICT,
        )
    except RuntimeError as e:                       # pytesseract raises this on timeout
        raise OcrError(f"OCR timed out after {settings.ocr_timeout_sec}s") from e

    paragraphs: dict[tuple, dict] = {}
    for i, word in enumerate(data["text"]):
        word = word.strip()
        conf = float(data["conf"][i])
        if not word or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i])
        para = paragraphs.setdefault(key, {"lines": {}, "confs": []})
        para["lines"].setdefault(data["line_num"][i], []).append(word)
        para["confs"].append(conf)

    out = []
    for para in paragraphs.values():
        text = "\n".join(" ".join(words) for words in para["lines"].values())
        out.append((text, round(sum(para["confs"]) / len(para["confs"]) / 100, 3)))
    return out


def ocr_text(img: Image.Image) -> tuple[str, Optional[float]]:
    """OCR an image -> (all text, average confidence). Empty text -> ("", None)."""
    paras = ocr_paragraphs(img)
    if not paras:
        return "", None
    text = "\n".join(t for t, _ in paras)
    conf = round(sum(c for _, c in paras) / len(paras), 3)
    return text, conf
