"""
qise_classifier.py — Conservative, rule-based classification of quantum evidence.

Design principle (from the project brief):
    BROAD during discovery, CONSERVATIVE during classification.

The extractor already casts a wide net (any fragment that mentions a quantum
term survives). This module's job is to sort that evidence into four buckets
WITHOUT over-claiming:

    qise_core                        Quantum Information Science & Engineering
                                     proper, appearing in a course-like context.
    quantum_foundations_or_adjacent  Foundational/adjacent quantum courses
                                     (mechanics, physics, optics, chemistry,
                                     condensed matter, photonics, ...).
    non_course_or_contextual         Quantum mentioned, but on a seminar / news /
                                     research-group / thesis page — not a course.
    unclear                          Quantum evidence that is course-like but not
                                     specific enough to decide core-vs-adjacent,
                                     or too thin to decide course-vs-not.

Everything is keyword + context rules — no model, no network — so every row is
fully explainable via its `explanation` field. A single fragment can yield more
than one candidate row when it mentions genuinely different topics (e.g. a page
listing both "quantum computing" and "quantum optics").
"""

import re
from urllib.parse import unquote

from keywords import (
    CORE_TERMS, ADJACENT_TERMS, GENERIC_TERMS, fold,
    find_quantum_matches, count_signals, detect_academic_level,
)
from utils import evidence_snippet, guess_course_title, get_logger

logger = get_logger("classifier")

# category -> tier, derived once from the taxonomy so the two never drift.
_CATEGORY_TIER: dict[str, str] = {}
for _cat in CORE_TERMS:
    _CATEGORY_TIER[_cat] = "core"
for _cat in ADJACENT_TERMS:
    _CATEGORY_TIER[_cat] = "adjacent"
for _cat in GENERIC_TERMS:
    _CATEGORY_TIER[_cat] = "generic"

CLASSIFICATIONS = (
    "qise_core",
    "quantum_foundations_or_adjacent",
    "non_course_or_contextual",
    "unclear",
)

# Chars on each side of a quantum match to inspect for course / non-course
# context. On a clean course card this spans the whole card; on a coarse
# whole-page fragment it keeps each match's context local so a distant
# "créditos" cannot lend false course-context to a faculty bio or news blurb.
_CONTEXT_RADIUS = 300


def _context_window(text: str, start: int, end: int) -> str:
    return text[max(0, start - _CONTEXT_RADIUS): end + _CONTEXT_RADIUS]


_SENTENCE_BOUNDARY = re.compile(r"[.!?\n]")
_SENTENCE_CAP = 250  # hard cap per side when a "sentence" never terminates

# A quantum term preceded (on its own line) by a prerequisite marker is a
# *reference* to a course listed elsewhere in the same curriculum, not a course
# entry itself — "Prerrequisito: Mecánica Cuántica I" inside the card of
# another course must not become a row of its own.
_PREREQ_MARKER = re.compile(
    r"(prerrequisitos?|pre-?requisitos?|prerequisites?)\s*:", re.IGNORECASE)


def _is_prereq_reference(text: str, start: int) -> bool:
    line_start = text.rfind("\n", 0, start) + 1
    # fold() so "Pré-requisito:" (PT) matches too.
    return bool(_PREREQ_MARKER.search(fold(text[line_start:start])))


def _sentence_window(text: str, start: int, end: int) -> str:
    """The sentence (or line) containing the match — tighter than the context
    window. Used to spot research-blurb phrasing like "se investiga ... óptica
    cuántica" where the surrounding page otherwise looks like a curriculum."""
    lo = max(0, start - _SENTENCE_CAP)
    hi = min(len(text), end + _SENTENCE_CAP)
    before, after = text[lo:start], text[end:hi]
    last = None
    for last in _SENTENCE_BOUNDARY.finditer(before):
        pass  # keep the last boundary before the match
    s = lo + last.end() if last else lo
    nxt = _SENTENCE_BOUNDARY.search(after)
    e = end + nxt.start() if nxt else hi
    return text[s:e]


class QISEClassifier:

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}
        logger.info(f"Classifier ready | categories={len(_CATEGORY_TIER)} "
                    f"(core={len(CORE_TERMS)} adjacent={len(ADJACENT_TERMS)})")

    # ── PUBLIC API ────────────────────────────────────────────────────────────

    def classify(self, fragment: dict) -> list[dict]:
        """
        Turn one evidence fragment into 0..N candidate rows. Returns [] only when
        the fragment carries no quantum evidence AND extracted cleanly (i.e.
        genuinely irrelevant). Failed/scanned PDFs always yield one review row.
        """
        status = fragment.get("extraction_status", "extracted")
        text = fragment.get("raw_text", "") or ""

        # PDFs we couldn't read are candidates for MANUAL review, never dropped.
        if status in ("failed_pdf_extraction", "needs_manual_review"):
            return [self._manual_review_row(fragment)]

        matches = find_quantum_matches(text)
        if not matches:
            return []

        # Prefer specific (core/adjacent) categories; fall back to the bare stem
        # only when nothing specific matched.
        specific = [m for m in matches if m["tier"] in ("core", "adjacent")]
        chosen = specific if specific else matches

        source_type = fragment.get("source_type", "html_page")
        media = fragment.get("media_type", "html")
        coarse = bool(fragment.get("coarse"))

        # Undergrad/grad level, strongest signal first: the URLs themselves
        # (/pregrado/, posgrado. subdomains), then each match's local context,
        # then the document-level hint stamped by the extractor (front matter).
        url_level = detect_academic_level(unquote(re.sub(
            r"[-_/.:+%?=&]+", " ",
            " ".join(fragment.get(k) or ""
                     for k in ("source_url", "pdf_url", "found_on_page")))))
        doc_level_hint = fragment.get("academic_level_hint", "")

        # Group matches by (category, course line): a curriculum can list
        # several distinct courses of the same category ("Mecánica Cuántica 1",
        # "Mecánica Cuántica 2", "Mecánica Cuántica Relativista") and each must
        # become its own row. Repeat mentions of the same line collapse; within
        # a group keep the most specific (longest) phrase.
        groups: dict[tuple[str, str], dict] = {}
        for m in chosen:
            if _is_prereq_reference(text, m["start"]):
                continue  # reference to a course listed elsewhere, not an entry
            line_key = fold(guess_course_title(text, m["start"], m["end"]))
            key = (m["category"], line_key)
            cur = groups.get(key)
            if cur is None or len(m["phrase"]) > len(cur["phrase"]):
                groups[key] = m

        all_phrases = sorted({m["phrase"] for m in chosen})

        rows: list[dict] = []
        for (cat, _line_key), m in groups.items():
            tier = _CATEGORY_TIER.get(cat, "generic")
            # Count context signals in a LOCAL window around this match rather
            # than across the whole fragment — size-robust and bleed-proof.
            window = _context_window(text, m["start"], m["end"])
            strong = count_signals(window, "strong")
            weak = count_signals(window, "weak")
            noncourse = count_signals(window, "noncourse")
            # Tighter still: the match's own sentence. Catches research blurbs
            # ("se investiga ... óptica cuántica") sitting on curriculum pages.
            sentence = _sentence_window(text, m["start"], m["end"])
            sent_strong = count_signals(sentence, "strong")
            sent_noncourse = count_signals(sentence, "noncourse")
            classification = self._decide(
                tier, strong, weak, noncourse, source_type, coarse,
                sent_strong=sent_strong, sent_noncourse=sent_noncourse,
            )
            confidence = self._confidence(
                classification, strong, noncourse, source_type
            )
            snippet = evidence_snippet(text, m["start"], m["end"])
            # Per-match line first: the fragment title is document-level (for a
            # PDF it's the first line — often a university letterhead) and must
            # not override the actual course line. For PDFs, no usable match
            # line means no course title; the letterhead would be misleading.
            course_title = guess_course_title(text, m["start"], m["end"])
            if not course_title and media != "pdf":
                course_title = fragment.get("title", "")
            academic_level = (url_level
                              or detect_academic_level(window)
                              or doc_level_hint)
            rows.append({
                **fragment,
                "matched_keyword": m["phrase"],
                "matched_keywords": all_phrases,
                "semantic_category": cat,
                "keyword_tier": tier,
                "classification": classification,
                "confidence": confidence,
                "academic_level": academic_level,
                "course_title": course_title,
                "evidence_snippet": snippet,
                "explanation": (
                    f"tier={tier} cat={cat} strong={strong} weak={weak} "
                    f"noncourse={noncourse} sent_strong={sent_strong} "
                    f"sent_noncourse={sent_noncourse} src={source_type}/{media} "
                    f"level={academic_level or 'unknown'} "
                    f"→ {classification}/{confidence}"
                ),
            })
        return rows

    # ── DECISION RULES ────────────────────────────────────────────────────────

    @staticmethod
    def _decide(tier, strong, weak, noncourse, source_type, coarse=False,
                sent_strong=0, sent_noncourse=0) -> str:
        formal_doc = source_type in ("syllabus", "curriculum_grid")
        course_page = source_type in ("syllabus", "curriculum_grid", "catalog",
                                      "course_list")
        contextual_src = source_type in ("news", "social")

        # 1) Contextual / non-course evidence dominates. A real syllabus almost
        #    always carries STRONG signals (créditos, prerrequisito, sílabo), so
        #    the strong==0 guard protects genuine courses that merely mention a
        #    "seminario" as a teaching method.
        if contextual_src:
            return "non_course_or_contextual"
        if noncourse >= 1 and strong == 0 and not formal_doc:
            return "non_course_or_contextual"
        # 1b) Sentence-level: the match's own sentence reads as research/news
        #     ("se investiga ... óptica cuántica") with no strong course signal
        #     in that same sentence. Overrides page-level context — hub pages
        #     mix curriculum links with research blurbs, and the URL saying
        #     "plan-de-estudios" must not launder a research mention.
        if sent_noncourse >= 1 and sent_strong == 0:
            return "non_course_or_contextual"

        # 2) Is there a course-like context at all?
        has_course_ctx = (
            strong >= 1 or formal_doc or course_page or weak >= 2
        )

        if tier == "core":
            # Coarse whole-page fragment: only a STRONG local signal (créditos,
            # sílabo, prerrequisito, …) justifies the strongest claim. Weak
            # proximity on a mixed page is not enough — send it to review.
            if coarse:
                return "qise_core" if strong >= 1 else "unclear"
            # Core terms are highly specific; any course signal is enough.
            return "qise_core" if (has_course_ctx or weak >= 1) else "unclear"
        if tier == "adjacent":
            return "quantum_foundations_or_adjacent" if has_course_ctx else "unclear"
        # generic bare stem: course-like but cannot decide core vs adjacent.
        return "unclear"

    @staticmethod
    def _confidence(classification, strong, noncourse, source_type) -> str:
        formal_doc = source_type in ("syllabus", "curriculum_grid")
        if classification in ("qise_core", "quantum_foundations_or_adjacent"):
            return "high" if (strong >= 1 or formal_doc) else "medium"
        if classification == "non_course_or_contextual":
            return "medium" if noncourse >= 1 else "low"
        return "low"  # unclear

    # ── FAILED / SCANNED PDF ROW ──────────────────────────────────────────────

    @staticmethod
    def _manual_review_row(fragment: dict) -> dict:
        status = fragment.get("extraction_status", "needs_manual_review")
        note = ("PDF opened but little/no extractable text (likely scanned or "
                "image-based) — manual review needed."
                if status == "needs_manual_review"
                else "PDF could not be opened by any extraction engine.")
        return {
            **fragment,
            "matched_keyword": "",
            "matched_keywords": [],
            "semantic_category": "",
            "keyword_tier": "",
            "classification": "unclear",
            "confidence": "low",
            "academic_level": fragment.get("academic_level_hint", ""),
            "course_title": fragment.get("title", ""),
            "evidence_snippet": note,
            "explanation": f"extraction_status={status}",
        }
