# QISE-LatAm Scraper

> Research instrumentation for detecting **quantum-related coursework** —
> especially **Quantum Information Science & Engineering (QISE)** — across Latin
> American university websites, including PDF curricula (*mallas curriculares*,
> *planes de estudio*, *grades curriculares*, syllabi/*sílabos*/*ementas*).

The scraper takes a list of universities and produces a **structured list of
candidate quantum-related courses**, each with an official **source link** and an
**evidence snippet**. It is deliberately **broad during discovery** and
**conservative during classification**: it captures every quantum mention it can
find but does *not* claim that every quantum course is a QISE course.

**Every output row is auditable** — it always carries a source URL and an
evidence snippet. The scraper generates *candidate evidence*, not final scholarly
conclusions; human reviewers validate each candidate.

---

## Install

```bash
cd web_scrapper
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Python 3.10+ is required. PDF support uses **PyMuPDF** (primary) and
**pdfplumber** (fallback); both are installed by `requirements.txt`.

---

## Prepare the input list

Input can be **CSV** or **YAML**, in **two modes**:

### Mode A — manual seed URLs (most precise)

```csv
institution,country,country_code,website,seed_urls,max_depth,max_pages
Instituto Balseiro,Argentina,AR,https://www.ib.edu.ar,https://www.ib.edu.ar/carreras/licenciatura-en-fisica/ | https://www.ib.edu.ar/carreras/maestria-en-fisica/,1,8
```

### Mode B — domain only (automatic seed discovery)

`seed_urls` is **optional**. Given only a domain, the scraper discovers likely
course/curriculum/faculty pages itself before crawling:

```csv
institution,country,country_code,website,language
UTEC,Perú,PE,https://utec.edu.pe,es
PUCP,Perú,PE,https://www.pucp.edu.pe,es
```

Column names are matched loosely (case-insensitive, by substring), so headers
like `Institution`, `University Name`, `Website`, `Domain`, `Seed URLs`,
`Physics Dept URL` all work.

- **`seed_urls`** — one or more starting URLs (separate with `|`, `;`, or newlines).
  Point these at course catalogs, curricula, department pages, or physics / CS /
  engineering / math program pages. Any column whose name contains `url`, `seed`,
  `dept`, `faculty`, `curricul`, `malla`, etc. contributes seed URLs.
- **`website`** — used as the crawl domain (and as a seed if no others are given).
- **`country_code`** — optional; inferred from the country name if omitted.
- **`max_depth` / `max_pages`** — optional per-institution overrides.

### How automatic seed discovery works (Stage 1)

For every institution **without** manual seed URLs (or all of them with
`--force-discover`), the scraper:

1. Collects candidate URLs from the **homepage links** (URL + anchor text),
   **robots.txt** (`Sitemap:` declarations) and **sitemap.xml** (including one
   level of sitemap-index recursion, all capped and rate-limited).
2. **Scores** each same-domain candidate with multilingual academic terms
   (*plan de estudios, malla curricular, grade curricular, cursos, facultad,
   syllabus, …*), STEM terms (*física, computación, engenharia, photonics, …*),
   PDF and sitemap bonuses, and penalties for news/events/admissions/sports.
3. Keeps the top `max_auto_seeds_per_institution` (default 20) that score at
   least `min_seed_score`, and starts the crawl there.

Every discovered seed is written to `data/processed/discovered_seeds.csv` with
its institution, discovery source (`homepage` / `robots` / `sitemap`), score,
matched terms and a human-readable reason — review it to audit what discovery
chose. Preview the seeds without crawling anything:

```bash
python main.py --input data/universities.csv --discover-seeds-only
```

Each output row records how its institution's seeds were obtained in the
**`seed_origin`** column (`manual` / `auto_discovered` / `homepage_crawl`), so
you can later evaluate whether automatic discovery performs as well as manually
curated seeds. To A/B-test both modes on the same institutions, run once
normally and once with `--force-discover` (discovered seeds then replace the
manual ones for that run).

### YAML

The legacy [sources.yaml](sources.yaml) format also works as input:

```yaml
universities:
  - name: My University
    country: Brazil
    country_code: BR
    base_url: https://www.myuniversity.edu.br
    catalog_urls:
      - https://www.myuniversity.edu.br/graduacao/fisica
    max_depth: 1
    max_pages: 10
```

---

## Run

```bash
python main.py --input data/universities.csv --output data/qise_candidates.csv
```

Useful flags:

```bash
--max-depth 2                 # link depth from each seed
--max-pages-per-domain 100    # hard cap on pages per institution
--download-pdfs true|false    # fetch & parse linked PDFs (default: true)
--country Peru                # only institutions from this country/code
--resume true|false           # skip institutions already in the output file
--limit 200                   # stop after N fragments (quick smoke test)
--dry-run                     # classify but write nothing
--no-cache                    # ignore the on-disk download cache
--no-robots                   # skip robots.txt checks (use responsibly)
--discover-seeds-only         # run seed discovery, print/write seeds, exit
--auto-discover true|false    # toggle automatic seed discovery (default: true)
--force-discover              # discover even when manual seeds exist (A/B test)
```

The crawl itself is **prioritized** (Stage 2): every URL — seeds included — is
queued by value: PDFs first, then **STEM curricula** (*plan de estudios/malla*
for física/ingeniería/…), then other curricula, then course/faculty/STEM pages,
then news / events / admissions / people pages **last** (never discarded —
contextual quantum evidence counts — but they only get budget after the
academic pages). Seeds are ranked too, so a plan de estudios discovered on the
first seed is fetched before the twentieth seed: a large university links the
planes of *all* its carreras, and without this ordering sociología curricula
would drain the budget before física. Budgets are strict: `max_depth`,
`max_pages_per_domain`, `max_pdfs_per_domain`, `request_delay_seconds`,
`timeout_seconds` (see [config.yaml](config.yaml)).

Two files are written: `qise_candidates.csv` and a `.json` sibling with the same
rows. A `data/processed/run_summary.json` records per-run statistics, including
the **QISE availability variable** (institutions/countries with ≥1 `qise_core`
row). Downloaded HTML/PDFs are cached under `data/raw/<institution>/` and double
as the audit archive.

### Quick dry run (2–3 sample institutions)

```bash
python main.py --input data/universities_sample.csv \
               --output data/sample_candidates.csv \
               --max-depth 1 --max-pages-per-domain 6
```

### Tests

```bash
.venv/bin/python tests/test_pipeline.py     # standalone, prints PASS/FAIL
.venv/bin/python -m pytest tests/           # also works under pytest
```

The suite runs offline (it generates real PDFs in-memory) and covers all seven
required scenarios: HTML course title; PDF-link discovery; PDF served at a
non-`.pdf` URL; failed PDF extraction → manual review; `Quantum Computing` →
`qise_core`; `Mecánica Cuántica I` → `quantum_foundations_or_adjacent`; and a
seminar/news page → `non_course_or_contextual`.

---

## Output columns

| Column | Meaning |
|--------|---------|
| `timestamp` | UTC ISO-8601 time of the run |
| `institution` | Institution name (from the input list) |
| `country`, `country_code` | Country and ISO-3166 alpha-2 code |
| `classification` | `qise_core` / `quantum_foundations_or_adjacent` / `non_course_or_contextual` / `unclear` |
| `confidence` | `high` / `medium` / `low` |
| `is_qise_core` | `True` when `classification == qise_core` (drives the availability variable) |
| `academic_level` | `undergraduate` / `graduate` / `unknown` — from URL signals (`/pregrado/`, `/posgrado/`), the match's local context (*maestría en…*), or the document's front matter |
| `semantic_category` | Topic, e.g. `quantum_computing`, `quantum_mechanics`, `photonics` |
| `keyword_tier` | `core` / `adjacent` / `generic` |
| `matched_keyword` | Primary matched term (canonical, accent-folded form) |
| `matched_keywords` | All matched terms (`|`-separated) |
| `course_title` | Detected course/block title (best effort) |
| `evidence_snippet` | Verbatim text window around the match — the audit trail |
| `source_type` | `syllabus` / `curriculum_grid` / `catalog` / `department_page` / `course_list` / `html_page` / `pdf` / `news` / `social` |
| `media_type` | `html` or `pdf` |
| `source_url` | Canonical URL of the evidence document |
| `pdf_url` | PDF URL (when the evidence came from a PDF) |
| `pdf_page` | 1-based PDF page number of the match |
| `found_on_page` | HTML page where the PDF link was discovered |
| `seed_origin` | `manual` / `auto_discovered` / `homepage_crawl` — how the institution's seed URLs were obtained |
| `extraction_status` | `extracted` / `failed_pdf_extraction` / `needs_manual_review` |
| `language` | `es` / `pt` / `en` (heuristic) |

Rows are deduplicated per `(source_url, semantic_category)`, keeping the
highest-confidence occurrence, and sorted institution → classification →
confidence for reviewer convenience.

---

## How PDF handling works

PDFs are essential for LatAm curricula and were the main gap in the previous
version. The scraper now:

1. **Detects PDF links** from crawled HTML — including links under `/media/`,
   `/download/`, `/archivo/`, and *"descargar malla/plan"* anchors that the old
   junk filter used to discard.
2. **Resolves relative links** to absolute URLs and normalizes them.
3. **Downloads politely** (rate-limited, size-capped, cached on disk).
4. **Detects PDFs even when the URL is not `*.pdf`**, by checking the HTTP
   `Content-Type` *and* the `%PDF` file-header magic bytes.
5. **Extracts text per page** using PyMuPDF → pdfplumber → pdfminer.six → pypdf
   (first engine that yields usable text wins), so it can report the **PDF page
   number** of each match.
6. **Records both** the PDF URL (`pdf_url`) and the page the link was found on
   (`found_on_page`).
7. **Never silently drops a PDF.** If extraction fails or returns almost no text
   (scanned/image PDFs), the file is emitted with `extraction_status =
   needs_manual_review` (or `failed_pdf_extraction` if it can't be opened at
   all) instead of vanishing.
8. **OCR is intentionally not implemented.** Scanned PDFs are *flagged*, not
   OCR'd. Enabling OCR later is isolated future work (see `requirements.txt`).

### Beyond PDFs: Excel plans, embedded documents, sibling subdomains

- **Excel curricula.** Planes de estudio published as `.xlsx`/`.xls` are
  detected (by suffix, Content-Type, or file magic) and extracted one sheet at
  a time via **openpyxl** (legacy `.xls` needs optional `xlrd`, else it is
  flagged `needs_manual_review` — never dropped).
- **Embedded / external documents.** The crawler scans `<iframe>`, `<embed>`
  and `<object>` (not just `<a>`), and fetches documents embedded from
  **Google Drive / Google Sheets / SharePoint-OneDrive** by rewriting share or
  preview links to their direct-download form (UNI embeds its pregrado plan as
  a SharePoint spreadsheet and its facultad plans as Drive iframes). Those
  hosts are only ever fetched as *documents*, never crawled as pages. Disable
  with `fetch_external_docs: false` in [config.yaml](config.yaml).
- **Sibling subdomains.** The crawl scope is the institution's *registrable*
  domain (`portal.uni.edu.pe` → anything under `uni.edu.pe`), so faculty sites
  like `fc.uni.edu.pe` stay in bounds while other institutions do not.

> Validation: running the 18 PDFs previously left undetected in `data/raw/`
> through the new extractor yields 14 cleanly extracted (50k–168k chars each) and
> 4 correctly flagged `needs_manual_review` — **0 silent drops** (previously all
> 18 produced 0 rows).

---

## How classification works (broad discovery, conservative labels)

The extractor keeps **any** fragment that mentions a quantum term (Spanish,
Portuguese, or English — see [keywords.py](keywords.py)). The classifier then
sorts that evidence into four buckets using transparent keyword + context rules
(no ML, no network — every decision is explained in the `evidence_snippet` and an
internal `explanation`):

| Classification | Meaning |
|----------------|---------|
| **`qise_core`** | QISE proper — quantum computing/information/algorithms/cryptography/communication/sensing/technologies/engineering/software/hardware/circuits/programming/ML/error-correction — appearing in a **course-like** context. |
| **`quantum_foundations_or_adjacent`** | Foundational/adjacent quantum courses — quantum mechanics/physics/optics/chemistry, condensed matter, solid state, semiconductors, photonics, atomic/molecular, modern/statistical physics. |
| **`non_course_or_contextual`** | Quantum mentioned, but on a research-group, lab, seminar, workshop, conference, news, thesis, or outreach page — **not a formal course**. |
| **`unclear`** | Course-like but too generic to decide core-vs-adjacent, or too thin to decide course-vs-not (also used for PDFs pending manual review). |

Course context is detected from **strong** signals (*sílabo*, *malla curricular*,
*plan de estudios*, *grade curricular*, *ementa*, *créditos*, *prerrequisito*, …)
and **weak** ones (*curso*, *asignatura*, *disciplina*, *catálogo*, *programa*,
*semestre*, …). Non-course context comes from signals like *seminario*,
*conferencia*, *noticia*, *tesis*, *grupo de investigación*, *escuela de verano*.

**The QISE availability variable is computed only from `qise_core`.** Adjacent
and foundational quantum courses are preserved in the dataset but are *not*
counted as QISE unless your research codebook later decides to include them.

---

## Architecture

```
main.py            CLI entry point (--input/--output/--max-depth/…)
input_loader.py    CSV/YAML → internal university dicts
pipeline.py        Orchestration, dedupe, output schema, resume, summary
dispatcher.py      Routes sources → crawlers → extractors
crawler.py         WebCrawler (PDF-sensitive BFS, robots, cache) + RSS/social
extractor.py       HTML→fragments, PDF→per-page fragments (+ failure flags)
keywords.py        Quantum + course + non-course taxonomy (ES/PT/EN), one place
qise_classifier.py Conservative 4-category rule-based classifier
utils.py           URL normalization, robots cache, snippet/title helpers, logging
geo_enricher.py    Optional per-country metadata (World Bank + manual + QS CSV)
config.yaml        Crawl behaviour + output locations
sources.yaml       Example institution list (also valid as --input)
tests/             Offline tests covering the seven required scenarios
```

---

## Reliability & politeness

- Respects `robots.txt` (fails open on unreadable robots files; disable with `--no-robots`).
- Rate-limited requests, timeouts, retries, size caps on downloads.
- Identifiable academic `User-Agent`.
- URL normalization + dedup; bounded crawl (`max_depth`, `max_pages`); no infinite crawling.
- On-disk cache so re-runs don't re-hit servers (`--no-cache` to bypass).
- Never logs in, bypasses CAPTCHAs, or scrapes private/protected content.

---

## Limitations & next steps

**Limitations**
- **No JavaScript rendering.** Sites that build course lists client-side (e.g.
  some SPA university portals) return an empty shell to a static fetch. Point
  seed URLs at static curriculum pages or PDFs, or add a headless-browser
  fetcher.
- **No OCR.** Scanned/image PDFs are flagged `needs_manual_review`, not read.
- **Heuristic language detection** and **rule-based classification** — tuned for
  precision on `qise_core`, but `unclear` and `non_course_or_contextual` still
  benefit from human review.
- **`course_title`** is best-effort (line containing the match); structured
  catalogs give the cleanest titles.
- Some institutions block automated access (403); use `manual_input.py` for those.

**Next steps**
- Optional headless-browser fetch for JS-rendered portals.
- Isolated OCR fallback (pytesseract/pdf2image) for `needs_manual_review` PDFs.
- Reviewer workflow: load `qise_candidates.csv`, adjudicate `unclear`/`needs_manual_review`.
- Optionally re-enable semantic (embedding) scoring as a *secondary* signal.
