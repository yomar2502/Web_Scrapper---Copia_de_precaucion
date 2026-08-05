"""
crawler.py — Crawlers especializados por tipo de fuente.

WebCrawler (portales universitarios):
  - BFS por dominio con max_depth / max_pages configurables por universidad.
  - Descubrimiento sensible a PDFs: los links a PDF NO se descartan por el
    filtro de "basura"; se detectan por sufijo .pdf, por Content-Type y por
    la cabecera de archivo %PDF (aunque la URL no termine en .pdf).
  - Registra la página donde se encontró cada PDF (found_on_page).
  - Respeta robots.txt (configurable, fail-open) y cachea descargas en disco
    para que reejecuciones no vuelvan a golpear los servidores.
  - User-Agent académico identificable.
"""

import heapq
import itertools
import re
import json
from pathlib import Path
from urllib.parse import urlparse
from typing import Generator

import requests

from keywords import (
    ACADEMIC_SEED_TERMS, STEM_TERMS, LOW_PRIORITY_TERMS, match_terms,
)
from utils import (
    RateLimiter, RobotsCache, get_logger, slugify,
    normalize_url, same_registered_domain, registered_domain, url_hash,
    looks_like_pdf_url, external_doc_download_url,
)

logger = get_logger("crawler")

# Identify ourselves honestly as an academic research crawler.
ACADEMIC_UA = (
    "QISE-LatAm-Research-Bot/2.0 (academic research on quantum education; "
    "+contact via project README)"
)


def _make_session(user_agent: str, timeout: int, max_retries: int) -> requests.Session:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent or ACADEMIC_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "application/pdf;q=0.9,*/*;q=0.8",
        "Accept-Language": "es,pt,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    })
    retry = Retry(
        total=max_retries,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class WebCrawler:
    """
    BFS crawler for university portals with PDF-sensitive discovery.

    Discovery rules:
      * Follow same-domain links whose URL/anchor looks like a course/program
        page (COURSE_URL_PATTERNS), up to max_depth.
      * ALWAYS collect PDF-looking links regardless of the junk filter — a PDF
        under /media/, /download/ or a query-string endpoint is exactly what we
        want. PDFs are fetched even at max_depth (they are documents, not
        further crawl surface) but still count against max_pages.
      * A response is a PDF if the Content-Type says so, the URL looks like a
        PDF, OR the first bytes are the %PDF magic header.
    """

    COURSE_URL_PATTERNS = re.compile(
        r"(curso|course|syllabus|silabo|s[íi]labo"
        r"|asignatura|disciplina|programa|materia|pensum|ementa"
        r"|catalogo|catalog|oferta.?academ|curricul|malla|grade.?curricular"
        r"|matriz.?curricular|plano.?de.?ensino"
        r"|posgrado|postgrado|graduate|undergraduate|graduacao"
        r"|licenciatura|maestr[íi]a|maestria|mestrado|doutorado|doctorado"
        r"|especializacion|especializaci[oó]n|carrera"
        r"|fisica|f[íi]sica|ingenieria|ingenier[íi]a|ciencias|computac"
        r"|pregrado|plan.?de.?estudio)",
        re.IGNORECASE,
    )

    # Anchor text hints (checked on link TEXT, not URL) — catches "descargar malla".
    COURSE_ANCHOR_PATTERNS = re.compile(
        r"(malla|plan de estudio|pensum|s[íi]labo|syllabus|programa|curr[íi]cul"
        r"|grade curricular|matriz curricular|ementa|asignatura|disciplina|curso)",
        re.IGNORECASE,
    )

    # URLs that very likely hold the actual curriculum. Crawled BEFORE generic
    # course pages so a small max_pages budget is spent where the evidence is —
    # a portal homepage can emit 80+ plausible links and starve the crawl.
    CURRICULUM_PRIORITY_PATTERNS = re.compile(
        r"(plan.?de.?estudio|malla|pensum|curricul|grade.?curricular"
        r"|matriz.?curricular|plano.?de.?ensino|silabo|s[íi]labo|syllabus"
        r"|(?<![a-z])ementa|oferta.?academ|catalogo|catalog)",  # ementa needs a
        re.IGNORECASE,  # boundary: it is a substring of "implementar"
    )

    # Junk to NEVER queue: binary assets, auth/admin, per-user pages. Anything
    # merely off-topic (news, events, admissions…) is queued at LOW priority
    # instead — it may still hold contextual quantum evidence.
    HARD_SKIP_PATTERNS = re.compile(
        r"\.(jpg|jpeg|png|gif|svg|ico|mp4|mp3|zip|rar|exe|js|css"
        r"|woff|woff2|ttf|eot)(\?.*)?$"
        r"|/(login|logout|admin|wp-admin|wp-login|search|tag|autor|author"
        r"|comment|registro|register|password|reset|cart|shop"
        r"|contact|contacto|sitemap|privacidad|privacy|terminos|terms"
        r"|rss|atom|newsletter|suscri)",
        re.IGNORECASE,
    )

    # Crawled LAST (priority 4): news/events/admissions/people/etc. Not junk —
    # contextual quantum evidence lives here — but they must not eat the page
    # budget before curricula do.
    LOW_PRIORITY_URL_PATTERNS = re.compile(
        r"/(noticias?|news|blog|boletin|eventos?|events?|agenda|calendario"
        r"|prensa|press|comunicado|galeria|gallery"
        r"|admision(es)?|admissions?|vestibular"
        r"|alumni|egresados|exalumnos|deportes?|sports"
        r"|profesor|docentes?|staff|equipo|team|investigador|researcher"
        r"|transparencia|licitacion|administrativ)",
        re.IGNORECASE,
    )

    def __init__(self, cfg: dict):
        sc = cfg["scraper"]
        self.delay = sc["request_delay_sec"]
        self.timeout = sc["request_timeout_sec"]
        self.max_retries = sc["max_retries"]
        self.global_max_depth = sc["max_depth"]
        self.global_max_pages = sc["max_pages_per_university"]
        self.download_pdfs = sc.get("download_pdfs", True)
        self.use_cache = sc.get("use_cache", True)
        self.max_pdf_bytes = int(sc.get("max_pdf_mb", 40)) * 1024 * 1024
        self.max_pdfs_per_domain = int(sc.get("max_pdfs_per_domain", 50))
        # Fetch documents embedded from Google Drive / SharePoint (documents
        # only — those hosts are never crawled as pages).
        self.fetch_external_docs = sc.get("fetch_external_docs", True)
        self.user_agent = sc.get("user_agent") or ACADEMIC_UA
        # Per-institution crawl statistics, read by the pipeline for the run
        # summary: {institution: {pages_crawled, html_pages, pdfs_detected}}
        self.stats: dict[str, dict] = {}
        # Subdomains seen during the current crawl (reset per institution).
        self._seen_hosts: set[str] = set()

        self.raw_dir = Path(cfg["output"]["raw_dir"])
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.limiter = RateLimiter(min_delay=self.delay)
        self.session = _make_session(self.user_agent, self.timeout, self.max_retries)
        self.robots = RobotsCache(
            self.user_agent, enabled=sc.get("respect_robots", True), logger=logger
        )

    # ── PUBLIC ────────────────────────────────────────────────────────────────

    def crawl_university(self, university: dict) -> Generator[dict, None, None]:
        base = university.get("base_url") or (university.get("catalog_urls") or [""])[0]
        # Scope the crawl to the institution's REGISTRABLE domain so sibling
        # subdomains stay in bounds: PUCP links www.pucp.edu.pe →
        # facultad-ciencias-ingenieria.pucp.edu.pe, UNI links portal.uni.edu.pe
        # → fc.uni.edu.pe. Curricula usually live on faculty subdomains.
        allowed_domain = registered_domain(urlparse(base).netloc)
        seeds = [normalize_url(u) for u in university.get("catalog_urls", []) if u]

        # Explicit None checks: the input loader stores max_pages/max_depth as
        # None when the CSV has no such column, so dict.get(key, default)
        # would return None instead of falling back to the global config.
        max_pages = university.get("max_pages")
        max_pages = self.global_max_pages if max_pages is None else max_pages
        max_depth = university.get("max_depth")
        max_depth = self.global_max_depth if max_depth is None else max_depth
        max_pdfs = university.get("max_pdfs")
        max_pdfs = self.max_pdfs_per_domain if max_pdfs is None else max_pdfs

        st = {"pages_crawled": 0, "html_pages": 0, "pdfs_detected": 0}
        self.stats[university.get("name", "?")] = st

        visited: set[str] = set()
        # Priority queue: (priority, seq, url, depth, found_on_page).
        # 1 = PDF/document, 2 = STEM curriculum page, 3 = other curriculum,
        # 4 = course/STEM/department page, 5 = news/events/admissions/people,
        # 6 = generic. Low-priority pages are queued, not discarded — they may
        # hold contextual quantum evidence — but only get budget after the
        # academic pages. `seq` keeps insertion (BFS) order within a priority.
        #
        # Seeds are NOT all fetched first: they enter the same ranking, one
        # level above what their URL alone would get. Otherwise 20 discovered
        # seeds eat a small page budget before the crawl ever descends into a
        # plan de estudios found on the very first seed.
        seq = itertools.count()
        queue: list[tuple[int, int, str, int, str]] = []
        for u in seeds:
            if looks_like_pdf_url(u):
                prio = 1
            else:
                prio = max(1, self._link_priority(
                    u, urlparse(u).path.lower(), "") - 1)
            queue.append((prio, next(seq), u, 0, ""))
        heapq.heapify(queue)
        pdf_cap_warned = False
        # Reset per-crawl subdomain tracking (see _extract_links: the first
        # link into each unseen faculty subdomain gets elevated priority).
        self._seen_hosts = {urlparse(u).netloc for u in seeds}

        logger.info(
            f"Crawl: {university.get('name','?')} | seeds={len(seeds)} "
            f"max_pages={max_pages} max_depth={max_depth} max_pdfs={max_pdfs} "
            f"pdfs={'on' if self.download_pdfs else 'off'} "
            f"seed_origin={university.get('seed_origin', 'manual')}"
        )

        while queue and st["pages_crawled"] < max_pages:
            _prio, _, url, depth, found_on = heapq.heappop(queue)
            url = normalize_url(url)
            if not url or url in visited:
                continue
            visited.add(url)

            if not self.robots.allowed(url):
                logger.info(f"  robots.txt disallows, skipping: {url}")
                continue

            result = self._fetch(url, university)
            if result is None:
                continue
            st["pages_crawled"] += 1

            doc_kind = "pdf" if result["is_pdf"] else result.get("doc_kind", "")
            if doc_kind:
                st["pdfs_detected"] += 1
                if st["pdfs_detected"] > max_pdfs:
                    if not pdf_cap_warned:
                        logger.warning(f"  max_pdfs_per_domain ({max_pdfs}) "
                                       f"reached — further documents skipped")
                        pdf_cap_warned = True
                    continue
                logger.debug(f"  {doc_kind.upper()}: {url} "
                             f"(found on: {found_on or 'seed'})")
                yield {"type": doc_kind, "content": result["content"],
                       "url": result["final_url"], "found_on": found_on,
                       "university": university}
                continue

            if not result["is_html"]:
                logger.debug(f"  Skipping non-HTML/PDF ({result['content_type']}): {url}")
                continue

            html = result["text"]
            st["html_pages"] += 1
            logger.debug(f"  HTML ({len(html):,}b) depth={depth}: {url}")
            yield {"type": "html", "content": html,
                   "url": result["final_url"], "found_on": found_on,
                   "university": university}

            scored_links, pdf_links = self._extract_links(html, url, allowed_domain)

            # PDFs: fetch even at max_depth (documents, not crawl surface),
            # but stop queueing once the per-domain PDF cap is reached.
            if self.download_pdfs and st["pdfs_detected"] < max_pdfs:
                for link in pdf_links:
                    if link not in visited:
                        heapq.heappush(queue, (1, next(seq), link, depth + 1, url))

            # HTML pages: only descend while there is depth budget. The
            # priority (2..6) was computed from URL + anchor text, with the
            # first link into each new subdomain elevated (see _extract_links).
            # Depth is PER SITE: crossing into another subdomain restarts it —
            # a faculty site reached at depth 2 is a whole new tree, and
            # max_pages (not depth) is the real global cap.
            if depth < max_depth:
                page_host = urlparse(url).netloc
                for prio, link in scored_links:
                    if link not in visited:
                        link_depth = (0 if urlparse(link).netloc != page_host
                                      else depth + 1)
                        heapq.heappush(queue,
                                       (prio, next(seq), link, link_depth, url))

        logger.info(
            f"  Done: {university.get('name','?')} | "
            f"{st['pages_crawled']} pages fetched "
            f"({st['html_pages']} HTML, {st['pdfs_detected']} PDFs)"
        )

    # ── FETCH (with on-disk cache) ────────────────────────────────────────────

    def _fetch(self, url: str, university: dict) -> dict | None:
        """
        Return a normalized dict, or None on failure:
          {content: bytes, text: str, content_type: str, final_url: str,
           is_pdf: bool, is_html: bool}
        Uses the disk cache when enabled so re-runs don't re-hit servers.
        """
        cached = self._load_cache(url, university)
        if cached is not None:
            return cached

        self.limiter.wait()
        try:
            # (connect, read) timeout: dead hosts abort in seconds instead of
            # holding the whole read timeout through every retry — university
            # sites are full of stale links and each one costs wall-clock time.
            resp = self.session.get(url, timeout=(6, self.timeout),
                                    allow_redirects=True, stream=True)
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "").lower()

            content = b""
            for chunk in resp.iter_content(65536):
                content += chunk
                if len(content) > self.max_pdf_bytes:
                    logger.warning(f"  Truncated oversized download (> "
                                   f"{self.max_pdf_bytes // (1024*1024)}MB): {url}")
                    break
            final_url = normalize_url(resp.url) or url
            resp.close()
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            logger.warning(f"  HTTP {code}: {url}")
            return None
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            # Old links on university pages often say http:// while the server
            # only answers on https (PUCP's facultad links, for example).
            # Browsers upgrade silently; do the same, once.
            if url.startswith("http://"):
                https_url = "https://" + url[len("http://"):]
                logger.info(f"  {type(e).__name__} on http, retrying https: {https_url}")
                return self._fetch(https_url, university)
            logger.warning(f"  {type(e).__name__}: {url}")
            return None
        except Exception as e:
            logger.warning(f"  Error: {url} → {e}")
            return None

        is_pdf = self._is_pdf(url, content_type, content)
        doc_kind = "" if is_pdf else self._is_spreadsheet(url, content_type, content)
        is_html = (not is_pdf) and (not doc_kind) and (
            "html" in content_type or content_type == "" or "xml" in content_type)
        result = self._normalize_result(content, content_type, final_url,
                                        is_pdf, is_html, doc_kind)
        self._save_cache(url, university, result)
        return result

    @staticmethod
    def _is_pdf(url: str, content_type: str, content: bytes) -> bool:
        if "pdf" in content_type:
            return True
        if looks_like_pdf_url(url):
            return True
        # Magic header — handles PDFs served at extensionless URLs with a wrong
        # or generic Content-Type (application/octet-stream, etc.).
        return content[:1024].lstrip()[:5] == b"%PDF-"

    @staticmethod
    def _is_spreadsheet(url: str, content_type: str, content: bytes) -> str:
        """'xlsx' / 'xls' / '' — planes de estudio are often published as Excel."""
        path = urlparse(url.lower()).path
        if "spreadsheetml" in content_type or path.endswith(".xlsx"):
            return "xlsx"
        if "vnd.ms-excel" in content_type or path.endswith(".xls"):
            return "xls"
        if content[:4] == b"PK\x03\x04":
            # OOXML is a zip; a workbook has entries under xl/. Reading the
            # central directory is cheap and exact (entry order varies by writer).
            import io as _io
            import zipfile
            try:
                with zipfile.ZipFile(_io.BytesIO(content)) as z:
                    if any(n.startswith("xl/") for n in z.namelist()[:80]):
                        return "xlsx"
            except Exception:
                pass
        if content[:4] == b"\xd0\xcf\x11\xe0":
            return "xls"   # legacy OLE compound file
        return ""

    @staticmethod
    def _normalize_result(content, content_type, final_url, is_pdf, is_html,
                          doc_kind="") -> dict:
        text = ""
        if is_html:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = content.decode("latin-1", errors="replace")
        return {"content": content, "text": text, "content_type": content_type,
                "final_url": final_url, "is_pdf": is_pdf, "is_html": is_html,
                "doc_kind": doc_kind}

    # ── DISK CACHE (also serves as the raw archive for auditing) ──────────────

    def _cache_paths(self, url: str, university: dict) -> tuple[Path, Path]:
        slug = slugify(university.get("name") or "misc")
        out_dir = self.raw_dir / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        h = url_hash(url)
        return out_dir / f"{h}.meta.json", out_dir / h

    def _load_cache(self, url: str, university: dict) -> dict | None:
        if not self.use_cache:
            return None
        meta_path, _ = self._cache_paths(url, university)
        if not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            body_path = Path(meta["body_path"])
            content = body_path.read_bytes()
        except Exception:
            return None
        return self._normalize_result(
            content, meta.get("content_type", ""), meta.get("final_url", url),
            meta.get("is_pdf", False), meta.get("is_html", False),
            meta.get("doc_kind", ""),
        )

    def _save_cache(self, url: str, university: dict, result: dict) -> None:
        if not self.use_cache:
            return
        meta_path, body_base = self._cache_paths(url, university)
        ext = ("pdf" if result["is_pdf"]
               else result.get("doc_kind")
               or ("html" if result["is_html"] else "bin"))
        body_path = body_base.with_suffix("." + ext)
        try:
            body_path.write_bytes(result["content"])
            meta_path.write_text(json.dumps({
                "url": url,
                "final_url": result["final_url"],
                "content_type": result["content_type"],
                "is_pdf": result["is_pdf"],
                "is_html": result["is_html"],
                "doc_kind": result.get("doc_kind", ""),
                "body_path": str(body_path),
            }, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.debug(f"cache write failed for {url}: {e}")

    # ── LINK EXTRACTION ───────────────────────────────────────────────────────

    def _extract_links(self, html, base_url,
                       allowed_domain) -> tuple[list[tuple[int, str]], list[str]]:
        """
        Return (scored_links, pdf_links) for one HTML page.
        scored_links = [(priority, url), ...] with priority 2 (curriculum),
        3 (course/STEM/department), 4 (news/events/people…), 5 (generic) —
        computed from BOTH the URL and the anchor text. PDF links bypass every
        filter and are returned separately (fetched terminally at priority 1).
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return [], []

        soup = BeautifulSoup(html, "html.parser")
        scored_links: list[tuple[int, str]] = []
        pdf_links: list[str] = []
        seen: set[str] = set()

        # <a href> for navigation; <iframe>/<embed>/<object> because LatAm
        # universities embed their planes de estudio as Drive previews or
        # SharePoint spreadsheets rather than linking them.
        for el in soup.find_all(["a", "iframe", "embed", "object"]):
            raw = (el.get("href") or el.get("src") or el.get("data") or "").strip()
            if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:",
                                          "about:", "data:")):
                continue

            # External document hosts (Google Drive / SharePoint): fetched as
            # documents, never crawled as pages — bypasses the domain rule.
            if self.fetch_external_docs and "://" in raw:
                ext_url = external_doc_download_url(raw)
                if ext_url:
                    if ext_url not in seen:
                        seen.add(ext_url)
                        pdf_links.append(ext_url)
                    continue

            full_url = normalize_url(raw, base=base_url)
            if not full_url or full_url in seen:
                continue
            if not same_registered_domain(full_url, allowed_domain):
                continue
            seen.add(full_url)

            anchor_text = el.get_text(" ", strip=True) if el.name == "a" else ""
            path_low = urlparse(full_url).path.lower()

            # Document candidates: fetched terminally (even at max_depth) and
            # NEVER dropped by the junk filter. Covers real .pdf/.xlsx links and
            # extensionless "descargar malla/plan" download endpoints that often
            # turn out to be PDFs (confirmed later via Content-Type/%PDF header).
            is_download = any(w in path_low for w in ("download", "descargar",
                                                      "documento", "archivo", "adjunto"))
            is_doc = (looks_like_pdf_url(full_url)
                      or path_low.endswith((".xlsx", ".xls"))
                      or (is_download
                          and self.COURSE_ANCHOR_PATTERNS.search(anchor_text)))
            if is_doc:
                pdf_links.append(full_url)
                continue

            if el.name != "a":
                continue  # embedded non-document frames are not crawl surface

            if self.HARD_SKIP_PATTERNS.search(path_low):
                continue

            prio = self._link_priority(full_url, path_low, anchor_text)

            # Faculty sites live on their own subdomains, often linked with
            # opaque URLs (fc.uni.edu.pe/fc/) that would score priority 6 and
            # never win budget. Elevate the FIRST link into each unseen
            # subdomain: tier 2 when the host/anchor reads academic ("Facultad
            # de Ciencias", ciencias.uni…) OR the host label is a short
            # acronym — LatAm faculties are fc./fiee./fim./if. — tier 3
            # otherwise, so junk hosts (bolsa de trabajo, CDNs) cannot
            # displace curricula.
            host = urlparse(full_url).netloc
            if host not in self._seen_hosts:
                self._seen_hosts.add(host)
                host_and_anchor = re.sub(r"[-.]", " ", host) + " " + anchor_text
                first_label = host.split(".")[0]
                if (match_terms(host_and_anchor, ACADEMIC_SEED_TERMS)
                        or match_terms(host_and_anchor, STEM_TERMS)
                        or re.fullmatch(r"[a-z]{2,4}", first_label)):
                    prio = min(prio, 2)
                else:
                    prio = min(prio, 3)

            scored_links.append((prio, full_url))

        return scored_links, pdf_links

    def _link_priority(self, url: str, path_low: str, anchor_text: str) -> int:
        """
        2 = STEM curriculum (plan de estudios/malla/… for física/ingeniería/…).
        3 = other curriculum page. A big university links the planes of ALL its
            carreras (sociología, comunicaciones, …); without the STEM split
            those can drain the page budget before the física plan is reached.
        4 = course / STEM / department / faculty page.
        5 = news, events, admissions, people, sports… (contextual evidence only).
        6 = generic institutional page (crawled last, never discarded).
        Curriculum outranks the low-priority check on purpose: a plan-de-estudios
        link is gold even when it sits under /noticias/.
        """
        path_text = re.sub(r"[-_/.+%]+", " ", path_low)
        if (self.CURRICULUM_PRIORITY_PATTERNS.search(url)
                or self.CURRICULUM_PRIORITY_PATTERNS.search(anchor_text)):
            stem = match_terms(path_text + " " + anchor_text, STEM_TERMS)
            return 2 if stem else 3
        if (self.LOW_PRIORITY_URL_PATTERNS.search(path_low)
                or match_terms(anchor_text, LOW_PRIORITY_TERMS)):
            return 5
        if (self.COURSE_URL_PATTERNS.search(url)
                or self.COURSE_ANCHOR_PATTERNS.search(anchor_text)
                or match_terms(path_text + " " + anchor_text, STEM_TERMS)):
            # STEM-named program pages (/pregrado/fisica, /carrera/fisica)
            # gate the plan pages — rank them with non-STEM curricula, above
            # generic course/faculty pages.
            if match_terms(path_text + " " + anchor_text, STEM_TERMS):
                return 3
            return 4
        return 6


# ── RSS CRAWLER ───────────────────────────────────────────────────────────────

class RSSCrawler:
    def __init__(self, cfg: dict):
        self.timeout = cfg["scraper"]["request_timeout_sec"]
        self.limiter = RateLimiter(min_delay=1.5)
        self.session = _make_session("", self.timeout, 2)

    def crawl_feed(self, source: dict) -> Generator[dict, None, None]:
        try:
            import feedparser
        except ImportError:
            logger.error("feedparser no instalado: pip install feedparser")
            return

        self.limiter.wait()
        logger.info(f"RSS: {source['name']}")
        try:
            resp = self.session.get(source["url"], timeout=self.timeout)
            feed = feedparser.parse(resp.text)
        except Exception as e:
            logger.warning(f"RSS error {source['name']}: {e}")
            return

        for entry in feed.entries:
            yield {"type": "rss", "content": entry,
                   "url": getattr(entry, "link", ""),
                   "university": None,
                   "rss_source_name": source["name"]}
        logger.info(f"  RSS {source['name']}: {len(feed.entries)} entradas")


# ── TWITTER CRAWLER ───────────────────────────────────────────────────────────

class TwitterCrawler:
    def __init__(self, cfg: dict, bearer_token: str | None = None):
        self.bearer_token = bearer_token
        self.timeout = cfg["scraper"]["request_timeout_sec"]
        self.limiter = RateLimiter(min_delay=3.0)
        self.session = _make_session("", self.timeout, 2)

    def crawl_queries(self, source: dict) -> Generator[dict, None, None]:
        if not self.bearer_token:
            return
        for query in source.get("queries", []):
            yield from self._api_search(query)

    def _api_search(self, query: str) -> Generator[dict, None, None]:
        self.limiter.wait()
        url = "https://api.twitter.com/2/tweets/search/recent"
        headers = {"Authorization": f"Bearer {self.bearer_token}"}
        params = {
            "query": f"{query} -is:retweet lang:es OR lang:pt OR lang:en",
            "max_results": 100,
            "tweet.fields": "created_at,author_id",
        }
        try:
            resp = self.session.get(url, headers=headers, params=params)
            resp.raise_for_status()
            for tweet in resp.json().get("data", []):
                yield {"type": "tweet", "content": tweet,
                       "url": f"https://twitter.com/i/web/status/{tweet['id']}",
                       "university": None, "search_query": query}
        except Exception as e:
            logger.warning(f"Twitter API '{query}': {e}")


# ── REDDIT CRAWLER ────────────────────────────────────────────────────────────

class RedditCrawler:
    def __init__(self, cfg: dict, praw_cfg: dict | None = None):
        self.timeout = cfg["scraper"]["request_timeout_sec"]
        self.limiter = RateLimiter(min_delay=2.0)
        self.session = _make_session("", self.timeout, 2)

    def crawl_source(self, source: dict) -> Generator[dict, None, None]:
        for subreddit in source.get("subreddits", []):
            for query in source.get("queries", []):
                yield from self._search(subreddit, query)

    def _search(self, subreddit: str, query: str) -> Generator[dict, None, None]:
        self.limiter.wait()
        url = f"https://www.reddit.com/r/{subreddit}/search.json"
        params = {"q": query, "sort": "relevance", "limit": 25, "restrict_sr": 1}
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            for post in resp.json().get("data", {}).get("children", []):
                data = post.get("data", {})
                yield {"type": "reddit_post", "content": data,
                       "url": data.get("url", ""), "university": None,
                       "subreddit": subreddit}
        except Exception as e:
            logger.warning(f"Reddit r/{subreddit} '{query}': {e}")