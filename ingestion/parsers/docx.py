"""
Word (.docx) parser.

Walks the document body IN ORDER (paragraphs and tables are mixed in Word),
and turns each into an Element:
  Heading 1/2/3 style -> heading (with level)      List style -> list_item
  normal paragraph    -> paragraph                 table      -> Markdown table
  picture inside a paragraph -> OCR (image_ocr) or a placeholder
Word files have no fixed pages, so location stays at document level.
"""
from pathlib import Path

import docx
from docx.table import Table
from docx.text.paragraph import Paragraph

from ingestion.models import Document, Element, ElementType, Location
from ingestion.parsers.base import BaseParser
from ingestion.parsers.image import image_elements, open_image_bytes
from ingestion.registry import register
from ingestion.textutils import rows_to_markdown

BLIP = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"


@register
class DocxParser(BaseParser):
    name, file_types = "docx_parser", ("docx",)

    def parse(self, doc: Document, path: Path) -> list[Element]:
        d = docx.Document(str(path))
        props = d.core_properties
        doc.metadata.title = props.title or None
        doc.metadata.author = props.author or None
        doc.metadata.created = props.created.isoformat() if props.created else None

        out, loc = [], Location()
        for child in d.element.body.iterchildren():
            tag = child.tag.split("}")[-1]
            if tag == "p":
                para = Paragraph(child, d)
                out += self._paragraph(para, loc)
                out += self._images(child, d, loc, doc)
            elif tag == "tbl":
                rows = self._table_rows(Table(child, d))
                if rows:
                    out.append(Element(type=ElementType.TABLE, text=rows_to_markdown(rows), location=loc))
        return out

    @staticmethod
    def _paragraph(para: Paragraph, loc: Location) -> list[Element]:
        text = para.text.strip()
        if not text:
            return []
        style = (para.style.name if para.style is not None else "") or ""
        if style == "Title":
            return [Element(type=ElementType.HEADING, text=text, level=1, location=loc)]
        if style.startswith("Heading"):
            level = int(style.split()[-1]) if style.split()[-1].isdigit() else 1
            return [Element(type=ElementType.HEADING, text=text, level=level, location=loc)]
        if style.startswith("List") or para._p.pPr is not None and para._p.pPr.numPr is not None:
            return [Element(type=ElementType.LIST_ITEM, text=text, location=loc)]
        return [Element(type=ElementType.PARAGRAPH, text=text, location=loc)]

    @staticmethod
    def _images(p_xml, d, loc: Location, doc: Document) -> list[Element]:
        out = []
        for blip in p_xml.iter(BLIP):
            rel_id = blip.get(EMBED)
            part = d.part.related_parts.get(rel_id)
            img = open_image_bytes(part.blob) if part is not None else None
            if img is not None:
                out += image_elements(img, loc, doc)
        return out

    @staticmethod
    def _table_rows(table: Table) -> list[list[str]]:
        rows = []
        for row in table.rows:
            cells, prev = [], None
            for cell in row.cells:
                # merged cells repeat the same cell object -> keep it once
                if cell._tc is prev:
                    continue
                prev = cell._tc
                cells.append(cell.text.strip())
            if any(cells):
                rows.append(cells)
        return rows
