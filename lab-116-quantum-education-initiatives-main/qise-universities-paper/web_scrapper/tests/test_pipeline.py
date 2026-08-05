"""
test_pipeline.py — offline tests + dry-run examples for the QISE scraper.

Runs WITHOUT network access. It builds HTML strings and generates real PDF bytes
(via PyMuPDF) in-memory, then exercises the extractor, classifier, crawler
helpers, input loader and pipeline row-building end to end.

Run:
    ../.venv/bin/python tests/test_pipeline.py      # standalone, prints PASS/FAIL
    ../.venv/bin/python -m pytest tests/            # also works under pytest

Covers the seven scenarios from the brief:
  1. HTML page with a course title
  2. page linking to a PDF curriculum        (crawler link discovery)
  3. PDF whose URL is not *.pdf but content/header says PDF
  4. PDF where extraction fails -> needs_manual_review
  5. "Quantum Computing"      -> qise_core
  6. "Mecánica Cuántica I"    -> quantum_foundations_or_adjacent
  7. news/seminar page        -> non_course_or_contextual
"""

import os
import sys

# Make the package importable whether run from repo root or tests/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extractor
import crawler
import input_loader
from qise_classifier import QISEClassifier
from pipeline import Pipeline, OUTPUT_FIELDS

CLF = QISEClassifier({})
UNIV = {"name": "Test U", "country": "Peru", "country_code": "PE", "language": "es"}


# ── helpers ───────────────────────────────────────────────────────────────────

def _classify_html(html, url):
    frags = extractor.extract_from_html(html, url, UNIV)
    rows = []
    for f in frags:
        rows.extend(CLF.classify(f))
    return frags, rows


def _make_pdf(text: str) -> bytes:
    """Generate a real, text-based PDF using PyMuPDF."""
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def _make_blank_pdf() -> bytes:
    """A valid PDF with a page but no text (stands in for a scanned/image PDF)."""
    import fitz
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def _classifications(rows):
    return {r["classification"] for r in rows}


# ── SCENARIO 1: HTML page with a course title ─────────────────────────────────

def test_html_course_title():
    html = """
    <html><head><title>Plan de Estudios - Física</title></head><body>
    <table>
      <tr><th>Código</th><th>Asignatura</th><th>Créditos</th></tr>
      <tr><td>FIS-410</td><td>Computación Cuántica</td><td>4 créditos</td></tr>
      <tr><td>FIS-210</td><td>Termodinámica</td><td>4 créditos</td></tr>
    </table></body></html>
    """
    frags, rows = _classify_html(html, "https://uni.edu/fisica/plan-de-estudios")
    assert frags, "HTML should yield fragments"
    qcore = [r for r in rows if r["classification"] == "qise_core"]
    assert qcore, f"expected a qise_core row, got {[r['classification'] for r in rows]}"
    r = qcore[0]
    assert "Computación Cuántica" in (r["course_title"] + r["evidence_snippet"])
    assert r["source_url"] == "https://uni.edu/fisica/plan-de-estudios"
    assert r["evidence_snippet"], "every candidate must carry an evidence snippet"


# ── SCENARIO 2 + 3: PDF discovery & non-.pdf detection ────────────────────────

def _test_crawler():
    return crawler.WebCrawler({
        "scraper": {"request_delay_sec": 0, "request_timeout_sec": 5,
                    "max_retries": 0, "max_depth": 1, "max_pages_per_university": 5},
        "output": {"raw_dir": "/tmp/qise_test_raw"},
    })


def test_pdf_link_discovery_and_detection():
    # (2) A page links to a PDF curriculum under a /download/ path (which the old
    #     junk filter used to drop). Discovery must still find it.
    html = """
    <html><body>
      <a href="/media/malla-curricular-fisica.pdf">Descargar malla curricular</a>
      <a href="/noticias/evento">Noticias</a>
      <a href="/download/plan?id=42">Descargar plan de estudios</a>
    </body></html>
    """
    wc = _test_crawler()
    _, pdf_links = wc._extract_links(html, "https://uni.edu/fisica", "uni.edu")
    assert any("malla-curricular-fisica.pdf" in u for u in pdf_links), pdf_links
    assert any("/download/plan" in u for u in pdf_links), pdf_links
    assert not any("noticias" in u for u in pdf_links)

    # (3) A PDF served at an extensionless URL with a generic content-type is
    #     still detected via the %PDF magic header.
    assert wc._is_pdf("https://uni.edu/file/serve?id=9",
                      "application/octet-stream", b"%PDF-1.6\n...") is True
    assert wc._is_pdf("https://uni.edu/page", "text/html", b"<html>") is False
    assert wc._is_pdf("https://uni.edu/x/plan.pdf", "", b"garbage") is True


def test_link_prioritization():
    """STEM curricula first, then other curricula, then course pages; news is
    queued LOW, not dropped."""
    html = """
    <html><body>
      <a href="/carreras/fisica/plan-de-estudios">Plan de estudios</a>
      <a href="/carreras/sociologia/plan-de-estudios">Plan de estudios</a>
      <a href="/facultad/ciencias">Facultad de Ciencias</a>
      <a href="/noticias/evento-cuantico">Noticias</a>
      <a href="/nosotros/historia">Historia</a>
      <a href="/login">Intranet</a>
    </body></html>
    """
    wc = _test_crawler()
    scored_links, _ = wc._extract_links(html, "https://uni.edu/", "uni.edu")
    prio = {u: p for p, u in scored_links}
    # STEM curriculum beats non-STEM curriculum (budget goes to física first)
    assert prio["https://uni.edu/carreras/fisica/plan-de-estudios"] == 2, prio
    assert prio["https://uni.edu/carreras/sociologia/plan-de-estudios"] == 3, prio
    # STEM-named program/faculty pages rank with non-STEM curricula
    assert prio["https://uni.edu/facultad/ciencias"] == 3, prio
    # news: kept (contextual evidence) but at LOW priority — never discarded
    assert prio["https://uni.edu/noticias/evento-cuantico"] == 5, prio
    assert prio["https://uni.edu/nosotros/historia"] == 6, prio
    # hard junk (login) is not queued at all
    assert "https://uni.edu/login" not in prio, prio


# ── SCENARIO 3 (cont.): extract text from a real PDF ──────────────────────────

def test_pdf_text_extraction():
    pdf = _make_pdf("Programa del curso\nComputación Cuántica\n"
                    "Créditos: 4  Prerrequisito: Álgebra Lineal\n"
                    "Contenido: qubits, algoritmos cuánticos, criptografía cuántica.")
    frags = extractor.extract_from_pdf(pdf, "https://uni.edu/serve?doc=7", UNIV,
                                       found_on_page="https://uni.edu/fisica")
    assert frags and frags[0]["extraction_status"] == "extracted"
    assert frags[0]["media_type"] == "pdf"
    assert frags[0]["pdf_page"] == 1
    assert frags[0]["found_on_page"] == "https://uni.edu/fisica"
    rows = CLF.classify(frags[0])
    assert any(r["classification"] == "qise_core" for r in rows), \
        [r["classification"] for r in rows]
    assert all(r["pdf_url"] == "https://uni.edu/serve?doc=7" for r in rows)


# ── SCENARIO 4: PDF extraction fails -> needs_manual_review ────────────────────

def test_pdf_extraction_failure_is_flagged():
    # (a) valid PDF, no text -> scanned/image -> needs_manual_review
    frags = extractor.extract_from_pdf(_make_blank_pdf(), "https://uni.edu/scan.pdf", UNIV)
    assert len(frags) == 1
    assert frags[0]["extraction_status"] == "needs_manual_review", frags[0]
    # (b) unreadable bytes -> failed_pdf_extraction (still NOT discarded)
    frags2 = extractor.extract_from_pdf(b"%PDF-1.4 totally-corrupt-not-a-pdf",
                                        "https://uni.edu/broken.pdf", UNIV)
    assert len(frags2) == 1
    assert frags2[0]["extraction_status"] in ("failed_pdf_extraction",
                                              "needs_manual_review"), frags2[0]
    # both must still produce an auditable candidate row
    rows = CLF.classify(frags[0]) + CLF.classify(frags2[0])
    assert all(r["extraction_status"] != "extracted" for r in rows)
    assert all(r["evidence_snippet"] for r in rows)


# ── SCENARIO 5: "Quantum Computing" -> qise_core ──────────────────────────────

def test_quantum_computing_is_core():
    frag = {
        "media_type": "html", "source_url": "https://uni.edu/catalog",
        "source_type": "catalog", "title": "Quantum Computing",
        "raw_text": "Quantum Computing. 4 credits. Introduction to qubits and "
                    "quantum algorithms.", "university": "U", "country": "US",
        "country_code": "US", "language": "en", "extraction_status": "extracted",
        "found_on_page": "", "pdf_url": "", "pdf_page": None,
    }
    rows = CLF.classify(frag)
    cats = {r["semantic_category"]: r["classification"] for r in rows}
    assert cats.get("quantum_computing") == "qise_core", cats


# ── SCENARIO 6: "Mecánica Cuántica I" -> adjacent ─────────────────────────────

def test_mecanica_cuantica_is_adjacent():
    frag = {
        "media_type": "html", "source_url": "https://uni.edu/plan-de-estudios",
        "source_type": "curriculum_grid", "title": "Mecánica Cuántica I",
        "raw_text": "Mecánica Cuántica I | 6 créditos | Prerrequisito: Física III",
        "university": "U", "country": "MX", "country_code": "MX", "language": "es",
        "extraction_status": "extracted", "found_on_page": "", "pdf_url": "",
        "pdf_page": None,
    }
    rows = CLF.classify(frag)
    cats = {r["semantic_category"]: r["classification"] for r in rows}
    assert cats.get("quantum_mechanics") == "quantum_foundations_or_adjacent", cats
    # and it must NOT be labelled qise_core
    assert "qise_core" not in _classifications(rows), cats


# ── SCENARIO 7: news/seminar page -> non_course_or_contextual ─────────────────

def test_news_seminar_is_contextual():
    html = """
    <html><head><title>Noticias</title></head><body>
    <p>El grupo de investigación organizó un seminario sobre computación
    cuántica. La conferencia contó con expositores internacionales y fue parte
    de la agenda de divulgación del laboratorio.</p></body></html>
    """
    _, rows = _classify_html(html, "https://uni.edu/noticias/seminario-cuantico")
    assert rows, "seminar page mentioning quantum should still be captured"
    assert all(r["classification"] == "non_course_or_contextual" for r in rows), \
        [r["classification"] for r in rows]


# ── multiple same-category courses on one document (PUCP regression) ─────────

def test_multiple_courses_same_category():
    """A curriculum listing Mecánica Cuántica 1, 2 AND Relativista must yield
    one row per course, not one per semantic category."""
    frag = {
        "media_type": "pdf", "source_url": "https://uni.edu/plan.pdf",
        "source_type": "curriculum_grid", "title": "UNIVERSIDAD X",
        "raw_text": ("Mecánica Cuántica 1  4 créditos\n"
                     "Mecánica Cuántica 2  4 créditos\n"
                     "Mecánica Cuántica Relativista  4 créditos\n"
                     # prerequisite REFERENCE — must not become a 4th row
                     "Física Térmica  4 créditos  Prerrequisito: Mecánica Cuántica 1\n"),
        "university": "U", "country": "PE", "country_code": "PE", "language": "es",
        "extraction_status": "extracted", "found_on_page": "", "pdf_url":
        "https://uni.edu/plan.pdf", "pdf_page": 1,
    }
    rows = [r for r in CLF.classify(frag)
            if r["semantic_category"] == "quantum_mechanics"]
    titles = sorted(r["course_title"] for r in rows)
    assert len(rows) == 3, titles
    assert titles == ["Mecánica Cuántica 1 4 créditos",
                      "Mecánica Cuántica 2 4 créditos",
                      "Mecánica Cuántica Relativista 4 créditos"], titles
    # and the letterhead-style doc title must NOT be used as course_title
    assert all("UNIVERSIDAD X" not in r["course_title"] for r in rows)
    # pipeline dedupe must keep all three (course_title is part of the key)
    prows = [Pipeline._to_row(c, "2026-01-01T00:00:00Z") for c in rows]
    merged = Pipeline._merge([], prows + prows)
    assert len(merged) == 3, [r["course_title"] for r in merged]


# ── research blurb on a curriculum page -> non_course (PUCP regression) ──────

def test_research_sentence_is_not_a_course():
    frag = {
        "media_type": "html",
        "source_url": "https://uni.edu/plan-de-estudios?especialidad=fisica",
        "source_type": "curriculum_grid", "title": "Plan de estudios",
        "raw_text": ("En la especialidad de Física, se investiga, teórica o "
                     "experimentalmente, en temas de altas energías, ciencias "
                     "de materiales, óptica cuántica y dinámica de fluidos.\n"
                     "ver plan de estudios"),
        "university": "U", "country": "PE", "country_code": "PE", "language": "es",
        "extraction_status": "extracted", "found_on_page": "", "pdf_url": "",
        "pdf_page": None,
    }
    rows = CLF.classify(frag)
    assert rows, "the mention must still be captured as evidence"
    assert all(r["classification"] == "non_course_or_contextual" for r in rows), \
        [(r["semantic_category"], r["classification"]) for r in rows]


# ── seed discovery (Stage 1) ──────────────────────────────────────────────────

def test_seed_scoring():
    import seed_discovery as sd
    hi, hi_terms, hi_reason = sd.score_candidate(
        "https://uni.edu.pe/facultad/fisica/plan-de-estudios.pdf",
        anchor="Descargar plan de estudios", in_sitemap=True)
    lo, _, _ = sd.score_candidate("https://uni.edu.pe/noticias/evento-deportivo")
    mid, _, _ = sd.score_candidate("https://uni.edu.pe/facultad/derecho")
    assert hi > mid > lo, (hi, mid, lo)
    assert lo < 0, lo                       # news+sports sinks below the cutoff
    assert "plan de estudios" in hi_terms and "fisica" in hi_terms, hi_terms
    assert "pdf" in hi_reason and "sitemap" in hi_reason, hi_reason
    # academic + STEM combo must outrank academic-only
    both, _, _ = sd.score_candidate("https://uni.edu.pe/cursos/fisica")
    acad, _, _ = sd.score_candidate("https://uni.edu.pe/cursos/verano")
    assert both > acad, (both, acad)


def test_sitemap_and_robots_parsing():
    import seed_discovery as sd
    robots = "User-agent: *\nDisallow: /admin\nSitemap: https://u.edu/sm.xml\n"
    assert sd.parse_robots_sitemaps(robots) == ["https://u.edu/sm.xml"]
    xml = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://u.edu/carreras/fisica</loc></url>
      <url><loc> https://u.edu/sitemap-posts.xml </loc></url>
      <url><loc>https://u.edu/cursos</loc></url>
    </urlset>"""
    urls, children = sd.parse_sitemap(xml)
    assert urls == ["https://u.edu/carreras/fisica", "https://u.edu/cursos"], urls
    assert children == ["https://u.edu/sitemap-posts.xml"], children


def test_seed_origin_in_rows():
    assert "seed_origin" in OUTPUT_FIELDS
    uni = {"name": "U", "country": "PE", "country_code": "PE", "language": "es",
           "seed_origin": "auto_discovered"}
    frags = extractor.extract_from_html(
        "<html><body><table><tr><th>c</th><th>n</th></tr>"
        "<tr><td>FIS-1</td><td>Computación Cuántica, 4 créditos</td></tr>"
        "</table></body></html>",
        "https://uni.edu/plan", uni)
    assert frags and frags[0]["seed_origin"] == "auto_discovered"
    rows = [Pipeline._to_row(c, "t") for f in frags for c in CLF.classify(f)]
    assert rows and all(r["seed_origin"] == "auto_discovered" for r in rows)
    assert all(set(r.keys()) == set(OUTPUT_FIELDS) for r in rows)


def test_input_loader_manual_flag():
    import tempfile, textwrap
    csv_text = textwrap.dedent("""\
        institution,country,website,seed_urls
        Con Seeds,Peru,https://a.edu.pe,https://a.edu.pe/fisica/malla
        Solo Dominio,Peru,https://b.edu.pe,
    """)
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                     encoding="utf-8") as f:
        f.write(csv_text)
        path = f.name
    try:
        unis = input_loader.load_universities(path)
        by_name = {u["name"]: u for u in unis}
        assert by_name["Con Seeds"]["has_manual_seeds"] is True
        assert by_name["Solo Dominio"]["has_manual_seeds"] is False
        # domain-only institutions are still valid input (homepage as seed)
        assert by_name["Solo Dominio"]["catalog_urls"] == ["https://b.edu.pe/"]
    finally:
        os.unlink(path)


# ── catalog-PDF dedupe + print-view URLs + prose titles (UNI regressions) ─────

def test_catalog_duplicates_and_titles():
    from utils import normalize_url, guess_course_title

    # (a) Joomla print-view params must normalize away → one page, one crawl
    assert normalize_url("https://u.edu/x?tmpl=component&print=1") == \
           normalize_url("https://u.edu/x")

    # (b) a match inside syllabus prose gets the course heading above it
    text = ("MF604 Mecánica Estadística Cuántica\n"
            "Fundamentos de la mecánica estadística cuántica. Matriz, "
            "densidad y sus aplicaciones a sistemas cuánticos.\n")
    i = text.index("mecánica estadística", 40)
    assert guess_course_title(text, i, i + 20) == \
           "MF604 Mecánica Estadística Cuántica"

    # (c) catalog listing + syllabus header of the SAME course collapse:
    #     the course-code prefix is not part of the dedupe identity
    base = {"source_url": "https://u.edu/catalogo.pdf",
            "semantic_category": "quantum_mechanics", "confidence": "medium"}
    merged = Pipeline._merge(
        [], [dict(base, course_title="Simetrías discretas en mecánica cuántica"),
             dict(base, course_title="MF719 Simetrías discretas en mecánica cuántica")])
    assert len(merged) == 1, [r["course_title"] for r in merged]
    # …but genuinely different courses never collapse
    merged2 = Pipeline._merge(
        [], [dict(base, course_title="MF603 Mecánica cuántica"),
             dict(base, course_title="MF719 Simetrías discretas en mecánica cuántica")])
    assert len(merged2) == 2


# ── registrable domain + external documents + Excel + level (UNI follow-ups) ──

def test_registered_domain_scope():
    from utils import registered_domain, same_registered_domain
    assert registered_domain("portal.uni.edu.pe") == "uni.edu.pe"
    assert registered_domain("fc.uni.edu.pe") == "uni.edu.pe"
    assert registered_domain("www5.usp.br") == "usp.br"
    assert registered_domain("utec.edu.pe") == "utec.edu.pe"
    assert registered_domain("ib.edu.ar") == "ib.edu.ar"
    # the UNI case: faculty subdomain is in scope when the base is the portal
    assert same_registered_domain("https://fc.uni.edu.pe/pregrado/fisica/",
                                  registered_domain("portal.uni.edu.pe"))
    # …but other institutions under edu.pe are NOT
    assert not same_registered_domain("https://www.pucp.edu.pe/x",
                                      registered_domain("portal.uni.edu.pe"))


def test_external_doc_download_urls():
    from utils import external_doc_download_url as x
    assert x("https://drive.google.com/file/d/1izo5xP9ak-mm8pFB4fGSnSOS0bSUK3Da/preview") \
        == "https://drive.google.com/uc?export=download&id=1izo5xP9ak-mm8pFB4fGSnSOS0bSUK3Da"
    assert x("https://docs.google.com/spreadsheets/d/ABC_12/edit#gid=0") \
        == "https://docs.google.com/spreadsheets/d/ABC_12/export?format=xlsx"
    sp = x("https://uniedupe93141-my.sharepoint.com/:x:/g/personal/escuelas_fc2_uni_edu_pe/IQCik?e=mWw49W")
    assert sp.endswith("&download=1"), sp
    assert x("https://uni.edu.pe/normal/page") == ""


def test_embedded_documents_are_discovered():
    html = """
    <html><body>
      <iframe src="https://drive.google.com/file/d/FILE_ID_1/preview"></iframe>
      <a href="https://uniedupe-my.sharepoint.com/:x:/g/personal/x/PLAN?e=1">Plan de estudios (excel)</a>
      <embed src="/documentos/malla-fisica.xlsx">
      <iframe src="https://www.youtube.com/embed/abc"></iframe>
    </body></html>
    """
    wc = _test_crawler()
    _, doc_links = wc._extract_links(html, "https://uni.edu.pe/fisica", "uni.edu.pe")
    assert any("uc?export=download&id=FILE_ID_1" in u for u in doc_links), doc_links
    assert any("download=1" in u and "sharepoint" in u for u in doc_links), doc_links
    assert any(u.endswith("malla-fisica.xlsx") for u in doc_links), doc_links
    assert not any("youtube" in u for u in doc_links), doc_links


def test_xlsx_extraction_and_classification():
    import io as _io
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Plan de Estudios"
    ws.append(["Código", "Asignatura", "Créditos"])
    ws.append(["FIS-401", "Mecánica Cuántica", "4 créditos"])
    ws.append(["FIS-501", "Computación Cuántica", "4 créditos"])
    buf = _io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()

    # detection by magic bytes at a URL without extension
    wc = _test_crawler()
    assert wc._is_spreadsheet("https://u.edu/serve?id=1", "application/octet-stream",
                              data) == "xlsx"

    frags = extractor.extract_from_xlsx(data, "https://u.edu/plan.xlsx", UNIV,
                                        found_on_page="https://u.edu/fisica")
    assert frags and frags[0]["extraction_status"] == "extracted"
    assert frags[0]["media_type"] == "xlsx"
    rows = [r for f in frags for r in CLF.classify(f)]
    cats = {r["semantic_category"]: r["classification"] for r in rows}
    assert cats.get("quantum_computing") == "qise_core", cats
    assert cats.get("quantum_mechanics") == "quantum_foundations_or_adjacent", cats

    # corrupt bytes are flagged, never dropped
    bad = extractor.extract_from_xlsx(b"PK\x03\x04 corrupt", "https://u.edu/x.xlsx", UNIV)
    assert len(bad) == 1 and bad[0]["extraction_status"] == "needs_manual_review"


def test_academic_level():
    from keywords import detect_academic_level
    assert detect_academic_level("plan de estudios de pregrado") == "undergraduate"
    assert detect_academic_level("escuela de posgrado maestria en fisica") == "graduate"
    assert detect_academic_level("pregrado y posgrado") == ""  # conflict → unknown

    assert "academic_level" in OUTPUT_FIELDS

    def frag(**kw):
        base = {"media_type": "pdf", "source_type": "curriculum_grid",
                "title": "", "university": "U", "country": "PE",
                "country_code": "PE", "language": "es",
                "extraction_status": "extracted", "found_on_page": "",
                "pdf_url": "", "pdf_page": 1, "source_url": "https://u.edu/x.pdf",
                "raw_text": "Mecánica Cuántica I  4 créditos"}
        base.update(kw)
        return base

    # 1) URL signal wins
    r = CLF.classify(frag(source_url="https://u.edu/pregrado/fisica/plan.pdf"))[0]
    assert r["academic_level"] == "undergraduate", r["academic_level"]
    # 2) local-context signal
    r = CLF.classify(frag(raw_text="Maestría en Física\nMecánica Cuántica Avanzada 4 créditos"))[0]
    assert r["academic_level"] == "graduate", r["academic_level"]
    # 3) doc-level hint as fallback
    r = CLF.classify(frag(academic_level_hint="graduate"))[0]
    assert r["academic_level"] == "graduate"
    # 4) nothing → unknown in the output row
    r = CLF.classify(frag())[0]
    assert r["academic_level"] == ""
    assert Pipeline._to_row(r, "t")["academic_level"] == "unknown"


# ── input loader + pipeline schema ────────────────────────────────────────────

def test_input_loader_csv():
    # Self-contained: write a temp CSV so the test does not depend on whatever
    # the user currently has in data/universities_sample.csv.
    import tempfile, textwrap
    csv_text = textwrap.dedent("""\
        institution,country,country_code,website,seed_urls,max_depth,max_pages
        Instituto Balseiro,Argentina,AR,https://www.ib.edu.ar,https://www.ib.edu.ar/carreras | https://www.ib.edu.ar/fisica,1,8
        USP,Brazil,,https://www5.usp.br,https://www5.usp.br/fisica,2,10
    """)
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                     encoding="utf-8") as f:
        f.write(csv_text)
        path = f.name
    try:
        unis = input_loader.load_universities(path)
        assert len(unis) == 2, len(unis)
        u0 = unis[0]
        assert u0["name"] == "Instituto Balseiro" and u0["country_code"] == "AR"
        assert u0["catalog_urls"] and all(s.startswith("http") for s in u0["catalog_urls"])
        # country_code is inferred from the country name when the column is blank
        assert unis[1]["country_code"] == "BR", unis[1]["country_code"]
    finally:
        os.unlink(path)


def test_pipeline_row_schema_and_dedupe():
    frag = {
        "media_type": "pdf", "source_url": "https://uni.edu/x", "source_type": "syllabus",
        "title": "Quantum Information", "raw_text": "Quantum information. qubits. "
        "4 créditos. sílabo.", "university": "U", "country": "PE", "country_code": "PE",
        "language": "en", "extraction_status": "extracted", "found_on_page": "",
        "pdf_url": "https://uni.edu/x", "pdf_page": 2,
    }
    cands = CLF.classify(frag)
    rows = [Pipeline._to_row(c, "2026-01-01T00:00:00Z") for c in cands]
    assert rows
    for r in rows:
        assert set(r.keys()) == set(OUTPUT_FIELDS), set(OUTPUT_FIELDS) ^ set(r.keys())
        assert r["pdf_page"] == 2 and r["pdf_url"] == "https://uni.edu/x"
    # dedupe keeps one row per (source_url, semantic_category)
    merged = Pipeline._merge([], rows + rows)
    keys = {(r["source_url"], r["semantic_category"]) for r in merged}
    assert len(merged) == len(keys)


# ── runner ────────────────────────────────────────────────────────────────────

def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failures += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
