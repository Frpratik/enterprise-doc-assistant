"""
Setup check: is everything installed?

Run:  python scripts/check_setup.py
"""
import importlib
import sys
from pathlib import Path

# allow "import ingestion" when running this script directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# (import name, pip name)
PACKAGES = [
    ("pymupdf", "pymupdf"),
    ("pdfplumber", "pdfplumber"),
    ("docx", "python-docx"),
    ("pptx", "python-pptx"),
    ("pandas", "pandas"),
    ("openpyxl", "openpyxl"),
    ("bs4", "beautifulsoup4"),
    ("PIL", "Pillow"),
    ("pytesseract", "pytesseract"),
    ("filetype", "filetype"),
    ("watchdog", "watchdog"),
    ("langdetect", "langdetect"),
    ("pydantic", "pydantic"),
    ("pydantic_settings", "pydantic-settings"),
    ("structlog", "structlog"),
    ("psycopg", "psycopg[binary]"),
    ("psycopg_pool", "psycopg-pool"),
    ("pgvector", "pgvector"),
    ("pytest", "pytest"),
    ("reportlab", "reportlab"),
]

ok = True

# 1. Python version
py = sys.version_info
if py >= (3, 10):
    print(f"[OK]      Python {py.major}.{py.minor}.{py.micro}")
else:
    ok = False
    print(f"[MISSING] Python 3.10+ needed, found {py.major}.{py.minor}")

# 2. Python packages
for import_name, pip_name in PACKAGES:
    try:
        importlib.import_module(import_name)
        print(f"[OK]      {pip_name}")
    except ImportError:
        ok = False
        print(f"[MISSING] {pip_name}  ->  pip install {pip_name}")

# 3. Tesseract OCR program (separate install, not a pip package)
try:
    import pytesseract
    from ingestion.config import settings

    if settings.tesseract_cmd and Path(settings.tesseract_cmd).exists():
        pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd
    version = pytesseract.get_tesseract_version()
    print(f"[OK]      Tesseract OCR {version}")
except Exception as e:  # not installed / not on PATH
    ok = False
    print("[MISSING] Tesseract OCR program")
    print("          Windows: install from https://github.com/UB-Mannheim/tesseract/wiki")
    print("          then set INGEST_TESSERACT_CMD in .env if it's not on PATH")
    print(f"          ({type(e).__name__}: {e})")

# 4. Database (Phase 2): Postgres + pgvector from docker compose
try:
    import psycopg
    from ingestion.config import settings as _s

    with psycopg.connect(_s.database_url, connect_timeout=3) as conn:
        pg = conn.execute("SHOW server_version").fetchone()[0]
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        vec = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()[0]
    print(f"[OK]      Postgres {pg} with pgvector {vec}")
    from indexing.migrate import _files, applied
    pending = [f.name for f in _files() if f.name not in applied()]
    if pending:
        print(f"[TODO]    {len(pending)} database migration(s) pending  ->  python -m indexing.migrate")
    else:
        print("[OK]      Database schema up to date")
except ImportError:
    pass                                   # packages already reported missing above
except Exception as e:
    ok = False
    print("[MISSING] Database not reachable")
    print("          Start it with:  docker compose up -d   (Docker Desktop must be running)")
    print(f"          ({type(e).__name__}: {str(e).strip().splitlines()[0][:120]})")

# 5. Data folders
from ingestion.config import settings  # noqa: E402

settings.ensure_dirs()
print(f"[OK]      Data folders ready under {settings.data_dir}")

print("\nAll good!" if ok else "\nFix the [MISSING] items above, then run this again.")
sys.exit(0 if ok else 1)
