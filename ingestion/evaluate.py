"""
Ingestion evaluation = compare the pipeline's output with the golden set (the correct answers).

Metrics per document (0..1, higher is better):
  status_ok        did the file get the expected status (done / needs_review / failed / duplicate)?
  word_recall      how much of the correct text we got, in order       (missed or misread words lower it)
  word_precision   how much of OUR text is correct                       (junk like headers/footers lowers it)
  heading_recall   expected headings found as headings
  table_accuracy   expected table rows found exactly (every cell right)
  junk_removed     none of the 'must_not_contain' text is left

Run:   python -m ingestion.evaluate                  (prints the score table, compares with the baseline)
       python -m ingestion.evaluate --save-baseline  (stores today's numbers as the new baseline)
Runs in a temporary folder, so your real data/ folders are never touched.
"""
import argparse
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from ingestion.config import PROJECT_ROOT, settings
from ingestion.manifest import Manifest
from ingestion.models import Document, ElementType
from ingestion.pipeline import process_file

GOLDEN = PROJECT_ROOT / "evals" / "ingestion_golden.json"
SAMPLES = PROJECT_ROOT / "data" / "samples"
BASELINE = PROJECT_ROOT / "evals" / "ingestion_baseline.json"
LATEST = PROJECT_ROOT / "evals" / "ingestion_latest.json"
METRICS = ["status_ok", "word_recall", "word_precision", "heading_recall", "table_accuracy", "junk_removed"]


# ---------- helpers ----------
def words(text: str) -> list[str]:
    """Lowercase words and numbers only: 'Rs 5,00,000.' -> ['rs', '5', '00', '000']"""
    return re.findall(r"[a-z0-9]+", text.lower())


def norm(text: str) -> str:
    return " ".join(words(text))


def matched_words(reference: list[str], got: list[str]) -> int:
    """How many words match IN ORDER (like comparing two versions of a document)."""
    sm = SequenceMatcher(None, reference, got, autojunk=False)
    return sum(block.size for block in sm.get_matching_blocks())


def table_rows(doc: Document) -> set[tuple[str, ...]]:
    rows = set()
    for e in doc.elements:
        if e.type != ElementType.TABLE:
            continue
        for line in e.text.splitlines():
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
            if cells and not all(set(c) <= {"-", ""} for c in cells):     # skip the |---|---| line
                rows.add(tuple(norm(c.replace("\\|", "|")) for c in cells))
    return rows


# ---------- scoring ----------
def score(doc: Document, expected: dict) -> dict:
    s = {"status_ok": 1.0 if doc.status.value == expected["status"] else 0.0}
    # placeholders like "[Image: no text found]" are not extracted text
    got_text = "\n".join(e.text for e in doc.elements if e.type != ElementType.IMAGE_PLACEHOLDER)

    if "reference" in expected:
        ref, got = words(expected["reference"]), words(got_text)
        m = matched_words(ref, got)
        s["word_recall"] = round(m / len(ref), 3) if ref else 1.0
        s["word_precision"] = round(m / len(got), 3) if got else 0.0

    if "headings" in expected:
        found = {norm(e.text) for e in doc.elements if e.type == ElementType.HEADING}
        hits = sum(norm(h) in found for h in expected["headings"])
        s["heading_recall"] = round(hits / len(expected["headings"]), 3)

    if "table_rows" in expected:
        got_rows = table_rows(doc)
        hits = sum(tuple(norm(c) for c in row) in got_rows for row in expected["table_rows"])
        s["table_accuracy"] = round(hits / len(expected["table_rows"]), 3)

    if "must_not_contain" in expected:
        left = [j for j in expected["must_not_contain"] if j.lower() in got_text.lower()]
        s["junk_removed"] = 0.0 if left else 1.0
    return s


def run_eval() -> dict:
    """Process every golden file in a fresh temporary workspace and score it."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    if not (SAMPLES / "good").exists():
        raise SystemExit("Sample files missing. Run: python scripts/make_sample_data.py")

    tmp = Path(tempfile.mkdtemp(prefix="ingest_eval_"))
    for name in ("inbox_dir", "originals_dir", "output_dir", "failed_dir"):
        setattr(settings, name, tmp / name.replace("_dir", ""))
    settings.manifest_path, settings.log_path = tmp / "manifest.json", tmp / "logs" / "ingestion.jsonl"
    settings.ensure_dirs()
    manifest = Manifest(settings.manifest_path)

    rows = []
    for exp in golden["documents"]:
        doc = process_file(SAMPLES / exp["file"], manifest)
        rows.append({"file": exp["file"], "expected": exp["status"], "got": doc.status.value,
                     "time_ms": doc.duration_ms, **score(doc, exp)})

    # status is checked on every file; text/heading/table accuracy only on files that are
    # SUPPOSED to be read correctly (the deliberately bad scans would hide real changes)
    overall = {}
    for m in METRICS:
        pool = rows if m == "status_ok" else [r for r in rows if r["expected"] == "done"]
        vals = [r[m] for r in pool if m in r]
        if vals:
            overall[m] = round(sum(vals) / len(vals), 3)
    return {"run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "golden_version": golden["version"], "parser_version": settings.parser_version,
            "overall": overall, "documents": rows}


# ---------- printing ----------
def print_report(result: dict, baseline: dict | None) -> None:
    short = {"status_ok": "status", "word_recall": "recall", "word_precision": "precis",
             "heading_recall": "heads", "table_accuracy": "tables", "junk_removed": "junk"}
    head = f"{'FILE':34}{'EXPECTED':>19}{'GOT':>19}" + "".join(f"{short[m]:>8}" for m in METRICS)
    print(head)
    print("-" * len(head))
    for r in result["documents"]:
        cells = "".join(f"{r[m]:>8.2f}" if m in r else f"{'-':>8}" for m in METRICS)
        flag = "" if r["expected"] == r["got"] else "   <-- WRONG STATUS"
        print(f"{r['file'][:34]:34}{r['expected']:>19}{r['got']:>19}{cells}{flag}")
    print("-" * len(head))
    base = (baseline or {}).get("overall", {})
    print("OVERALL  (status: all files | text, headings, tables, junk: files expected to be 'done')")
    for m, v in result["overall"].items():
        delta = ""
        if m in base:
            d = v - base[m]
            delta = "  (same as baseline)" if abs(d) < 0.0005 else f"  ({'+' if d > 0 else ''}{d:.3f} vs baseline)"
        print(f"  {m:16} {v:.3f}{delta}")
    weak = sorted((r for r in result["documents"] if "word_recall" in r and r["word_recall"] < 0.95),
                  key=lambda r: r["word_recall"])
    if weak:
        print("WEAKEST TEXT: " + ", ".join(f"{Path(r['file']).name} ({r['word_recall']:.2f})" for r in weak))


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m ingestion.evaluate")
    ap.add_argument("--save-baseline", action="store_true", help="store these numbers as the baseline")
    args = ap.parse_args(argv)

    result = run_eval()
    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else None
    print_report(result, baseline)
    LATEST.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if args.save_baseline:
        BASELINE.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\nBaseline saved: {BASELINE.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
