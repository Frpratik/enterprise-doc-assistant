"""
PowerPoint (.pptx) parser. Slide by slide:
  slide title        -> heading (level 1)
  text boxes         -> paragraph (one line) or list_item (bullet lines)
  tables             -> Markdown table
  pictures           -> OCR (image_ocr) or placeholder
  grouped shapes     -> opened up and read the same way
  speaker notes      -> notes
Shapes are read top-to-bottom, left-to-right (the order a person reads a slide).
"""
from pathlib import Path

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from ingestion.models import Document, Element, ElementType, Location
from ingestion.parsers.base import BaseParser
from ingestion.parsers.image import image_elements, open_image_bytes
from ingestion.registry import register
from ingestion.textutils import rows_to_markdown


@register
class PptxParser(BaseParser):
    name, file_types = "pptx_parser", ("pptx",)

    def parse(self, doc: Document, path: Path) -> list[Element]:
        prs = Presentation(str(path))
        doc.metadata.page_count = len(prs.slides)
        doc.metadata.title = prs.core_properties.title or None
        doc.metadata.author = prs.core_properties.author or None

        out = []
        for n, slide in enumerate(prs.slides, start=1):
            loc = Location(slide=n)
            title_shape = slide.shapes.title
            if title_shape is not None and title_shape.text.strip():
                out.append(Element(type=ElementType.HEADING, text=title_shape.text.strip(), level=1, location=loc))
                if doc.metadata.title is None:
                    doc.metadata.title = title_shape.text.strip()      # first slide title = deck title
            title_id = title_shape.shape_id if title_shape is not None else None
            out += self._shapes(slide.shapes, loc, doc, skip_id=title_id)

            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    out.append(Element(type=ElementType.NOTES, text=notes, location=loc))
        return out

    def _shapes(self, shapes, loc: Location, doc: Document, skip_id=None) -> list[Element]:
        out = []
        ordered = sorted(shapes, key=lambda s: ((s.top or 0), (s.left or 0)))   # reading order
        for shape in ordered:
            if shape.shape_id == skip_id:
                continue
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                out += self._shapes(shape.shapes, loc, doc)
            elif getattr(shape, "has_table", False):
                rows = [[c.text.strip() for c in row.cells] for row in shape.table.rows]
                rows = [r for r in rows if any(r)]
                if rows:
                    out.append(Element(type=ElementType.TABLE, text=rows_to_markdown(rows), location=loc))
            elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                img = open_image_bytes(shape.image.blob)
                if img is not None:
                    out += image_elements(img, loc, doc)
            elif shape.has_text_frame:
                lines = [p.text.strip() for p in shape.text_frame.paragraphs if p.text.strip()]
                if len(lines) == 1:
                    out.append(Element(type=ElementType.PARAGRAPH, text=lines[0], location=loc))
                else:
                    out += [Element(type=ElementType.LIST_ITEM, text=t, location=loc) for t in lines]
        return out
