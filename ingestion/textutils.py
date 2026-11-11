"""Small shared helpers used by several parsers."""
from pathlib import Path


def read_text_file(path: Path) -> str:
    """Read a text file whatever its encoding (UTF-8 first, then common Windows encodings)."""
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")   # Windows / old Mac line endings -> \n


def rows_to_markdown(rows: list[list]) -> str:
    """[[header...], [row...], ...] -> a Markdown table. First row is the header."""
    if not rows:
        return ""

    def cell(v) -> str:
        text = "" if v is None else str(v)
        return text.replace("\n", " ").replace("|", "\\|").strip()

    width = max(len(r) for r in rows)
    rows = [[cell(v) for v in r] + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def looks_like_heading(line: str) -> bool:
    """For plain text / OCR: a short line in CAPITALS is probably a heading (e.g. 'CODE OF CONDUCT')."""
    line = line.strip()
    letters = [c for c in line if c.isalpha()]
    return 0 < len(line.split()) <= 8 and len(letters) >= 3 and line.upper() == line and not line.endswith(".")
