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
| **Phase 1 — Ingestion** | Any file comes in → checked → read correctly → one standard JSON | ✅ Done |
| **Phase 2 — Store** | Chunk, embed, save to a vector database, keep it in sync | ✅ Done |
| Phase 3 — Answer | Search + rerank + LLM answer with citations, streamed API | 🔨 In progress |
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
  - **15 good files:** digital PDF, scanned PDF, mixed PDF, a 4-page PDF with repeated
    headers/footers and broken words (Stage 5), 3 "bad quality" files — a blurry scan,
    an unreadable faded scan, a PDF with a blank page (Stage 6), Word (with table + image),
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

### ✅ Stage 5 — Clean (`ingestion/clean.py`)

**What:** tidy the extracted text **without changing its meaning**.

**Why:** real PDFs repeat the same header and footer on every page and break words across
lines. If we keep that junk, every chunk in Phase 2 will contain "INTERNAL USE ONLY — Page 3
of 4", search gets worse, and the LLM sees words like "perfor- mance".

**What it does (in order)**
1. **Fix odd characters** — ligatures (`ﬁ` → `fi`), non-breaking spaces, invisible
   zero-width characters, control characters.
2. **Join broken words** — `employ- ees` → `employees`. Only when the second part starts
   with a small letter, so real dashes like `Mumbai - Pune` and `Q3- Report` are left alone.
3. **Tidy spaces** — no double spaces, no piles of blank lines.
4. **Remove repeated headers/footers** — a short line whose *pattern* (numbers replaced by #)
   shows up on **60%+ of pages** is a header or footer. So `Page 1 of 4`, `Page 2 of 4`...
   all match `page # of #`. Only done when the document has **3+ pages** (too risky to guess
   on short documents).
5. **Remove lone page numbers** like `7`, `Page 7`, `- 7 -`.
6. **Drop empty elements** and renumber ids (`el_0001`, `el_0002`...).
7. **Detect the language** (`en`, `hi`, `mr`...) from the full text.

Tables keep their Markdown layout — only odd characters are fixed inside them.
Every change is **counted** in `quality.checks`, so we can see what cleaning did.

**New sample file:** `long_handbook.pdf` — 4 pages, the same header on every page, a
"Page N of 4" footer, and two words broken across lines. That's how most real company PDFs look.

**Before → after (real output)**
```
BEFORE (16 elements)                                   AFTER (8 elements)
ACME EXAMPLE PVT LTD - INTERNAL USE ONLY   (x4)    →   removed
Page 1 of 4 ... Page 4 of 4                (x4)    →   removed
"New employ- ees ... perfor- mance review"         →   "New employees ... performance review"

checks: joined_words=2, removed_headers_footers=8, language=en
```

**Tests (66 total):** odd characters, broken words (and real dashes left alone), spaces,
headers/footers removed on the real PDF, short documents left untouched, lone page numbers,
tables keep layout, language detection (English, Hindi, too-short text → none).

**Memory trick:** *Clean the dust, not the furniture — remove repeats and fix broken words,
never change the meaning.*
### ✅ Stage 6 — Quality checks (`ingestion/quality.py`)

**What:** an inspector that checks every document **after** parsing + cleaning, gives it a
**score (0–1)**, and decides **done**, **needs_review** or **failed** — with the reasons.

**Why:** extraction can "succeed" and still be bad — a blurry scan, a blank page, broken
characters. If we don't catch it here, wrong text silently goes into search and the bot
gives wrong answers later.

**The checks**
| Check | Meaning | Flag when |
|---|---|---|
| text_chars | any usable text at all? | 0 → **failed** (nothing to search) |
| chars_per_page | how much text per page | under 50 → OCR probably missed things |
| ocr_confidence | Tesseract's own confidence | under 0.70 → blurry / faded scan, **and it names the page** |
| garbage_ratio | share of odd symbols (■ ◆ ▓ �) | over 30% → broken encoding or bad OCR |
| page_coverage | pages that gave any text | under 80% → blank / unreadable pages, **lists which ones** |
| images_without_text | pictures where OCR found nothing | info only (could be a chart or a logo) |
| spreadsheet tables | a spreadsheet must give tables | none → parsing went wrong |

All limits live in config / `.env`.

**Score = the weakest link.** At first the score was an average — and a blurry scan with OCR
confidence 0.69 got **0.996** because four other checks were perfect. That hides the problem.
Now the score is the **lowest** sub-score, so one weak part pulls the whole document down.

**3 new "bad quality" samples** (made on purpose): a blurry canteen notice, a faded receipt
that is unreadable, and a 3-page PDF whose middle page is blank.

**Result on all samples (real output)**
```
canteen_notice_scan.png       needs_review  0.688  low OCR confidence 0.69 on: Page 1
policy_with_blank_page.pdf    needs_review  0.667  only 2 of 3 pages have text (empty: 2)
faded_receipt_scan.png        failed        0.0    no text could be extracted
scanned_code_of_conduct.pdf   done          0.946
leave_policy.pdf              done          1.0
(all other clean files)       done          ≥ 0.94
```

**Big lesson: OCR confidence is NOT accuracy.** While making the samples, Tesseract read
"1:00 PM" as "2:00 PM" and still said it was 85% confident. The mixed report's "Q4 training"
became "04 tralning" without being flagged. Confidence only catches *obviously* bad scans.
To measure real accuracy we compare against known correct text — that's the golden set in Stage 8.

**Tests (83 total):** clean files → done; blurry scan → needs_review naming the page;
blank page → needs_review naming the page; unreadable scan → failed; garbage text flagged;
spreadsheet without tables flagged; garbage ratio maths.

**Memory trick:** *The inspector trusts nothing — check text, pages, OCR and garbage,
score by the weakest link, and always say why.*
### ✅ Stage 7 — The full pipeline + the `run` command (`pipeline.py`, `store.py`, `logs.py`, `printer.py`, `run.py`)

**What:** all the stages connected into one pipeline, plus saving, logs, a readable printout,
and the command you actually run.

**Why:** until now each stage worked alone. A real system needs one entry point that runs
every stage in order, never crashes on one bad file, and leaves a record of everything.

**The pipeline for ONE file**
```
gate (intake + safety) → detect → parse → clean → final label → quality → save → log
```
- If anything breaks while parsing/cleaning, **that file** becomes `failed` with the error
  as the reason, a copy goes to `data/failed/` with a `.reason.txt`, and **the run goes on**.
- Every file gets a JSON result: `data/output/<doc_id>_v<version>.json`
  (old versions are kept, never overwritten; written safely via temp file + swap).
- The manifest gets the **final** status and reason.
- One **JSON log line** per file in `data/logs/ingestion.jsonl` (file, status, reason, type,
  parser, pages, elements, score, time taken) — the same format CloudWatch / Datadog read.

**How to run it**
```bash
python -m ingestion.run --file "C:\path\to\file.pdf"        # one or more of your files (only copied, never changed)
python -m ingestion.run --folder "C:\path\to\folder"        # every file in a folder
python -m ingestion.run --watch                                # process data/inbox, then keep watching it
# options: --summary (only the table)   --max-chars 300 (shorten long text)
```
The command exits with **code 1 if any file failed** (useful later in scripts and CI).

**What you see (real output)**
```
FILE   : leave_policy.pdf
TYPE   : pdf, 2 pages, lang=en, label=text-heavy
STATUS : [OK] DONE   score=1.0
[Page 1 - Heading H1] Leave Policy 2026
[Page 1 - Heading H2] 1. Sick Leave
[Page 1 - Paragraph 1] Employees get 12 sick leaves per year...
[Page 1 - Table 1]
| Leave Type | Days per Year | Carry Forward | ...
QUALITY: text_chars=677, chars_per_page=338.5, garbage_ratio=0.0, page_coverage=1.0
FOUND  : 6 heading, 4 paragraph, 1 table
SAVED  : data/output/doc_206b74c2ba45_v1.json

SUMMARY (2 files)
leave_policy.pdf        done   1.00
onboarding_deck.pptx    done   0.94
```

**Watch mode, tested live:** started the watcher, then dropped 3 files into the inbox while
it was running → the CSV was done, the fake PDF (really an `.exe`) was rejected, the photo
was OCR'd — and the **inbox was empty afterwards** (processed files are moved out).

**Small fixes found by running it for real**
1. A 2-second "is the file still copying?" wait was slowing every file. It only matters for
   files arriving in the inbox — files you point at with `--file` are already complete, so
   they skip it now.
2. The console printout is **plain text only** (no emoji) and the output is forced to UTF-8,
   so old Windows consoles never crash on Hindi text or symbols.

**Tests (90 total):** every sample gets the right final status; the saved JSON loads back
complete; the manifest has the final status; **one crashing parser doesn't stop the next
file**; logs are valid JSON lines; the run command prints DONE/FAILED and returns exit code 1
on failure; folder mode with `--summary`.

**Memory trick:** *One file in, every stage in order, one record out — and a bad file
only fails itself, never the whole run.*
### ✅ Stage 8 — Golden set + eval gate (`evals/`, `ingestion/evaluate.py`, `tests/test_eval_gate.py`)

**What:** for every sample file we wrote down **the correct result by hand, once** — the
expected status, all the text in reading order, the headings, and the table rows. Then we
run the real pipeline and **measure** how close it gets, with numbers.

**Why:** Stage 6 showed that OCR confidence is NOT accuracy (Tesseract read "1:00" as "2:00"
and was 85% sure). "Looks fine to me" is not a measurement. With a golden set, every change
to the code is **measured**, and we can prove improvements with before/after numbers.

**The golden set:** `evals/ingestion_golden.json` — 21 files (15 good + 6 bad).

**The 6 metrics**
| Metric | Question it answers |
|---|---|
| status_ok | Did the file get the expected status (done / needs_review / failed / duplicate)? |
| word_recall | How much of the **correct** text did we get, in order? (missed or misread words lower it) |
| word_precision | How much of **our** text is correct? (junk like headers/footers lowers it) |
| heading_recall | Were the expected headings found **as headings**? |
| table_accuracy | Were the expected table rows found with **every cell right**? |
| junk_removed | Is the junk (headers, footers, scripts, broken words) gone? |

Status is checked on every file. Text/heading/table numbers are averaged only over files
that are **supposed** to be read correctly — the deliberately bad scans are shown row by row
but don't hide real changes in the average.

**How to run**
```bash
python -m ingestion.evaluate                   # score table + comparison with the baseline
python -m ingestion.evaluate --save-baseline   # store today's numbers as the new baseline
```
It runs in a temporary folder, so your real `data/` is never touched.

**Baseline (real numbers, saved in `evals/ingestion_baseline.json`)**
```
status_ok        1.000   (21 of 21 files got the right status)
word_recall      0.988
word_precision   0.988
heading_recall   1.000
table_accuracy   1.000
junk_removed     1.000

weakest text: faded_receipt_scan.png 0.00 (unreadable, expected)
              canteen_notice_scan.png 0.72 (blurry, flagged needs_review)
              notice_board_photo.png 0.89 ("drillon" - OCR joined two words)
              mixed_quarterly_report.pdf 0.97 ("04 tralning")
```

**The eval gate** (`tests/test_eval_gate.py`) — minimum numbers that every code change must
keep: status 100%, recall/precision/headings/tables ≥ 95%, junk removed 100%.
In Phase 9 this exact test runs in GitHub Actions on every push.

**Proof it works:** we simulated a bad code change (header/footer removal switched off).
```
junk_removed     0.500  FAIL  <-- gate blocks this change
word_precision   0.964  PASS     (overall still passed!)
long_handbook.pdf  precision 0.705, junk_removed 0.0
```
**Lesson:** one metric alone can miss a problem — overall precision still looked fine because
one bad file was averaged with good ones. That's why we check several metrics.

**Tests (97 total):** 90 earlier tests + 7 eval-gate checks.

**Memory trick:** *Write the right answer once, then let numbers — not eyes — decide if a change is better.*

---

## ✅ Phase 1 complete

Any file → safety gate → per-page detection → the right parser (with OCR) → cleaning →
quality score → saved JSON + status + logs → measured against a golden set with an eval gate.

**Numbers:** 9 file types, 21 golden files, 97 tests, word accuracy 0.988, tables 1.000.

**Next phase:** Phase 2 — chunk the elements, embed them, store them in a vector database,
and keep the index in sync when files change.

---

## Phase 2 — Store (chunk, embed, save, keep in sync)

**Goal:** cut the clean Elements into good chunks, turn each chunk into numbers (embeddings),
save them in Postgres + pgvector, and keep the index correct when files are added, changed or deleted.

**Analogy:** Phase 1 was the sorting room. Phase 2 is the **library** — cut each book into
index cards (one topic per card, chapter name on top), give each card a "meaning address"
(embedding), put them on shelves (database), and when a book gets a new edition, swap all its
cards at once.

**Decisions**
- **Embeddings:** a **local, free model** by default (`BAAI/bge-small-en-v1.5` via fastembed —
  no API key, runs offline). OpenAI stays an option behind the same interface.
- **Database:** **Postgres 16 + pgvector in Docker** — the industry-standard setup, and the
  same thing we'll run on AWS later.

**Design rules**
1. Chunk by meaning (section / slide / table), never by raw character count.
2. Every chunk carries its context: document title + heading path + page/slide.
3. Tables stay whole, or are split by rows with the header repeated.
4. One embedding interface — changing the model is a config change.
5. Never embed the same text twice (content hash = cache).
6. Search never sees a half-updated document (write new version inactive → swap in one transaction).
7. Soft delete first, clean up later.
8. The database can always be rebuilt from the original files.
9. Chunking choices are decided by a retrieval golden set (recall@5, MRR), not by guessing.

**Stages:** 0 database setup · 1 chunker · 2 embeddings · 3 DB layer · 4 indexer + atomic swap ·
5 sync + delete + search command · 6 retrieval golden set · 7 background worker (Celery + Redis)

---

### ✅ Stage 0 — Database setup (`docker-compose.yml`, `indexing/db.py`, `indexing/migrate.py`)

**What:** Postgres + pgvector + Redis running in Docker, a connection pool, versioned
database migrations, and the setup check now looks at the database too.

**Why:** everything in Phase 2 lives in the database. The schema must be versioned like code
(so every machine — your laptop, CI, AWS — builds exactly the same tables).

**What we built**
- `docker-compose.yml` — `pgvector/pgvector:pg16` (Postgres with pgvector built in) and
  `redis:7` (used in Stage 7). Data is kept in Docker volumes.
- `indexing/db.py` — a **connection pool** (connections are opened once and reused) and
  `with connect() as conn:` = **one transaction**: everything is saved if the block finishes,
  **nothing** is saved if it crashes halfway.
- `indexing/migrate.py` — runs numbered SQL files (`001_...sql`) **once each, in order**,
  each in a transaction, and records them in `schema_migrations`.
- `check_setup.py` — now checks Postgres, pgvector and pending migrations.

**The tables (migration 001)**
| Table | Holds |
|---|---|
| `documents` | one row per document **version**: hash, status, reason, label, language, **is_active**, soft-delete time. Replaces `manifest.json`. |
| `embeddings` | one vector per **(text hash, model)** — this table IS the embedding cache, and different models can live side by side |
| `chunks` | content (for users/LLM), embed_text (title + heading path + content), heading path, location, token count, **is_active**, plus `tenant_id` / `allowed_roles` ready for Phase 5, and an automatic **keyword-search column** ready for Phase 3 |

**Why embeddings get their own table:** the same text (e.g. an unchanged section in version 2
of a document) is embedded **once** and reused forever; switching to a different model later
just adds new rows — nothing breaks; and each model gets its own fast search index.

**Testing without touching your data:** every DB test creates a **brand-new database**,
runs the migrations, and drops it afterwards. Tests use your Docker Postgres; on machines
without Docker they can use `pgserver` (a pip-installed Postgres with pgvector).

**Tests (103 total):** all tables created; migrations run only once; vectors go in and come
back the same; a crash halfway saves **nothing**; a chunk can't exist without its document;
keyword search works with stemming ("leaves" matches "leave").

**Memory trick:** *Shelves before books — versioned tables, a pooled connection, and
all-or-nothing transactions.*

---

### ✅ Stage 1 — Chunker (`indexing/chunker.py`, `chunk_checks.py`, `tokens.py`)

**What:** cut every document into chunks **by meaning**, each one carrying its context.

**Why:** search finds *chunks*, not documents. A chunk that is half a sentence, or two unrelated
topics glued together, or "12 days" with no idea *12 days of what*, gives bad search results
and bad answers. Good chunks are the biggest single lever on RAG quality.

**How each type is chunked**
| Content | Chunk |
|---|---|
| PDF / Word / MD / HTML / TXT / images | one chunk per **section** (heading + its text) |
| Tables inside documents | their **own chunk**, with the section heading on top |
| PowerPoint | one chunk per **slide** (title, bullets, table, picture text, speaker notes) |
| Excel / CSV | one chunk per **table block**, header always included |
| A section that is too long | split at **sentence** boundaries, with a small **overlap** |
| A table that is too long | split by **rows**, header **repeated** in every piece |

**Every chunk carries its context**
```
heading_path : ['Leave Policy 2026', '1. Sick Leave']
location     : {'pages': [1]}         (or {'slides': [3]}, {'sheet': 'Salary Bands'})
content      : "1. Sick Leave\nEmployees get 12 sick leaves per year..."     <- shown to user / LLM
embed_text   : "Leave Policy 2026 > 1. Sick Leave\n\n" + content           <- what gets embedded
```

**Sizes are counted with the model's own tokenizer.** Our local model (bge-small) reads at most
**512 tokens** — anything longer is silently cut off. So sizes are measured exactly the way the
model sees them (target 350, hard limit 480, overlap 40 — all in config). OpenAI's `tiktoken`
was not used: it counts *OpenAI* tokens, not ours, and it downloads its vocabulary at runtime.

**The embedding model** is downloaded once with `python scripts/download_model.py` into
`models/` (about 70 MB, kept out of git).

**Automatic chunk checks** (every document, every time)
- no empty chunk, no chunk above the token limit, every chunk has a heading path
- every table piece starts with its header row
- **every element ends up in a chunk** — nothing lost, and nothing duplicated unless it was split

**Result on the samples (real tokenizer)**
```
FILE                         CHUNKS  TYPES                 TOKENS   CHECKS
leave_policy.pdf                  5  4 text, 1 table        26-69   OK
scanned_code_of_conduct.pdf       2  2 text                 39-67   OK
mixed_quarterly_report.pdf        4  3 text, 1 table        22-65   OK
long_handbook.pdf                 4  4 text                 25-36   OK
employee_handbook.docx            4  3 text, 1 table        17-52   OK
onboarding_deck.pptx              4  4 slide                16-50   OK
salary_structure.xlsx             2  2 table                43-66   OK
hr_faq.html                       3  2 text, 1 table        28-37   OK
```

**Bugs the tests caught**
1. When one very long "sentence" (e.g. an OCR line with no full stops) needed cutting more than
   once, **part of the text could be dropped**, and one giant word could **loop forever**.
   Rewritten: cut by words, and a single word always fits, so it can never loop.
2. The checks flagged that spreadsheet **sheet-name headings were not linked to any chunk**.
   Fixed — the sheet heading now belongs to its table chunk.
3. The mixed report's path repeated the title (`Quarterly HR Report Q3 2026 > Quarterly HR Report - Q3 2026`)
   because the PDF title and its first heading are the same words. Near-duplicates are now skipped.

**Stress tests:** a 120-sentence section (split into several pieces, every piece under the
limit, overlap carried over) and a 300-row table (split by rows, header repeated, every row
exactly once and in order).

**Tests (126 total):** every sample passes the chunk checks; sections keep their path and page;
tables are their own chunk; one chunk per slide with notes; sheet chunks keep the header;
long sections and big tables split correctly; a lost element is detected.

**Memory trick:** *Cut by meaning, label every card — section, slide or table, with its path
and page, never bigger than the model can read.*

---

### ✅ Stage 2 — Embeddings (`indexing/embeddings.py`)

**What:** turn each chunk into a list of 384 numbers (a vector) so that texts with similar
**meaning** get similar vectors — even with no shared words ("internet claim" ↔ "Reimbursement Limits").

**Why:** keyword search only finds exact words. Employees ask in their own words; embeddings
let search match the *meaning*.

**What we built**
1. **One interface, two models** (chosen in config — the rest of the code never changes):
   - `LocalEmbedder` — `BAAI/bge-small-en-v1.5` via fastembed: **free, offline**, 384 numbers,
     loaded from the `models/` folder (no internet needed at run time)
   - `OpenAIEmbedder` — `text-embedding-3-small`: needs an API key, 1536 numbers, sends up to
     100 texts per request, retries with backoff
2. **Documents and questions are embedded differently.** BGE models are trained to expect an
   instruction in front of *search questions* (not documents):
   `"Represent this sentence for searching relevant passages: "`. fastembed doesn't add it, so we do.
   Measured: with the instruction, the right chunk wins by a bigger margin (gap 0.176 vs 0.155).
3. **Vectors are normalised** (length 1), so "how close" = a simple dot product (cosine similarity).
4. **Cache in front of the model** (`embed_with_cache`): every text is identified by its SHA-256
   hash. A (text, model) pair is embedded **once, ever** — re-running, or version 2 of a document
   with mostly unchanged sections, costs almost nothing. Duplicate texts in one batch are embedded once.

**Real result — 25 chunks from 8 sample files, 7 questions**
```
Q: how many sick leaves do I get        1st 0.798  leave_policy.pdf > 1. Sick Leave
Q: what is the notice period            1st 0.798  leave_policy.pdf > 4. Notice Period
Q: how much can I claim for internet    1st 0.688  employee_handbook.docx > Reimbursement Limits
Q: who do I contact in IT               1st 0.648  onboarding_deck.pptx > Key Contacts
Q: what is the HRA percentage           1st 0.699  salary_structure.xlsx > Sheet: Components
Q: when is salary credited              1st 0.872  hr_faq.html > When is salary credited?
Q: how long is probation                1st 0.817  long_handbook.pdf > Probation
```
**7 of 7 correct**, none of them by matching exact words. Speed in the test sandbox:
about 10 chunks/second on 2 CPU cores, for free.

**Tests (136 total):** 384 numbers and length 1; same text → same vector; similar meaning is
closer ("sick leave" vs "medical leave" beats "sick leave" vs "laptop policy"); 4 questions
each find the right chunk; the instruction goes on questions only; batching keeps the order;
**cache:** second run embeds only the new text, duplicates embedded once, different models
cached separately; OpenAI requests batched as 100 + 100 + 50.

**Memory trick:** *Meaning becomes numbers — close numbers, close meaning; questions get an
instruction, and the same text is never embedded twice.*

---

### ✅ Stages 3 + 4 — Documents table + Indexer with atomic swap (`doc_store.py`, `indexer.py`, `service.py`)

**What:** the manifest moves into Postgres, and every processed document is **indexed**:
chunked, embedded and saved so it becomes searchable — and updated safely when the file changes.

**Why:** in real companies files change all the time. The index must never show a half-updated
document, never mix old and new versions, and never pay to re-embed text that didn't change.

**What we built**
1. **`PgManifest`** — the `documents` table replaces `data/manifest.json`, with the same 3
   methods (find by hash, latest by name, save), so the Phase 1 gate works unchanged.
   The JSON manifest is still used when there is no database (Phase 1 keeps working alone).
2. **The indexer — 4 steps:**
   ```
   1. CHUNK   chunk_document() + check_chunks()   -> refuse to index broken chunks
   2. EMBED   embed_with_cache()                   -> only never-seen text goes to the model
   3. WRITE   insert chunks as INACTIVE            -> transaction 1
   4. SWAP    old version off, new version on      -> transaction 2 (all or nothing)
   ```
   If anything crashes between WRITE and SWAP, search still sees the complete **old** version.
   Re-running is always safe (the half-written rows are replaced).
3. **Only approved content becomes searchable.** `done` documents are indexed; `needs_review`
   documents (blurry scans, blank pages) **wait for a person to approve them** — wrong text in
   search means wrong answers. One setting (`index_needs_review`) changes this.
4. **One fast search index (HNSW) per embedding model**, created automatically.
5. **Indexing results are recorded** on the document: when, how many chunks, which model, or the error.
6. **`python -m ingestion.run`** now indexes automatically when the database is running, and
   prints what happened. If the database is down, it says so and keeps working without indexing.

**Live run with the real model**
```
leave_policy.pdf            DONE          INDEXED: 5 chunks searchable (5 new embeddings, 0 reused)
onboarding_deck.pptx        DONE          INDEXED: 4 chunks searchable (4 new embeddings, 0 reused)
policy_with_blank_page.pdf  NEEDS REVIEW  INDEXED: no - waiting for a person to approve it
```

**The key scenario — HR edits ONE section of a policy**
```
VERSION 1                         INDEXED: 3 chunks (3 new embeddings, 0 reused)
Hotels: Rs 4,000 -> Rs 5,500      INDEXED: 3 chunks (1 new embeddings, 2 reused, replaced v1)

What search sees now:
  v2 | Booking | Flights must be booked ... 7 days in advance.
  v2 | Hotels  | Hotel stays are allowed up to Rs 5,500 per night ...
  v2 | Claims  | Submit travel claims within 15 days of returning.
documents table: v1 inactive (kept for history), v2 active
```
Only the changed section was re-embedded; the other two were reused from the cache.

**A bug in a test (worth remembering):** the crash test used `monkeypatch.undo()` to "switch
the power back on". That undid **everything** patched in the test — including the fixture that
points the test at its throwaway database — so the next step tried to reach the **real**
database address and hung. On a developer machine it could have written into real data.
Fixed by patching only the crash inside `monkeypatch.context()`. **Lesson: test isolation needs
the same care as production code.**

**Tests (145 total):** a new document is indexed and searchable; a duplicate file isn't indexed
twice; **a new version re-embeds only the changed section** and search sees only the new version;
**a crash between write and swap keeps the old version live**, and re-running finishes the job
without re-embedding; re-indexing the same version creates no duplicates; needs_review waits
for approval; failed files are recorded; one HNSW index per model.

**Memory trick:** *Write the new cards face-down, then flip all of them at once — and only
re-write the cards whose text changed.*

---

### ✅ Stage 5 — Sync, delete, approve, cleanup + search (`sync.py`, `manage.py`, `search.py`)

**What:** keep the index matching a folder automatically, manage documents (delete, approve,
cleanup, list), and **ask questions to your own documents** from the terminal.

**Why:** a company folder changes every day. If the index doesn't follow — deleted policies still
answering, old versions still showing — the bot gives confidently wrong answers.

**Sync — compares the folder with the database and builds a plan**
| Case | What happens | Cost |
|---|---|---|
| new | ingest + index | full |
| unchanged (same name, same hash) | skip | zero |
| changed (same name, new hash) | new version, **only changed sections re-embedded** | small |
| renamed (a "new" file with a vanished file's content) | just rename it — **no re-indexing** | zero |
| deleted | **soft delete**: hidden from search at once, rows kept until cleanup | zero |

A naive sync treats a rename as "delete + add" and throws away all its embeddings. Ours spots
the same content under a new name. `--dry-run` shows the plan without changing anything.

**Other commands**
```bash
python -m indexing.manage sync --folder "C:\path\to\company\docs" [--dry-run]
python -m indexing.manage list               # every document, version, status, searchable or not
python -m indexing.manage delete DOC_ID      # hide from search (soft delete)
python -m indexing.manage approve DOC_ID     # a person checked a needs_review doc -> searchable
python -m indexing.manage cleanup --days 30  # hard-delete old deleted data + unused embeddings
python -m indexing.search "how many sick leaves do I get" -k 3
```

**Live demo — 8 sample files in a "company folder"**
```
$ python -m indexing.manage sync --folder <company folder>
  new: 8 -> 25 chunks indexed

$ python -m indexing.search "what happens when I leave the company"
1. [0.687] long_handbook.pdf > Exit Process  (page 4)
   Employees must return the laptop and ID card on the last working day.

$ python -m indexing.search "can I claim my internet bill"
1. [0.612] employee_handbook.docx > Reimbursement Limits
   | Internet | 1,000 | ...
```
Then HR edited one file, deleted one and renamed one:
```
$ python -m indexing.manage sync --dry-run
  new: 0   changed: 1   unchanged: 5   renamed: 1   deleted: 1
$ python -m indexing.manage sync
  it_policy.txt    1 chunks (1 new)        <- only the edited file re-embedded
  renamed  -> wfh_policy.md                <- zero re-embedding
  deleted  hr_faq.html                     <- gone from search immediately
```
After the sync, "can I work from home" finds `wfh_policy.md`, the password question finds
**version 2** of the IT policy, and "when is salary credited" no longer finds the deleted FAQ.

**Something to fix in Phase 3:** with the FAQ deleted, "when is salary credited" still returns
*something* — the closest remaining chunk (a salary table, score 0.652) which doesn't answer it.
Vector search **always** returns its nearest neighbours, even when nothing is relevant. The answer
step needs a **minimum score / "I don't know"** rule.

**Tests (160 total):** first sync adds everything and the second sync does nothing; dry run changes
nothing; a changed file becomes version 2; a deleted file disappears from search (but its rows stay);
**a renamed file is not re-embedded at all**; cleanup only removes data older than N days, including
chunks and unused embeddings; approve makes a needs_review document searchable; search returns the
best first and respects k; with the real model, 3 questions each find the right file.

**Memory trick:** *Compare the shelf with the folder — add new, skip same, re-do only what changed,
relabel what was renamed, and hide what was removed.*

---

### ✅ Stage 6 — Retrieval golden set + chunking comparison (`evals/retrieval_golden.json`, `indexing/retrieval_eval.py`)

**What:** 30 real employee questions with **where the answer is**, and a measurement of how
well search finds it — comparing 4 chunking methods on exactly the same questions.

**Why:** "structure-aware chunking is better" is a claim. This turns it into a number, and gives
every future change (new chunk size, new model, hybrid search in Phase 3) a before/after score.

**How it's measured**
- Questions are worded **differently** from the documents, like real employees ask
  ("doctor's note" for "medical certificate", "hand back on my last day" for "Exit Process").
- A chunk counts as correct if it comes from the right file **and contains the answer phrase** —
  so methods with different chunk boundaries are compared fairly.
- **recall@k** = share of questions whose answer is in the top k chunks.
  **MRR** = average of 1/rank (answer at #1 → 1.0, #2 → 0.5, #3 → 0.33, not found → 0).

**The 4 methods**
| Method | What it is |
|---|---|
| structure | ours: sections / slides / tables, heading path in the embedded text |
| structure_raw | ours without the heading path (to measure what the context line is worth) |
| recursive | LangChain-style RecursiveCharacterTextSplitter: 500 characters, 100 overlap |
| fixed | naive: cut every 500 characters, no overlap |

**Results (30 questions, bge-small) — saved as `evals/retrieval_baseline.json`**
```
METHOD           CHUNKS   R@1    R@3    R@5    MRR
structure            33  0.967  1.000  1.000  0.983
structure_raw        33  0.967  1.000  1.000  0.983
recursive            13  0.833  0.933  0.967  0.897
fixed                13  0.833  0.933  1.000  0.898
```
**Structure-aware chunking puts the right chunk at #1 for 29 of 30 questions (96.7%), vs 83.3%
for recursive and fixed-size chunking.** The one miss: "When are performance **reviews** done?"
ranked *Probation* first (it literally says "performance review"); *Appraisals* came 2nd.

**Read these numbers honestly**
1. **recall@5 is too easy here.** The naive methods have only 13 chunks, so their top 5 is almost
   40% of all the text. **R@1 and MRR are the fair comparison** on a corpus this small.
2. **The heading path made no difference on this set** (`structure` = `structure_raw`): every one
   of our chunks already starts with its section heading, and these 12 documents cover clearly
   different topics. It should matter more when many documents have look-alike sections
   (e.g. "Eligibility" in ten different policies) — to be re-measured with a bigger corpus.
3. **Bigger naive chunks have a hidden cost the metric doesn't show:** a 500-character piece often
   spans 2–3 sections, so the LLM in Phase 3 would get more unrelated text and vaguer citations.
4. **30 questions on 12 short documents is a start, not a verdict.** The golden set should grow
   with longer, real documents.

**Speed:** parsed documents and embeddings are cached in `evals/.cache` (not in git), so re-running
the whole comparison after a change takes seconds.

**Tests (166 total):** the golden set checks itself (every answer phrase must really exist in its
document — a typo can't fake a result); questions unique; relevance needs the right file AND phrase;
fixed chunks cover the text exactly; recursive chunks respect size and overlap; and a **retrieval
gate**: our chunking must keep recall@5 ≥ 0.95, recall@1 ≥ 0.90 and MRR ≥ 0.90.

```bash
python -m indexing.retrieval_eval                   # compare all methods with the baseline
python -m indexing.retrieval_eval --save-baseline
```

**Memory trick:** *Ask real questions, know where the answers live, and count how often the
right card comes out on top — R@1 and MRR tell the truth, recall@5 flatters small libraries.*

---

### ✅ Stage 7 — Background worker with Celery + Redis (`indexing/worker.py`, `indexing/jobs.py`)

**What:** parsing, OCR and embedding run **in the background**. Whoever submits a file (the command
line now, the API later) gets a job id back at once and checks on it later.

**Why:** one PDF can take seconds (OCR) and a folder can take minutes. A web request must never wait
for that, and a crash must not lose the job.

**Analogy:** a restaurant. The **waiter** (`jobs.py`) takes the order and hands you a ticket. The
**board** (Redis) holds the orders. The **cook** (the Celery worker) cooks them one by one. The
**alarm** (Celery Beat) starts the cleaning round every night.

**What we built**
1. **Two tasks:** `ingest_file` (process + index one file) and `sync_folder` (make the index match a folder).
2. **Safe by design**
   - `acks_late`: a job leaves the queue only **after** it finishes, so a worker crash puts it back.
   - `prefetch 1`: the worker takes one slow job at a time. Time limit: 10 minutes per job.
   - **Retries only for temporary problems** (database restarting, connection lost): up to 3 times,
     waiting longer each time. A **broken file is never retried** - it ends as `failed` with a reason.
   - Running the same job twice is safe (the Phase 1 gate skips exact duplicates).
3. **Nightly sync at 02:15** (Celery Beat).
4. **Fail fast at startup:** the worker refuses to start if the database is down or a migration is
   pending. Real case: the worker ran against a database without migration 002, and every job failed
   with `column "indexed_at" does not exist`.
5. **Commands**
```bash
docker compose up -d
celery -A indexing.worker worker --loglevel=info --pool=solo     # --pool=solo is needed on Windows
celery -A indexing.worker beat --loglevel=info                   # only for the nightly sync
python -m indexing.jobs submit data\samples\good\leave_policy.pdf
python -m indexing.jobs status JOB_ID
python -m indexing.jobs sync --folder "C:\path	o\docs"
```

**Real run against the live worker**
```
leave_policy.pdf        done, 5 chunks searchable (0 new, 5 reused)
same file again         skipped_duplicate (safe, nothing doubled)
corrupted_report.pdf    failed in 0.3 s: "PDF is corrupted and cannot be opened"  (not retried)
sync of a 2-file folder new: 2
DATABASE STOPPED, then a job submitted:
  worker log   "Retry in 1s: OperationalError(...)"
  database back -> "retry_demo.md: done, 1 chunks searchable"
```

**Bugs found by running it for real**
1. **The test hang.** `self.update_state(...)` (shows "running" in the job status) writes to the Redis
   result backend **even in eager test mode**, so tests secretly needed Redis. Now one helper skips it
   in eager mode. Proof: the 8 worker tests pass with the Redis container **stopped**.
2. **Config patching in tests.** `monkeypatch` could not undo changes on Celery's config object (it
   crashed on teardown). The fixture now sets and restores the values itself.
3. **A "duplicate" that was never indexed.** The earlier missing-migration crash left `leave_policy.pdf`
   as `done` but **not searchable**. Re-submitting it was skipped as a duplicate - for ever. Now, if
   a duplicate's earlier copy finished processing but has no index record, it is indexed instead of skipped.
   This fixed the real stuck row ("5 chunks searchable").

**Honest limits**
- Eager mode cannot re-run a retry, so the unit test only checks that the task **asks** for a retry.
  The real retry was proven by stopping Postgres (above), by hand - not automated.
- While a job is retrying, its status can still say RUNNING (the PROGRESS note hides RETRYING).
- One worker process at a time (`--pool=solo`). Fine for now; more workers can be added later.

**Tests (168 total, 8 new):** ingest job indexes; same job twice is safe; a temporary DB error asks for a retry;
a broken file is not retried; sync job; reliability settings (late ack, prefetch 1, time limit);
nightly schedule at 02:15; **an unindexed duplicate gets indexed**. None needs Redis.

**Memory trick:** *Waiter takes the order, board holds it, cook cooks it - retry only when the kitchen
is briefly closed, never because the food is bad.*

---

## ✅ Phase 2 complete

Clean documents -> structure-aware chunks -> embeddings (cached) -> Postgres + pgvector with an
atomic swap -> folder sync (new / changed / renamed / deleted) -> measured retrieval (R@1 0.967,
MRR 0.983) -> background jobs with retries and a nightly sync.

**Next phase:** Phase 3 - Answer: hybrid search (vectors + keywords with RRF), a reranker, an LLM
answer with citations, an "I don't know" rule, and a streamed FastAPI endpoint.

---

## Phase 3 — Answer (search better, then answer with citations)

**Goal:** given a question, find the right chunks, decide whether we really know the answer, and
write a short answer with citations — streamed through an API.

**Analogy:** the librarian fetches ~20 candidate cards (hybrid search), an expert reads them closely
and keeps the best 5 (reranker), says "I don't know" if none of them helps, and otherwise writes the
answer and notes which card each fact came from.

**Decisions**
- **LLM:** Groq, behind one small interface, with a **fallback chain** (a second big model, then a small
  one) for rate limits and errors, and a **small fast model** for simple jobs (rewriting a vague question,
  spotting greetings / off-topic questions). The API key goes in `.env`, never in chat or git.
- **Reranker:** a local cross-encoder model (free, offline, like the embedding model).

**Stages:** 1 hybrid search · 2 reranker · 3 "I don't know" rule · 4 Groq LLM layer + answer with
citations · 5 FastAPI + streaming · 6 wrap-up

---

### ✅ Stage 1 — Hybrid search (`indexing/search.py`, `indexing/search_eval.py`, migration 003)

**What:** search with **two methods and merge them**.
- **Vector search** finds the same *meaning* ("internet claim" → "Reimbursement Limits").
- **Keyword search** finds the same *words* — exact things like `ext 4455`, `HRA`, an e-mail address.
- **RRF (Reciprocal Rank Fusion)** merges the two ranked lists: each list gives a chunk `weight / (60 + rank)`,
  and the scores are added. Only **ranks** are used, never raw scores, because cosine similarity (0–1) and
  keyword scores are on different scales and can't be added fairly.

**Why:** vectors are weak on exact strings and numbers; keywords are weak on paraphrases. Real employees
ask both kinds of question.

**What we built**
1. `search(query, k, mode="hybrid" | "vector" | "keyword")`. Every hit keeps its **raw vector similarity**
   (`vector_score`) and says who found it (`found_by`: vector / keyword / both). Stage 3's "I don't know"
   rule needs the real similarity, because an RRF score only says how a chunk *ranked*, not whether it is relevant.
2. **Migration 003:** the keyword column now also covers the **heading path** (weight A) as well as the text
   (weight B), so "reimbursement" finds the chunk under that heading.
3. **Keyword search with IDF weighting** (the idea behind BM25), written in Postgres: each term is its own
   query, and a term's weight is **how rare it is** among the active chunks. A chunk's score is the sum of the
   weights of the terms it contains. Terms keep inner punctuation, so `it@acme-example.com` and `25,000` stay whole.
4. `python -m indexing.search "ext 4455" --mode keyword` (or vector / hybrid); the printout shows the RRF score,
   the meaning score and who found it.
5. **`python -m indexing.search_eval`** — the same 30 golden questions **plus 14 new exact-term questions**,
   searched with the REAL code in a temporary database (your data is never touched), in all 3 modes.

**The honest story (this stage did not go as expected)**

*First try — equal weight, Postgres `ts_rank`:*
```
paraphrase (30 questions)   vector MRR 0.983   keyword 0.610   hybrid 0.869   <- hybrid WORSE than vector
exact terms (14 questions)  vector MRR 0.929   keyword 0.716   hybrid 0.848   <- even here, keyword lost
```
Why? Three real problems in my keyword search:
1. I split `it@acme-example.com` into `it | acme | example | com`. "it" is a stop word, and "acme" / "example"
   appear in nearly every chunk.
2. `ts_rank` has **no IDF**: it rates the common word "employees" the same as the rare number "4455".
3. One big OR-query meant a single common word could decide the ranking.

*After the fix (separate terms + IDF) — keyword alone on exact terms went 0.716 → **1.000**.* But at equal
weight hybrid still hurt paraphrases (0.920), because keyword search is genuinely weak on them (0.750 alone).

*Keyword weight in RRF (a small sweep, 5 values):*
```
weight      1.0     0.5     0.3     0.2     0.1
paraphrase  0.920   0.937   0.938   0.958   0.983      (vector alone: 0.983)
exact       1.000   0.964   0.964   0.964   0.964      (vector alone: 0.929)
```
**Chosen: weight 0.1** — "vector leads, keywords rescue". It never hurts normal questions and still
fixes exact-term ones.

**Final numbers — saved as `evals/search_baseline.json`**
```
paraphrase (30)     R@1    R@3    R@5    MRR
  vector            0.967  1.000  1.000  0.983
  keyword           0.667  0.867  0.867  0.750
  hybrid            0.967  1.000  1.000  0.983     (same as vector)
exact terms (14)
  vector            0.857  1.000  1.000  0.929
  keyword           1.000  1.000  1.000  1.000
  hybrid            0.929  1.000  1.000  0.964     ("HRA" now #1; "Rs 25,000" still #2)
```

**Read these numbers honestly**
1. **Hybrid did not beat vector on the 30 paraphrase questions** — it only matched it. On a corpus of 33
   chunks, `bge-small` is already near-perfect, so there is little left to gain.
2. **The exact-term set is mine**: I wrote the 14 questions knowing the documents, and I wrote them to favour
   keyword matching. It proves hybrid *keeps* what keyword search is good at; it does not prove a general gain.
3. **The weight 0.1 was chosen by looking at these two sets.** The difference between 0.1 and 0.2 is one
   question. Treat it as a sensible default, not a tuned optimum — re-check it on a bigger, real corpus
   (long documents, IDs, product codes, many look-alike sections), which is where hybrid should matter more.
4. Keyword-only is **not** a good standalone search (0.750). It is a helper.

**Lessons**
- A "standard best practice" (hybrid search) can make results **worse** if one half is weak. Measure first.
- The bug was in the keyword half, and only the exact-term questions made it visible.

**Tests (182 total, 14 new):** terms keep e-mails and amounts whole; RRF — a chunk in both lists beats one list,
the keyword weight lowers its list, empty lists are fine; keyword search finds exact terms, stems words, reads
headings, and a **rare term beats a common term**; a hit says who found it and always has a vector score;
hybrid rescues an exact term the (fake) vectors cannot see; a question of only stop words doesn't crash;
deleted documents are not found; unknown mode rejected; the exact-term answers really exist in their
documents; and a **hybrid gate** with the real model (paraphrase MRR ≥ 0.95, exact-term MRR ≥ 0.90, and hybrid
must not be worse than vector on either set).

```bash
python -m indexing.search "ext 4455" --mode hybrid
python -m indexing.search_eval                  # compare vector / keyword / hybrid with the baseline
python -m indexing.search_eval --save-baseline
```

**Memory trick:** *Two librarians — one knows meanings, one knows exact words. Let the meaning librarian
lead, and call on the word librarian only when a rare word really matters (and weigh words by how rare they are).*

---

### ✅ Stage 2 — Reranker (`indexing/rerank.py`)

**What:** after search picks its best ~10 candidates, a **cross-encoder** re-reads each one **together with
the question** and gives it a relevance score. The best 5 go on.

**Why:** search turns the question and every chunk into numbers *separately* and compares the numbers — fast,
but shallow. A cross-encoder reads question + chunk side by side, word by word, so it can tell "notice period
for resigning" from "a long period of leave". It is too slow to run on everything, so it only reads the top candidates.

**Analogy:** search = a librarian fetching books by their covers. The reranker = an expert who opens each book
and reads the page that matters.

**What we built**
1. **Local model, free:** `Xenova/ms-marco-MiniLM-L-6-v2` (about **88 MB**, English) through fastembed — no
   PyTorch, no API key. `python scripts\download_model.py` now downloads it too.
   *(I first said "~80 MB BGE reranker". The BGE reranker is actually 1 GB; this MiniLM one matches the size I meant.)*
2. `rerank.py`: `rerank(query, hits, k)` — the reranker reads **heading path + chunk text**, sets `hit.rerank_score`,
   and re-orders. A missing model gives a clear error ("run python scripts\download_model.py").
3. `search(..., rerank=True)` — fetches `rerank_candidates` (10) results, re-reads them, returns the best k.
   `python -m indexing.search "..." --rerank` shows the reranker score next to the meaning score.
4. `search_eval` has a new row, **hybrid+rerank**.

**The scores are raw model outputs, not 0–1.** Example (real):
```
Q: "how many sick leaves do I get"   [hybrid + rerank search]
1. [rerank  6.4 | meaning 0.80]  1. Sick Leave        <- the answer
2. [rerank -1.9 | meaning 0.66]  2. Casual Leave
3. [rerank -3.7 | meaning 0.61]  Leave Summary (table)
```
Meaning scores of 0.80 / 0.66 / 0.61 look close together; the reranker makes the gap obvious (6.4 vs −1.9).

**Results (saved in `evals/search_baseline.json`)**
```
paraphrase (30 questions)     R@1    R@3    R@5    MRR
  hybrid                      0.967  1.000  1.000  0.983
  hybrid + rerank             0.933  1.000  1.000  0.967      ("Which department loses the most employees?" 1 -> 2)
exact terms (14 questions)
  hybrid                      0.929  1.000  1.000  0.964
  hybrid + rerank             1.000  1.000  1.000  1.000      ("Rs 25,000" 2 -> 1)
```

**Read these numbers honestly**
1. **The reranker did not improve retrieval on this corpus.** One question got worse, one got better: a wash.
   With only 33 chunks and `bge-small` already at 0.98, there is almost nothing left to fix. Its job (re-reading
   ten look-alike candidates and picking the true answer) shows up on **bigger corpora with many similar sections**.
2. The question it lost: the answer is a *table* (attrition per department), and the reranker preferred the
   paragraph chunk of the same section. MS-MARCO rerankers are trained on web passages and are weaker on tables and numbers.
3. **It costs time:** about **0.4 s per query for 5 candidates, 0.6 s for 10, 1.0 s for 20** (a first measurement
   under load showed 2.2 s median for 20). Quality was identical for 5 / 10 / 20 here, so the corpus can't tell them
   apart; I chose **10** as a middle ground.
4. **Do not use the reranker score as a simple "I don't know" threshold.** I measured the top score for all 44
   answerable questions and for 4 off-topic ones:
```
answerable questions, top rerank score:  min -9.5   10th percentile -7.5   median +3.1
off-topic questions (capital of France, chocolate cake, ...):  -11.0 to -11.2
answerable questions, top meaning score: min 0.55   median 0.69
off-topic, top meaning score:            0.35 to 0.51     ("What is the CEO's home address?" scored 0.51)
```
   Many answerable questions (tables, numbers) score very low with the reranker, and the margins are thin for both
   scores. **Stage 3 has to combine signals and test the rule on questions that have NO answer** — that is exactly what it is for.

**Decision:** keep the reranker as an **option** (`rerank=True`); the answer step (Stage 4) will use it, and we keep
measuring it. It earns its place only if it helps on a bigger corpus or in the "I don't know" decision.

**Tests (189 total, 7 new in `test_rerank.py`):** the reranker reads heading + text; it orders by score, keeps k and
sets the fields; empty input is fine; a missing model gives a clear error; `search(rerank=True)` fetches more
candidates, then returns the best k with the original meaning score kept; search without rerank is unchanged;
**real model:** the answer scores more than 5 points above an unrelated chunk, and it picks the real answer over
a chunk that only shares words. The hybrid gate now also checks that the reranker is not clearly worse than hybrid.

```bash
python scripts\download_model.py                         # now also downloads the reranker (~88 MB)
python -m indexing.search "how many sick leaves do I get" --rerank
python -m indexing.search_eval                           # vector / keyword / hybrid / hybrid+rerank
```

**Memory trick:** *Search fetches by the cover, the reranker reads the page — slower, so only for the top few,
and not magic: on a tiny library there is nothing left for it to fix.*

---

### ✅ Stage 3 — The "I don't know" gate (`indexing/confidence.py`, `retrieve.py`, `abstain_eval.py`)

**What:** a check that runs **before** the LLM and decides: did search find anything worth answering from?
If not, we say *"I couldn't find anything about this in the company documents"* and never call the LLM.

**Why:** vector search **always** returns its nearest chunks, even for "what is the capital of France?"
(we saw this back in Phase 2 Stage 5). Hand those chunks to an LLM and it may write a confident answer
out of irrelevant text. A company assistant that confidently makes things up is worse than none.

**Analogy:** a security guard at the door of the answer room. He turns away visitors who are obviously
lost — but he cannot judge a visitor who looks like they belong and asks for something we don't have.
That second job belongs to the LLM (Stage 4).

**What we built**
1. **A hand-written set of 32 questions with NO answer in the documents** (`evals/unanswerable.json`):
   10 **off-topic** (capital of France, bake a cake, ...) and 22 **near-topic** — they sound like our
   documents but the answer isn't there ("How much is the joining bonus?", "What is the CEO's salary?",
   "What is the CTC range for grade L4?"). I checked each against the document text. (I removed
   "Is Christmas a company holiday?" because a 4-day holiday list arguably *does* answer it.)
2. **`decide(hits)`** — two signals, **either one** can say "no":
   the best **reranker score** (below −10.0) or the best **meaning score** (below 0.56).
3. **`retrieve(question)`** — hybrid search → reranker → gate, in one call. Stage 4 starts here.
4. **`python -m indexing.abstain_eval`** — runs all 76 questions through the real pipeline in a temporary
   database and prints the three numbers, the **margins**, and every near-topic question that slipped past.
5. `python -m indexing.search "..." --rerank` now prints a `GATE:` line.

**How the thresholds were chosen (and what that really means)**
I collected the scores for all 76 questions first, then looked at them.
```
                          rerank score (best chunk)      meaning score (best chunk)
answerable (44)           weakest -9.5                   weakest 0.58
off-topic  (10)           strongest -10.5                strongest 0.54
chosen threshold          -10.0                          0.56            (the middle of each gap)
```
A grid search over both thresholds showed the trade-off:
```
rule                                        answerable kept   off-topic blocked   near-topic blocked
rerank < -10.0 or meaning < 0.55                  100%              100%                14%
rerank < -9.0  or meaning < 0.55                   93%              100%                41%
rerank < -8.0  or meaning < 0.55                   93%              100%                55%
rerank < -6.0  or meaning < 0.55                   89%              100%                64%
```
Being stricter blocks more near-topic questions — **and starts blocking questions the documents CAN answer**
(doctor's note, phone bill, monitor, family insurance: the reranker scores these −7 to −9.5 because it is a
web-search model and doesn't connect "doctor's note" with "medical certificate"). A leave-one-out check
(choose the rule on 75 questions, test on the 76th) gave **43 of 44 answerable still answered**, and 16 of 32
no-answer questions blocked.

**Final result — saved as `evals/abstain_baseline.json`**
```
answerable questions still answered      100%  of 44   (want 100%)
off-topic questions blocked              100%  of 10   (want 100%)
near-topic questions blocked by scores    18%  of 22   (the LLM must refuse the other 18)
```
Blocked near-topic: "Where is the Pune office?" (−11.3), "Who is the head of Engineering?",
"What is the CEO's salary?" and "What is the sales commission structure?". The other 18 pass the gate with scores that look completely normal
(e.g. "How many days of leave for my marriage?" — rerank −1.2, meaning 0.72).

**Read these numbers honestly**
1. **The margins are thin.** Rerank: weakest answerable −9.5, threshold −10.0, strongest off-topic −10.5 — half a point
   each side. Meaning: 0.58 / 0.56 / 0.54 — **0.02** each side. One new document or one odd question could cross a line.
2. **Both thresholds were set by looking at the same questions they are scored on.** The leave-one-out check is some
   protection, not proof. Re-run `abstain_eval` after any change to the models or the documents, and treat 44 + 32
   questions as a start. A real company corpus will have different score ranges.
3. **The gate solves only the easy half.** Scores catch *"obviously unrelated"*. They cannot catch *"related but not
   there"*, because the documents really do talk about leave, pay and holidays. **82% of the near-topic questions go
   through to the LLM.** If Stage 4's prompt does not make it refuse, the assistant will make up answers about bonuses
   and commissions. Stage 4 will be measured on these exact 22 questions.
4. The gate was designed to be **conservative on purpose**: blocking a question the documents can answer
   ("doctor's note") is a visible failure for the user; letting a near-topic question reach the LLM has a second safety net.
5. All questions are mine; the off-topic set is easy by nature (a 10-question sanity check, not a benchmark).

**Lessons**
- Search scores say *how close*, not *whether the answer is there*. "Is there an answer?" is a separate question.
- Two layers beat one clever threshold: a cheap guard for the obvious cases, a smarter judge for the subtle ones.
- Look at the margin, not just the pass rate. 100% with a 0.02 margin is a warning, not a victory.

**Tests (199 total, 10 new in `test_confidence.py`):** no hits → no answer; a clear match passes; unrelated blocked;
**either signal alone can block**; the best hit counts, not the first; the boundary values are exact; without the
reranker only the meaning score is used; readable reasons; the question sets are well-formed and unique; and a
**gate test with the real models** (100% of answerable answered, 100% of off-topic blocked, near-topic ≥ 10% as a floor).

```bash
python -m indexing.abstain_eval                 # the 3 numbers + margins + the near-topic questions that slipped through
python -m indexing.search "what is the capital of France" --rerank      # shows GATE: NO ANSWER (...)
```

**Memory trick:** *A guard at the door turns away the obviously lost, not the look-alikes — and if his margin is
only two hundredths, say so out loud. The look-alikes are the LLM's job.*

---

### ✅ Stage 4 — Groq LLM layer with fallback + answers with citations (`indexing/llm.py`, `answer.py`, `ask.py`, `answer_eval.py`)

**What:** the assistant now **answers**. Search → rerank → gate → an LLM (Groq) writes 1–3 sentences using only the
chunks it is given, with `[1]`-style citations → we **check** what it wrote → the user sees the answer and its sources.

**Why:** retrieval alone returns chunks, not answers. And an LLM alone makes things up. So the LLM is boxed in:
it may only use the excerpts, must cite them, must say "not in the documents" when they don't contain the answer —
and we don't trust any of that, we verify it.

**Analogy:** an intern who may only quote from the folders on the table, must write the page number after every
fact, and must say "not in these folders" otherwise. We then check that every page number really exists.

**What we built**

*1. The LLM layer (`llm.py`)* — one place that talks to Groq. Three models, chosen from **what your account can use**
(my first idea, `llama-3.3-70b`, is not available to you):
```
main      openai/gpt-oss-120b   writes the answers
fallback  qwen/qwen3.8-27b      a DIFFERENT model family: if one model has a problem, the other probably doesn't
small     openai/gpt-oss-20b    simple, cheap jobs; also the last fallback
chains:   main -> fallback -> small          small -> fallback
```
| Problem | What happens |
|---|---|
| rate limit (429), short wait (≤ 5 s) | wait, retry the **same** model once |
| rate limit with a long wait (e.g. daily limit) | **next model at once** — never freeze the user |
| timeout / connection / 5xx | retry once, then next model |
| model removed (404) | next model at once |
| empty reply (a reasoning model used all tokens thinking) | next model |
| bad key (401 / 403) | **stop** with a clear message — every model would fail the same way |
| every model failed | `LLMUnavailable` → the user sees "busy, try again"; **nothing is invented** |

Every call records which model answered and every attempt (for Phase 8 tracing). Checked against the **real** Groq API:
a nonexistent main model → `NotFoundError 404` → `qwen` answered, attempts recorded; a wrong key → "Groq rejected the API key".
The key lives in `.env` as `GROQ_API_KEY`; it is never printed or committed.

*2. The answer step (`answer.py`)* — the LLM is **not trusted**:
- Excerpts are numbered (`[1] leave_policy.pdf, page 1 | Leave Policy > Sick Leave`) and wrapped in `<excerpts>` tags; the
  prompt says they are **data, never instructions** (a document saying "ignore your rules" must not work).
- The LLM must reply `NOT_IN_DOCUMENTS` when the excerpts don't contain the answer (**the second "I don't know" layer**:
  near-topic questions pass the Stage 3 score gate, so the LLM has to refuse them).
- Every `[n]` must be a real excerpt number. Made-up numbers are dropped and flagged. Models cite in different styles
  (`gpt-oss` writes full-width `【1】`), so all styles are normalised to `[1]`.
- An answer with **no valid citation is not shown** (`uncited`): an unsourced answer is worse than "I don't know".
- Statuses: `answered`, `refused_gate`, `refused_llm`, `uncited`, `llm_unavailable`. The prompt is versioned (`answer-v1`).

*3. `python -m indexing.ask "..."`* — a real answer in the terminal, with sources and the model used.
```
Q: "How long is maternity leave and paternity leave?"
Maternity leave is 26 weeks and paternity leave is 10 working days, to be taken within 3 months of the child's birth [1].
Sources:  [1] leave_policy.pdf > 3. Maternity and Paternity Leave  (page 2)
[answered | model openai/gpt-oss-120b | 3299 ms]

Q: "What is the capital of France?"   ->  "I couldn't find anything about this in the company documents."
[refused_gate: stopped by the score gate (no LLM call)]
```
*4. `python -m indexing.answer_eval`* — all 76 questions through the real pipeline, **once per model with no fallback**
(so models are compared fairly). Replies are cached on disk (with their original latency) so re-runs are free.

**Results (saved as `evals/answer_baseline.json`; temperature 0; 44 answerable + 22 near-topic + 10 off-topic)**
```
MODEL                 answered  right chunk  numbers ok  near-topic refused  off-topic refused
gpt-oss-120b (main)      98%       100%         96%            100%               100%
qwen3.8-27b (fallback)  100%       100%         96%             96%               100%
gpt-oss-20b (small)      98%       100%         92%            100%               100%
```
**Even the small and fallback models follow the rules**: all three refuse nearly every question that has no answer.
That is the answer to "can a small model do simple tasks?": for *this* job, yes, on this test.

**Read these numbers honestly**
1. **A prompt example leaked a test question — and I caught it.** My first prompt used "how much is the joining
   bonus" as its refusal example; that is one of the 22 near-topic test questions, so the first 100% was partly rigged.
   I replaced the example with one that is in no test set (parking allowance), cleared the cache, and re-ran.
   Near-topic refusal stayed 100% / 100% / 100% on that run. Lesson: **never put test questions in the prompt.**
2. **Temperature 0 is NOT deterministic on Groq.** Two live runs of the same prompt differed: gpt-oss-120b changed
   wording in 20 of 76 answers, and one question flipped (`When are performance reviews done?` answered → refused);
   qwen flipped one near-topic question (`How do I reset my email password?` refused → a hedged answer: "the excerpts do
   not state how... only that passwords must be changed every 90 days [1]"); gpt-oss-20b changed 5. So **a difference of one
   question, between models or between runs, is noise.** The table above is the third live run.
3. **One genuinely hard question:** `When are performance reviews done?`. The Probation section says "Confirmation depends on
   a performance review", the Appraisals section says "once a year in April". Retrieval ranked Probation first (we saw this
   in Phase 2). Models disagree and flip: 120b once cited the wrong chunk, 120b / 20b sometimes refused, qwen answered right.
4. **"numbers ok" is a rough proxy, not correctness.** It checks that the numbers of the expected answer appear in the
   text. The 2 "misses" (`Which department loses the most employees?` → "Sales ... 11.2%", `L2 grade CTC` → "8 LPA, 14 LPA") are
   **correct answers** that my check can't see. It also can't catch a fluent wrong sentence. **Real answer scoring (faithfulness,
   relevancy) is Phase 4.**
5. **A hedge is not a refusal.** qwen sometimes writes "the excerpts do not state X, but ..." with a citation instead of
   `NOT_IN_DOCUMENTS`. That is honest but it is not what the prompt asked, and my status check counts it as answered.
   Phase 4's faithfulness checks should catch this kind of answer.
6. **Latency is unstable**: median LLM time was 1.4 s / 4.3 s / 2.9 s in one run and 0.5 s / 0.3 s / 0.5 s in the next
   (qwen's 4.3 s was probably free-tier rate-limit waiting). Retrieval + reranking adds about 1 s. A single `ask`
   took 2.8–3.3 s end to end. Don't quote one number.
7. All 76 questions are mine, on 12 tiny documents, and the prompt was tuned while looking at them (apart from the leaked
   example, which I removed). Treat these as a first measurement.

**What is NOT built yet**
- The **small model has no job of its own yet.** It is wired as the second tier and the last fallback; its first real
  uses will be input checks (greetings, off-topic, prompt-injection screening) in Phase 6, and maybe rewriting vague questions.
- **Streaming** (words appearing as they are written) comes in Stage 5 with the API.

**Tests (224 total, 25 new in `test_llm_answer.py`, no network):** a scripted fake LLM covers every fallback path —
normal answer; short rate limit waits and retries the same model; long rate limit skips to the next model with no sleep;
5xx retried once then fallback; timeouts; removed model skipped; empty answer; **bad key stops everything**; missing key;
all models failing; small tier order; error classification. And the answer step: gate refusal never calls the LLM;
citations only for sources really cited; full-width `【1】` normalised; LLM refusal; **no valid citation → not shown**;
made-up numbers dropped; "busy" message when everything fails; the prompt fences excerpts as data and a hidden
"ignore all rules" stays inside the fence.

```bash
python -m indexing.ask "How long is maternity leave?"
python -m indexing.answer_eval                         # needs GROQ_API_KEY; cached replies make re-runs free
python -m indexing.answer_eval --models openai/gpt-oss-120b
```

**Memory trick:** *An intern with only the folders on the table: quote the page, say "not in these folders" when it isn't —
and check every page number, because even at temperature 0 the same intern doesn't always write the same sentence.*

---

## How to run (so far)

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
python scripts\check_setup.py     # should end with "All good!"
python scripts\make_sample_data.py
python -m pytest -q               # all tests should pass

# run the pipeline
python -m ingestion.run --file data\samples\good\leave_policy.pdf
python -m ingestion.run --folder data\samples\good --summary
python -m ingestion.run --file "C:\any\file\you\want.pdf"

# measure extraction quality against the golden set
python -m ingestion.evaluate

# Phase 2: database (Docker Desktop must be running)
docker compose up -d
python -m indexing.migrate
python scripts\download_model.py      # local embedding model (~70 MB) + reranker (~88 MB), once

# Phase 2: keep the index matching a folder, then ask questions
python -m indexing.manage sync --folder "C:\path\to\your\documents"
python -m indexing.search "how many sick leaves do I get"        # hybrid by default; --mode vector|keyword

# measure retrieval quality (30 questions, 4 chunking methods)
python -m indexing.retrieval_eval

# Phase 3: compare vector / keyword / hybrid search on the real database
python -m indexing.search_eval
python -m indexing.abstain_eval      # the "I don't know" gate: answered / blocked / margins
python -m indexing.ask "How long is maternity leave?"     # a real answer with sources (needs GROQ_API_KEY in .env)

# Phase 2: background jobs (Docker running)
celery -A indexing.worker worker --loglevel=info --pool=solo
python -m indexing.jobs submit data\samples\good\leave_policy.pdf
```
