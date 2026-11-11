# Build Log — Enterprise Document Intelligence Assistant

A simple, running record of **what we built, why, and how** — stage by stage.
Updated at the end of every stage.

---

## The big picture

We are building a **company assistant** that answers employee questions from internal
documents (with citations), respects who can see what, and can take simple actions.

It is built in **phases**. Each phase is split into small **stages**, and every stage is
tested before moving on.

| Phase | What it does | Status |
|---|---|---|
| **Phase 1 — Ingestion** | Any file comes in → checked → read correctly → one standard JSON | 🔨 In progress |
| Phase 2 — Store | Chunk, embed, save to a vector database, keep it in sync | ⏳ |
| Phase 3 — Answer | Search + rerank + LLM answer with citations, streamed API | ⏳ |
| Phase 4 — Measure | Evals: golden questions, retrieval + answer scores | ⏳ |
| Phase 5 — Access | Login, roles, who-can-see-what | ⏳ |
| Phase 6 — Safety | Guardrails, PII masking, rate limits | ⏳ |
| Phase 7 — Actions | Agent + MCP tools (apply leave, raise ticket) with approval | ⏳ |
| Phase 8 — Observe | Tracing, prompt versions, feedback | ⏳ |
| Phase 9 — Production | Retries, caching, CI eval gate | ⏳ |
| Phase 10 — Ship | Docker, AWS, UI, demo | ⏳ |

---

## Phase 1 — Document Ingestion

**Goal:** any file dropped in → checked for safety → read correctly (text, tables,
scanned pages, images) → turned into **one standard JSON** → quality-checked → saved with a status.

**Why it matters:** bad extraction = bad answers forever. If a table is read wrong here,
no prompt or model can fix it later.

**Analogy:** a post office sorting room — a gate guard, a sorting desk, specialist counters,
one standard form for every parcel, an inspector, and a register book.

### The pipeline (end to end)

```
File in → GATE (safe? real type? duplicate? new version?)
        → DETECT (per page: digital / scanned / mixed)
        → ROUTE (pick the right parser)
        → EXTRACT (text, headings, tables, OCR, metadata, page numbers)
        → ONE STANDARD FORMAT (Document + Elements)
        → CLEAN → QUALITY CHECK → SAVE + STATUS → PRINT
```

### Design rules we follow

1. The original file is **never changed** — we keep an untouched copy.
2. **One standard output format** for every file type.
3. **Plug-in parsers** — a new file type = one new parser, nothing else changes.
4. Decide **per page**, not per file (one PDF can be digital + scanned + mixed).
5. **One bad file never stops the pipeline.**
6. **Idempotent** — the same file twice gives the same result (hash).
7. Every file has a **status and a reason**. Nothing fails silently.
8. **Measure quality** — never "looks fine to me".
9. Settings live in **config**, not hard-coded.

---

### ✅ Phase 0 — Setup (foundation)

**What:** the project skeleton, all settings in one place, a setup checker, and fake test files.

**Why:** every later stage needs config and test data. Fake data means we never touch real
company files.

**What we built**
- `requirements.txt` — every package, with a comment saying why it's needed.
- `ingestion/config.py` — all settings (folders, size limit, page limit, allowed types,
  OCR settings, quality thresholds). Any value can be changed in `.env`.
- `scripts/check_setup.py` — checks Python, every package, and the Tesseract OCR program.
- `scripts/make_sample_data.py` — creates fake HR documents:
  - **11 good files:** digital PDF, scanned PDF, mixed PDF, Word (with table + image),
    PowerPoint (with table, image, notes), Excel (2 sheets), CSV, image, TXT, Markdown, HTML
  - **6 bad files:** `.exe` renamed to `.pdf`, corrupted PDF, password PDF, empty file,
    duplicate copy, a type we don't allow

**Lesson:** OCR is never perfect — it read *"Fire drill on"* as *"Fire drillon"*.
That's why we have quality checks later.

**Memory trick:** *Foundation first — config and test data before any real code.*

---

### ✅ Stage 1 — The standard format (`ingestion/models.py`)

**What:** the **one shape** every file gets turned into.

**Why:** if every parser returns the same shape, the cleaning, quality checks and Phase 2
chunking only need to understand ONE format — not PDF, Word, PPT, Excel separately.

**What we built**
- **Document** = one file → id, name, type, size, **hash**, **version**, metadata,
  **status + reason**, **quality** (score + warnings), list of **elements**.
- **Element** = one piece of content → `type` (heading / paragraph / list_item / table /
  image_ocr / image_placeholder / notes), `text` (tables as Markdown), `location`
  (page / slide / sheet — needed later for **citations**), heading level, OCR confidence,
  page kind.
- **Fixed lists (enums)** for status, element type and page kind — a typo like `"tabel"`
  is rejected immediately.

**Example**
```json
{"element_id": "el_0002", "type": "table",
 "text": "| Type | Days |\n|---|---|\n| Sick | 12 |", "location": {"page": 1}}
```

**Tests (5):** ids in order, counts, JSON save → load gives the exact same document,
bad type rejected, location labels.

**Memory trick:** *One form for every parcel — Document = the file, Element = one piece,
Location = where it came from.*

---

### ✅ Stage 2 — The gate (`intake.py`, `safety.py`, `manifest.py`)

**What:** the front door. Every file is checked **before** any heavy work.

**Why:** a bad file should cost milliseconds, not an OCR run. And we must never process
the same file twice or lose track of versions.

**What we built**
- **Safety checks** (cheap ones first):
  1. allowed file type
  2. not empty
  3. under the size limit
  4. **real type from the file's first bytes** — catches an `.exe` renamed to `.pdf`
  5. file actually opens — not corrupted, no password, under the page limit
  6. zip-bomb guard for Word / PPT / Excel
  7. **SHA-256 hash** (read in 1 MB pieces, so big files never fill memory)
- **Manifest** = the register book of every file seen:
  - same hash already done → **skip as duplicate**
  - same name, new content → **version 2 of the same document**
  - failed files don't block a retry
  - saved safely (write temp file → swap), never half-written
- **Intake:**
  - ignores temp files (`~$file.docx`, `.crdownload`, hidden files)
  - waits until a file is fully copied
  - keeps an untouched copy in `data/originals/<doc_id>/v1.pdf`
  - rejected files go to `data/failed/` with a `.reason.txt`
  - inbox files are moved out; **the user's own files are only copied, never touched**

**Result on the sample files**
```
11 good files            → queued   ✅
corrupted_report.pdf     → failed   "PDF is corrupted and cannot be opened"
empty_notes.txt          → failed   "file is empty (0 bytes)"
password_protected.pdf   → failed   "PDF is password-protected"
salary_details.pdf       → failed   "name says '.pdf' but the content is really 'exe'"
setup_tool.exe           → failed   "file type '.exe' is not allowed"
leave_policy_copy.pdf    → skipped_duplicate
```

**Tests (31 total):** good pass, bad fail with the right reason, duplicate skipped,
edited file becomes version 2, manifest survives a restart, inbox vs user files, temp files.

**Memory trick:** *Check the label, check the inside, check the register, keep a copy.*

---

### ✅ Stage 3 — Detect + route (`detect.py`, `registry.py`, `parsers/base.py`)

**What:** look at the file **before** reading it, and pick the right reader.

**Why:** a 100-page PDF with 2 scanned pages should run OCR on **only those 2 pages** —
not all 100. That saves time and cost.

**What we built**
- **Per-page PDF detection** — for each page, measure:
  - how much real text it has (≥ 20 characters = has a text layer)
  - how much of the page is covered by images (> 1% = has images)
  - text only → **digital** · image only → **scanned** · both → **mixed**
- **Document labels** (simple rules, no AI): slides / spreadsheet / image / scanned /
  text-heavy. *Table-heavy* is decided **after** parsing.
- **Parser contract** — a parser gets a file and returns Elements. Nothing else.
- **Parser registry** — file type → parser. A new type = one new class with `@register`.
  It refuses two parsers for the same type.

**Result on the sample PDFs**
```
leave_policy.pdf              p1=digital  p2=digital
scanned_code_of_conduct.pdf   p1=scanned  p2=scanned
mixed_quarterly_report.pdf    p1=mixed    p2=scanned
```

**Tests (46 total):** every PDF page classified correctly, labels, registry routing,
unknown type rejected, duplicate registration blocked.

**Memory trick:** *Look first, then pick the right reader — text page → read,
picture page → OCR, both → do both.*

---

### ✅ Stage 4 — Parsers (`ingestion/parsers/`, `ocr.py`, `textutils.py`)

**What:** one reader per file type. Each one turns its file into the standard Elements.

**Why:** every file type stores content differently. Each parser knows ONE format well,
and they all give back the same shape (Stage 1), so nothing after this cares about the type.

**Shared helpers**
- `ocr.py` — Tesseract OCR. Returns text **grouped into paragraphs, each with a confidence
  score** (0–1), so the quality stage can spot bad scans. Grayscale + enlarge small images
  first; timeout so one bad image can't freeze the pipeline.
- `textutils.py` — read any text file whatever its encoding (UTF-8 / Windows) and fix
  Windows line endings; turn rows into a **Markdown table**; spot CAPITALS lines as headings.
- `image_elements()` — one function every parser uses for pictures: OCR → `image_ocr`,
  or `[Image: no text found]` if the picture has no text (so we still know it was there).

**The parsers**
| File | How it reads it |
|---|---|
| TXT | paragraphs split by blank lines; short CAPITALS line = heading |
| Markdown | `#` headings with level, `-` lists, `\|` tables, paragraphs |
| HTML | headings, paragraphs, list items, tables — **drops scripts, menus, footers** |
| CSV / Excel | every sheet → heading + Markdown table; big sheets split into 100-row tables, each with the header repeated |
| Word | walks the body **in order** (text and tables are mixed); Heading styles → levels; lists; tables (merged cells kept once); **pictures → OCR** |
| PowerPoint | per slide: title → heading, bullets → list items, tables, **pictures → OCR**, grouped shapes opened up, **speaker notes**; shapes read top→bottom, left→right |
| Image | OCR with confidence; multi-page TIFF read page by page |
| PDF | **per page by kind**: digital → text + tables; scanned → render page → OCR; mixed → both + OCR the images |

**How the PDF parser finds headings:** it measures every font size in the document. The most
common size = normal body text. Anything clearly bigger is a heading — the biggest is level 1,
next level 2, and so on. Short bold lines also count as headings.

**How it avoids reading tables twice:** tables are found first (pdfplumber), and any text
block that sits inside a table's box is skipped.

**Results on the sample files (real output)**
```
leave_policy.pdf (digital)
  heading L1  Leave Policy 2026
  heading L2  1. Sick Leave
  paragraph   Employees get 12 sick leaves per year...
  heading L3  Leave Summary
  table       | Leave Type | Days per Year | Carry Forward | ... | Earned Leave | 18 | Yes, up to 30 days |
  (page 2)    3. Maternity and Paternity Leave ... notice period is 60 days.

scanned_code_of_conduct.pdf (scanned, OCR)
  heading     CODE OF CONDUCT                       conf 0.95
  image_ocr   2. Office hours are 9:30 AM to 6:30 PM.  conf 0.96

mixed_quarterly_report.pdf
  page 1 (mixed)    heading, paragraph, table, + OCR of the picture "Satisfaction Score: 4.2 / 5"
  page 2 (scanned)  "Budget for 04 tralning approved! Rs 5,00,000"   conf 0.83  ← OCR mistakes

onboarding_deck.pptx   4 slides: titles, 4 bullets, contacts table, picture OCR, speaker notes
employee_handbook.docx headings, 3 list items, table, picture OCR "Helpdesk: ext 4455"
salary_structure.xlsx  2 sheets → 2 clean tables (15.0 shown as 15)
```

**Real-world lessons from this stage**
1. **Windows line endings** — files saved on Windows have `\r\n`. The reader now cleans them.
2. **Config that works on two machines** — the Windows Tesseract path in `.env` doesn't exist
   on Linux. OCR now uses it only if it exists, otherwise falls back to `tesseract` on PATH.
3. **OCR on small, low-quality images makes mistakes** — "Q4 training" became "04 tralning",
   with a lower confidence (0.83). The quality stage will flag pages like this.
4. **Messy metadata** — PDF dates come as `D:20261110160445+05'00'` (now turned into
   `2026-11-10T16:04:45`), and some tools write the author as `(anonymous)` (now dropped).

**Tests (58 total):** every allowed type has a parser; each sample file gives the right
headings, levels, list items, tables, OCR text, page/slide/sheet locations and page kinds;
HTML junk dropped; table text not repeated; Windows line endings removed.

**Memory trick:** *One reader per file type, one shape out — and pictures always go through OCR.*

### ⏳ Stage 5 — Clean
### ⏳ Stage 6 — Quality checks
### ⏳ Stage 7 — Save, status, logs + the `run` command
### ⏳ Stage 8 — Ingestion golden set (measure extraction accuracy)

---

## How to run (so far)

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
python scripts\check_setup.py     # should end with "All good!"
python scripts\make_sample_data.py
python -m pytest -q               # all tests should pass
```
