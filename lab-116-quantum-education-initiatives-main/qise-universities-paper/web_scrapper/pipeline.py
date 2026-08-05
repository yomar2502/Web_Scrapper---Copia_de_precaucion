"""
pipeline.py — Institution-level orchestration.

Flow:
    input (CSV/YAML)  ─┐
    config.yaml       ─┴─► Dispatcher (WebCrawler → HTML/PDF)
                             │
                             ▼
                         Extractor  →  evidence fragments (HTML pages, PDF pages)
                             │
                             ▼
                         QISEClassifier  →  candidate rows (4-category, auditable)
                             │
                             ▼
                    dedupe (source_url × semantic_category)
                             │
                             ▼
              qise_candidates.csv  +  .json  +  run_summary.json

Every output row carries a source URL and an evidence snippet, so the dataset is
auditable: no "University X has QISE" claim without traceable evidence.
"""

import csv
import json
import re
import time
from pathlib import Path

import yaml

from dispatcher import Dispatcher
from keywords import fold
from qise_classifier import QISEClassifier
from input_loader import load_universities
from utils import get_logger, truncate_text, now_iso, normalize_url

logger = get_logger("pipeline")

# Auditable output schema (column order).
OUTPUT_FIELDS = [
    "timestamp",
    "institution",
    "country",
    "country_code",
    "classification",          # qise_core | quantum_foundations_or_adjacent | non_course_or_contextual | unclear
    "confidence",              # high | medium | low
    "is_qise_core",            # bool, convenience for the availability variable
    "academic_level",          # undergraduate | graduate | unknown
    "semantic_category",       # e.g. quantum_computing, quantum_mechanics
    "keyword_tier",            # core | adjacent | generic
    "matched_keyword",         # primary matched term
    "matched_keywords",        # all matched terms (| separated)
    "course_title",
    "evidence_snippet",
    "source_type",             # syllabus | curriculum_grid | catalog | department_page | course_list | html_page | pdf | news | social
    "media_type",              # html | pdf
    "source_url",
    "pdf_url",
    "pdf_page",
    "found_on_page",           # page a PDF link was discovered on
    "seed_origin",             # manual | auto_discovered | homepage_crawl
    "extraction_status",       # extracted | failed_pdf_extraction | needs_manual_review
    "language",
]

# Task-brief config key spellings accepted as aliases of the internal names.
_CONFIG_KEY_ALIASES = {
    "max_pages_per_domain": "max_pages_per_university",
    "request_delay_seconds": "request_delay_sec",
    "timeout_seconds": "request_timeout_sec",
    "respect_robots_txt": "respect_robots",
}

_CONF_RANK = {"high": 3, "medium": 2, "low": 1, "": 0, None: 0}

# Leading course-code token ("MF719 ", "FIS-410: ", "EE 80 ") — stripped from
# the dedupe key so a catalog listing "Simetrías discretas" and its syllabus
# page "MF719 Simetrías discretas" collapse into one row. Applied to folded
# (lowercased) titles.
_COURSE_CODE_PREFIX = re.compile(r"^[a-z]{1,4}[- ]?\d{2,4}[a-z]?\b[\s.:–—-]*")


def _title_key(title: str) -> str:
    t = _COURSE_CODE_PREFIX.sub("", fold(title or ""))
    return re.sub(r"\s+", " ", t).strip()


class Pipeline:

    def __init__(self, config_path="config.yaml", input_path=None,
                 sources_path=None, overrides=None):
        logger.info("=" * 60)
        logger.info("QISE-LatAm-Scraper pipeline starting")
        logger.info("=" * 60)

        with open(config_path, encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f) or {}
        self.cfg.setdefault("scraper", {})
        self.cfg.setdefault("output", {})

        # Accept alternative config key spellings (max_pages_per_domain,
        # request_delay_seconds, …) as aliases of the internal names.
        sc = self.cfg["scraper"]
        for alias, canonical in _CONFIG_KEY_ALIASES.items():
            if alias in sc and canonical not in sc:
                sc[canonical] = sc[alias]

        overrides = overrides or {}
        for key in ("max_depth", "max_pages_per_university", "download_pdfs",
                    "use_cache", "respect_robots", "request_delay_sec",
                    "auto_discover_seeds"):
            if overrides.get(key) is not None:
                self.cfg["scraper"][key] = overrides[key]

        for dkey in ("raw_dir", "processed_dir", "log_dir"):
            d = self.cfg["output"].get(dkey)
            if d:
                Path(d).mkdir(parents=True, exist_ok=True)

        # Load universities: explicit --input wins, else sources.yaml.
        if input_path:
            self.universities = load_universities(input_path)
        elif sources_path and Path(sources_path).exists():
            self.universities = load_universities(sources_path)
        else:
            raise FileNotFoundError(
                "No input provided. Pass --input <file.csv|yaml> or keep sources.yaml."
            )

        self.classifier = QISEClassifier(self.cfg)
        self._sources_path = sources_path

    # ── MAIN ENTRY POINT ──────────────────────────────────────────────────────

    def run(self, output_path, dry_run=False, limit=None, country=None,
            resume=False, include_news=False, include_social=False,
            force_discover=False) -> dict:
        start = time.time()
        ts = now_iso()

        universities = self._filter_by_country(self.universities, country)
        logger.info(f"Universities to process: {len(universities)}"
                    + (f" (country={country})" if country else ""))

        # Resume: keep prior rows, skip institutions already in the output file.
        existing_rows: list[dict] = []
        done_institutions: set[str] = set()
        out_path = Path(output_path)
        if resume and out_path.exists():
            existing_rows = self._read_existing(out_path)
            done_institutions = {r.get("institution", "") for r in existing_rows}
            universities = [u for u in universities
                            if u["name"] not in done_institutions]
            logger.info(f"Resume: {len(done_institutions)} institutions already done, "
                        f"{len(universities)} remaining")

        # Stage 1 — automatic seed discovery for institutions without manual
        # seeds (all institutions when force_discover). Sets seed_origin on
        # each university and writes data/processed/discovered_seeds.csv.
        seeds_discovered = self._resolve_seeds(universities, dry_run=dry_run,
                                               force=force_discover)

        sources = self._build_sources(universities, include_news, include_social)
        dispatcher = Dispatcher(self.cfg, sources)

        # source_url × semantic_category × course_title → best row so far.
        # course_title is part of the key so distinct courses of the same
        # category on one document (Mecánica Cuántica 1 / 2 / Relativista)
        # each keep their own row.
        best: dict[tuple, dict] = {}
        fragments_seen = 0
        pdf_docs_seen: set[str] = set()
        pdf_docs_extracted: set[str] = set()

        logger.info("Phase 1/2 — crawling, extracting, classifying...")
        for fragment in dispatcher.stream_all_records():
            fragments_seen += 1
            if fragment.get("media_type") == "pdf":
                pdf_docs_seen.add(fragment.get("source_url", ""))
                if fragment.get("extraction_status") == "extracted":
                    pdf_docs_extracted.add(fragment.get("source_url", ""))
            if limit and fragments_seen > limit:
                logger.info(f"Fragment limit reached ({limit}). Stopping.")
                break

            for cand in self.classifier.classify(fragment):
                row = self._to_row(cand, ts)
                key = (row["source_url"], row["semantic_category"],
                       _title_key(row.get("course_title", "")))
                prev = best.get(key)
                if prev is None or _CONF_RANK[row["confidence"]] > _CONF_RANK[prev["confidence"]]:
                    best[key] = row

            if fragments_seen % 100 == 0:
                logger.info(f"  {fragments_seen} fragments | {len(best)} candidate rows")

        new_rows = list(best.values())
        all_rows = self._merge(existing_rows, new_rows)
        logger.info(f"Phase 1/2 complete — {len(new_rows)} new candidate rows "
                    f"({len(all_rows)} total)")

        logger.info("Phase 2/2 — writing output...")
        if not dry_run:
            self._write_csv(out_path, all_rows)
            self._write_json(out_path.with_suffix(".json"), all_rows)

        crawl_stats = dict(dispatcher.web_crawler.stats)
        summary = self._build_summary(all_rows, round(time.time() - start, 1),
                                      fragments_seen, crawl_stats=crawl_stats,
                                      seeds_discovered=seeds_discovered,
                                      pdf_docs_seen=len(pdf_docs_seen),
                                      pdf_docs_extracted=len(pdf_docs_extracted))
        self._print_summary(summary)
        if not dry_run:
            proc_dir = Path(self.cfg["output"].get("processed_dir", "data/processed"))
            proc_dir.mkdir(parents=True, exist_ok=True)
            (proc_dir / "run_summary.json").write_text(
                json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        return summary

    # ── STAGE 1: SEED RESOLUTION / DISCOVERY ──────────────────────────────────

    def _resolve_seeds(self, universities, dry_run=False, force=False) -> int:
        """
        Decide each university's seeds + seed_origin:
          manual          — seed URLs came from the input file (untouched)
          auto_discovered — no manual seeds; SeedDiscoverer found candidates
          homepage_crawl  — no manual seeds and discovery found nothing (or is
                            disabled): crawl starts at the homepage as before
        With force=True, discovery runs for ALL institutions and discovered
        seeds replace manual ones (for A/B-testing discovery quality).
        Returns the number of discovered seeds; writes discovered_seeds.csv.
        """
        auto = self.cfg["scraper"].get("auto_discover_seeds", True)
        for u in universities:
            u["seed_origin"] = ("manual" if u.get("has_manual_seeds")
                                else "homepage_crawl")
        targets = [u for u in universities
                   if force or not u.get("has_manual_seeds")]
        if not auto or not targets:
            if targets:
                logger.info(f"Seed discovery disabled — {len(targets)} "
                            f"institution(s) will be crawled from the homepage")
            return 0

        from seed_discovery import SeedDiscoverer  # late import (network module)
        discoverer = SeedDiscoverer(self.cfg)
        all_candidates: list[dict] = []
        for u in targets:
            found = discoverer.discover(u)
            all_candidates.extend(found)
            if found:
                seeds = [f["seed_url"] for f in found]
                base = normalize_url(u.get("base_url") or "")
                u["catalog_urls"] = seeds + ([base] if base and base not in seeds
                                             else [])
                u["seed_origin"] = "auto_discovered"
            elif not u.get("has_manual_seeds"):
                u["seed_origin"] = "homepage_crawl"
            # force + nothing found + manual seeds present → keep manual.

        if not dry_run:
            self._write_discovered_seeds(all_candidates)
        origins = {}
        for u in universities:
            origins[u["seed_origin"]] = origins.get(u["seed_origin"], 0) + 1
        logger.info(f"Seed resolution: {origins} | "
                    f"{len(all_candidates)} seeds auto-discovered")
        return len(all_candidates)

    def _write_discovered_seeds(self, candidates: list[dict]) -> None:
        if not candidates:
            return
        proc_dir = Path(self.cfg["output"].get("processed_dir", "data/processed"))
        proc_dir.mkdir(parents=True, exist_ok=True)
        path = proc_dir / "discovered_seeds.csv"
        fields = ["institution", "seed_url", "source", "score",
                  "matched_terms", "reason"]
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(candidates)
        logger.info(f"Discovered seeds written → {path} ({len(candidates)} rows)")

    def discover_only(self, country=None, force=False) -> int:
        """
        --discover-seeds-only: run Stage 1 for every institution without manual
        seeds (all of them with force=True), print the results, write
        discovered_seeds.csv, and return the number of discovered seeds —
        without crawling anything else.
        """
        from seed_discovery import SeedDiscoverer
        universities = self._filter_by_country(self.universities, country)
        discoverer = SeedDiscoverer(self.cfg)
        all_candidates: list[dict] = []
        for u in universities:
            if u.get("has_manual_seeds") and not force:
                logger.info(f"{u['name']}: manual seeds present "
                            f"({len(u.get('catalog_urls') or [])}) — skipping "
                            f"discovery (use --force-discover to override)")
                continue
            all_candidates.extend(discoverer.discover(u))
        self._write_discovered_seeds(all_candidates)
        logger.info(f"Seed discovery complete: {len(all_candidates)} seeds "
                    f"across {len({c['institution'] for c in all_candidates})} "
                    f"institution(s)")
        return len(all_candidates)

    # ── HELPERS ───────────────────────────────────────────────────────────────

    @staticmethod
    def _filter_by_country(universities, country):
        if not country:
            return universities
        c = country.strip().lower()
        return [u for u in universities
                if c in (u.get("country", "").lower(), u.get("country_code", "").lower())]

    def _build_sources(self, universities, include_news, include_social) -> dict:
        sources = {"universities": universities}
        if (include_news or include_social) and self._sources_path \
                and Path(self._sources_path).exists() \
                and Path(self._sources_path).suffix.lower() in (".yaml", ".yml"):
            try:
                with open(self._sources_path, encoding="utf-8") as f:
                    extra = yaml.safe_load(f) or {}
                if include_news:
                    sources["news_sources"] = extra.get("news_sources", [])
                if include_social:
                    sources["social_sources"] = extra.get("social_sources", [])
            except Exception as e:
                logger.warning(f"Could not load extra sources: {e}")
        return sources

    @staticmethod
    def _to_row(cand: dict, ts: str) -> dict:
        classification = cand.get("classification", "unclear")
        pdf_page = cand.get("pdf_page")
        return {
            "timestamp": ts,
            "institution": cand.get("university", ""),
            "country": cand.get("country", ""),
            "country_code": cand.get("country_code", ""),
            "classification": classification,
            "confidence": cand.get("confidence", "low"),
            "is_qise_core": classification == "qise_core",
            "academic_level": cand.get("academic_level") or "unknown",
            "semantic_category": cand.get("semantic_category", ""),
            "keyword_tier": cand.get("keyword_tier", ""),
            "matched_keyword": cand.get("matched_keyword", ""),
            "matched_keywords": "|".join(cand.get("matched_keywords", []) or []),
            "course_title": (cand.get("course_title") or "")[:200],
            "evidence_snippet": truncate_text(cand.get("evidence_snippet", ""), 400),
            "source_type": cand.get("source_type", ""),
            "media_type": cand.get("media_type", ""),
            "source_url": cand.get("source_url", ""),
            "pdf_url": cand.get("pdf_url", ""),
            "pdf_page": "" if pdf_page is None else pdf_page,
            "found_on_page": cand.get("found_on_page", ""),
            "seed_origin": cand.get("seed_origin", ""),
            "extraction_status": cand.get("extraction_status", "extracted"),
            "language": cand.get("language", ""),
        }

    @staticmethod
    def _merge(existing: list[dict], new: list[dict]) -> list[dict]:
        by_key: dict[tuple, dict] = {}
        for r in existing + new:
            key = (r.get("source_url", ""), r.get("semantic_category", ""),
                   _title_key(r.get("course_title", "")))
            prev = by_key.get(key)
            if prev is None or _CONF_RANK.get(r.get("confidence")) \
                    >= _CONF_RANK.get(prev.get("confidence")):
                by_key[key] = r
        return list(by_key.values())

    @staticmethod
    def _read_existing(path: Path) -> list[dict]:
        try:
            with open(path, encoding="utf-8") as f:
                return list(csv.DictReader(f))
        except Exception as e:
            logger.warning(f"Could not read existing output for resume: {e}")
            return []

    @staticmethod
    def _write_csv(path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Sort for stable, reviewer-friendly output.
        order = {"qise_core": 0, "quantum_foundations_or_adjacent": 1,
                 "unclear": 2, "non_course_or_contextual": 3}
        rows = sorted(rows, key=lambda r: (
            r.get("institution", ""),
            order.get(r.get("classification"), 9),
            -_CONF_RANK.get(r.get("confidence"), 0),
        ))
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        logger.info(f"Wrote {len(rows)} rows → {path}")

    @staticmethod
    def _write_json(path: Path, rows: list[dict]) -> None:
        path.write_text(json.dumps(rows, indent=2, ensure_ascii=False),
                        encoding="utf-8")

    # ── SUMMARY ───────────────────────────────────────────────────────────────

    def _build_summary(self, rows, elapsed, fragments_seen, crawl_stats=None,
                       seeds_discovered=0, pdf_docs_seen=0,
                       pdf_docs_extracted=0) -> dict:
        by_class: dict[str, int] = {}
        by_conf: dict[str, int] = {}
        for r in rows:
            by_class[r["classification"]] = by_class.get(r["classification"], 0) + 1
            by_conf[r["confidence"]] = by_conf.get(r["confidence"], 0) + 1

        # QISE availability variable computed ONLY from qise_core (per the brief).
        qise_core = [r for r in rows if r.get("classification") == "qise_core"]
        institutions_with_core = sorted({r["institution"] for r in qise_core
                                         if r["institution"]})
        countries_with_core = sorted({r["country_code"] for r in qise_core
                                      if r["country_code"]})

        by_country: dict[str, dict] = {}
        for r in rows:
            code = r.get("country_code") or "??"
            b = by_country.setdefault(code, {"rows": 0, "qise_core": 0,
                                             "institutions": set(),
                                             "institutions_with_core": set()})
            b["rows"] += 1
            if r["institution"]:
                b["institutions"].add(r["institution"])
            if r["classification"] == "qise_core":
                b["qise_core"] += 1
                if r["institution"]:
                    b["institutions_with_core"].add(r["institution"])
        for b in by_country.values():
            b["institutions"] = len(b["institutions"])
            b["institutions_with_core"] = sorted(b["institutions_with_core"])

        manual_review = sum(1 for r in rows
                            if r.get("extraction_status") != "extracted")
        pdf_rows = sum(1 for r in rows if r.get("media_type") == "pdf")

        by_seed_origin: dict[str, int] = {}
        for r in rows:
            so = r.get("seed_origin") or ""
            by_seed_origin[so] = by_seed_origin.get(so, 0) + 1

        crawl_stats = crawl_stats or {}
        pages_crawled = sum(s.get("pages_crawled", 0) for s in crawl_stats.values())
        pdfs_detected = sum(s.get("pdfs_detected", 0) for s in crawl_stats.values())

        return {
            "run_timestamp": now_iso(),
            "elapsed_seconds": elapsed,
            "seeds_discovered": seeds_discovered,
            "pages_crawled": pages_crawled,
            "pdfs_detected": pdfs_detected,
            "pdf_documents_processed": pdf_docs_seen,
            "pdf_documents_extracted": pdf_docs_extracted,
            "fragments_processed": fragments_seen,
            "candidate_rows": len(rows),
            "rows_from_pdf": pdf_rows,
            "rows_needing_manual_review": manual_review,
            "by_classification": by_class,
            "by_confidence": by_conf,
            "by_seed_origin": by_seed_origin,
            "qise_core_rows": len(qise_core),
            "institutions_with_qise_core": institutions_with_core,
            "countries_with_qise_core": countries_with_core,
            "by_country": by_country,
            "crawl_stats_per_institution": crawl_stats,
        }

    @staticmethod
    def _print_summary(s: dict) -> None:
        logger.info("=" * 60)
        logger.info("PIPELINE COMPLETE")
        logger.info(f"  Seeds discovered    : {s.get('seeds_discovered', 0)}")
        logger.info(f"  Pages crawled       : {s.get('pages_crawled', 0)}")
        logger.info(f"  PDFs detected       : {s.get('pdfs_detected', 0)}")
        logger.info(f"  PDFs extracted OK   : {s.get('pdf_documents_extracted', 0)}"
                    f"/{s.get('pdf_documents_processed', 0)}")
        logger.info(f"  Fragments processed : {s['fragments_processed']}")
        logger.info(f"  Candidate rows      : {s['candidate_rows']}")
        logger.info(f"  From PDFs           : {s['rows_from_pdf']}")
        logger.info(f"  Need manual review  : {s['rows_needing_manual_review']}")
        logger.info(f"  By seed origin      : {s.get('by_seed_origin', {})}")
        logger.info(f"  Elapsed             : {s['elapsed_seconds']}s")
        logger.info("-" * 60)
        logger.info("  By classification:")
        for k, v in sorted(s["by_classification"].items()):
            logger.info(f"    {k:<34s}: {v}")
        logger.info("-" * 60)
        logger.info(f"  qise_core rows      : {s['qise_core_rows']}")
        logger.info(f"  Institutions w/ core: {len(s['institutions_with_qise_core'])}")
        for name in s["institutions_with_qise_core"]:
            logger.info(f"      • {name}")
        logger.info("=" * 60)
