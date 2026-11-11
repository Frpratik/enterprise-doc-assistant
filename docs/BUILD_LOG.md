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
| **Phase 2 — Store** | Chunk, embed, save to a vector database, keep it in sync | 🔨 In progress |
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
python scripts\download_model.py      # local embedding model, once (~70 MB)

# Phase 2: keep the index matching a folder, then ask questions
python -m indexing.manage sync --folder "C:\path\to\your\documents"
python -m indexing.search "how many sick leaves do I get"
```
