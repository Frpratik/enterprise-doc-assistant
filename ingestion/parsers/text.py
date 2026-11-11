"""Plain text, Markdown and HTML parsers (the simplest ones)."""
import re
from pathlib import Path

from bs4 import BeautifulSoup

from ingestion.models import Document, Element, ElementType, Location
from ingestion.parsers.base import BaseParser
from ingestion.registry import register
from ingestion.textutils import looks_like_heading, read_text_file, rows_to_markdown

PAGE1 = Location(page=1)


@register
class TxtParser(BaseParser):
    """Paragraphs are separated by blank lines. A short CAPITALS line = heading."""
    name, file_types = "txt_parser", ("txt",)

    def parse(self, doc: Document, path: Path) -> list[Element]:
        out = []
        for block in re.split(r"\n\s*\n", read_text_file(path)):
            block = block.strip()
            if not block:
                continue
            if "\n" not in block and looks_like_heading(block):
                out.append(Element(type=ElementType.HEADING, text=block, level=1, location=PAGE1))
            else:
                out.append(Element(type=ElementType.PARAGRAPH, text=block, location=PAGE1))
        return out


@register
class MarkdownParser(BaseParser):
    """# headings, - lists, | tables |, and normal paragraphs."""
    name, file_types = "md_parser", ("md",)

    def parse(self, doc: Document, path: Path) -> list[Element]:
        out, para, table = [], [], []

        def flush():
            if para:
                out.append(Element(type=ElementType.PARAGRAPH, text=" ".join(para), location=PAGE1))
                para.clear()
            if table:
                out.append(Element(type=ElementType.TABLE, text="\n".join(table), location=PAGE1))
                table.clear()

        for raw in read_text_file(path).splitlines():
            line = raw.strip()
            if line.startswith("|"):
                if para:
                    flush()
                table.append(line)
                continue
            if table:
                flush()
            if not line:
                flush()
            elif m := re.match(r"^(#{1,6})\s+(.*)", line):
                flush()
                out.append(Element(type=ElementType.HEADING, text=m.group(2).strip(), level=len(m.group(1)),
                                   location=PAGE1))
            elif m := re.match(r"^([-*+]|\d+[.)])\s+(.*)", line):
                flush()
                out.append(Element(type=ElementType.LIST_ITEM, text=m.group(2).strip(), location=PAGE1))
            else:
                para.append(line)
        flush()
        return out


@register
class HtmlParser(BaseParser):
    """Keep headings, paragraphs, list items and tables. Drop scripts, menus, footers."""
    name, file_types = "html_parser", ("html",)
    JUNK = ["script", "style", "noscript", "nav", "footer", "header", "aside", "form"]

    def parse(self, doc: Document, path: Path) -> list[Element]:
        soup = BeautifulSoup(read_text_file(path), "html.parser")
        if soup.title and soup.title.string:
            doc.metadata.title = soup.title.string.strip()
        for tag in soup(self.JUNK):
            tag.decompose()

        out = []
        for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table"]):
            if tag.name != "table" and tag.find_parent("table"):
                continue                       # text inside a table is part of the table element
            text = " ".join(tag.get_text(" ").split())
            if tag.name == "table":
                rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                        for tr in tag.find_all("tr")]
                rows = [r for r in rows if any(r)]
                if rows:
                    out.append(Element(type=ElementType.TABLE, text=rows_to_markdown(rows), location=PAGE1))
            elif not text:
                continue
            elif tag.name.startswith("h"):
                out.append(Element(type=ElementType.HEADING, text=text, level=int(tag.name[1]), location=PAGE1))
            elif tag.name == "li":
                out.append(Element(type=ElementType.LIST_ITEM, text=text, location=PAGE1))
            else:
                out.append(Element(type=ElementType.PARAGRAPH, text=text, location=PAGE1))
        return out
