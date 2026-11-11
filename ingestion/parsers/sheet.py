"""
Excel (.xlsx) and CSV parser.

Each sheet -> a heading (sheet name) + Markdown table(s).
Big sheets are split into tables of N rows, each repeating the header row,
so every piece still makes sense on its own (important for chunking later).
"""
from pathlib import Path

import pandas as pd

from ingestion.config import settings
from ingestion.models import Document, Element, ElementType, Location
from ingestion.parsers.base import BaseParser
from ingestion.registry import register
from ingestion.textutils import rows_to_markdown


def _clean(v):
    """Show 4.0 as 4, empty cells as blank."""
    if pd.isna(v):
        return ""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


@register
class SheetParser(BaseParser):
    name, file_types = "sheet_parser", ("xlsx", "csv")

    def parse(self, doc: Document, path: Path) -> list[Element]:
        if path.suffix.lower() == ".csv":
            sheets = {path.stem: self._read_csv(path)}
        else:
            sheets = pd.read_excel(path, sheet_name=None, dtype=object)   # all sheets
        doc.metadata.page_count = len(sheets)

        out = []
        n = settings.table_rows_per_element
        for sheet_name, df in sheets.items():
            df = df.dropna(how="all").dropna(axis=1, how="all")        # drop fully empty rows/cols
            loc = Location(sheet=str(sheet_name))
            out.append(Element(type=ElementType.HEADING, text=str(sheet_name), level=2, location=loc))
            if df.empty:
                continue
            header = [str(c) for c in df.columns]
            rows = [[_clean(v) for v in r] for r in df.itertuples(index=False)]
            for start in range(0, len(rows), n):
                out.append(Element(type=ElementType.TABLE, text=rows_to_markdown([header] + rows[start:start + n]),
                                   location=loc))
        return out

    @staticmethod
    def _read_csv(path: Path) -> pd.DataFrame:
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                return pd.read_csv(path, dtype=object, encoding=enc, sep=None, engine="python")  # sep=None: guess , ; tab
            except UnicodeDecodeError:
                continue
        raise ValueError("could not read CSV with any known encoding")
