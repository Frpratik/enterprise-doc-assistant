"""
Image parser (PNG, JPG, TIFF): the whole file is a picture -> OCR.
Multi-page TIFFs (common for scanned faxes) are read frame by frame.
"""
import io
from pathlib import Path
from typing import Optional

from PIL import Image, ImageSequence

from ingestion.config import settings
from ingestion.models import Document, Element, ElementType, Location
from ingestion.ocr import OcrError, ocr_paragraphs
from ingestion.parsers.base import BaseParser
from ingestion.registry import register
from ingestion.textutils import looks_like_heading


def image_elements(img: Image.Image, location: Location, doc: Document,
                   what: str = "image") -> list[Element]:
    """
    OCR one image -> Elements. Shared by the image, Word, PowerPoint and PDF parsers.
    No text found -> one placeholder, so we still know an image was there.
    """
    if min(img.size) < settings.min_image_px:
        return []                                      # tiny icon / line, ignore
    try:
        paras = ocr_paragraphs(img)
    except OcrError as e:
        doc.quality.warnings.append(f"{location.label()}: {e}")
        paras = []
    out = []
    for text, conf in paras:
        if looks_like_heading(text) and "\n" not in text:
            out.append(Element(type=ElementType.HEADING, text=text, level=2, location=location,
                               ocr_confidence=conf))
        else:
            out.append(Element(type=ElementType.IMAGE_OCR, text=text, location=location, ocr_confidence=conf))
    if not out:
        out.append(Element(type=ElementType.IMAGE_PLACEHOLDER, text=f"[{what.capitalize()}: no text found]",
                           location=location))
    return out


def open_image_bytes(blob: bytes) -> Optional[Image.Image]:
    try:
        img = Image.open(io.BytesIO(blob))
        img.load()
        return img
    except Exception:
        return None                                    # unsupported format (e.g. EMF/WMF in Office files)


@register
class ImageParser(BaseParser):
    name, file_types = "image_parser", ("image",)

    def parse(self, doc: Document, path: Path) -> list[Element]:
        out = []
        with Image.open(path) as img:
            frames = list(ImageSequence.Iterator(img))
            doc.metadata.page_count = len(frames)
            for i, frame in enumerate(frames, start=1):
                out += image_elements(frame.copy(), Location(page=i), doc)
        return out
