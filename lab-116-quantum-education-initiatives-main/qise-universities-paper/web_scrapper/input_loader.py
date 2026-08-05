"""
input_loader.py — Load the university list from CSV or YAML into the internal
`university` dict shape the crawler expects:

    {
      "name": str, "country": str, "country_code": str,
      "base_url": str, "catalog_urls": [str, ...],
      "type": "web", "language": str,
      "max_pages": int|None, "max_depth": int|None,
    }

CSV is intentionally forgiving: column names are matched case-insensitively and
by substring, so headers like "Institution", "University Name", "Website",
"Domain", "Seed URLs", "Physics Dept URL", etc. all work. Any column whose name
contains 'url', 'seed', 'catalog', 'dept', 'faculty', 'department', 'curricul'
or 'malla' contributes seed URLs (semicolon/pipe/comma/newline separated).
"""

import csv
import re
from pathlib import Path
from urllib.parse import urlparse

from utils import normalize_url, get_logger

logger = get_logger("input_loader")

# Minimal country → ISO-3166 alpha-2 for Latin America (extend as needed).
COUNTRY_TO_CODE = {
    "argentina": "AR", "bolivia": "BO", "brazil": "BR", "brasil": "BR",
    "chile": "CL", "colombia": "CO", "costa rica": "CR", "cuba": "CU",
    "dominican republic": "DO", "ecuador": "EC", "el salvador": "SV",
    "guatemala": "GT", "honduras": "HN", "mexico": "MX", "méxico": "MX",
    "nicaragua": "NI", "panama": "PA", "panamá": "PA", "paraguay": "PY",
    "peru": "PE", "perú": "PE", "puerto rico": "PR", "uruguay": "UY",
    "venezuela": "VE",
}

_SEED_COL_HINTS = ("url", "seed", "catalog", "catálogo", "dept", "department",
                   "departamento", "faculty", "facultad", "curricul", "malla",
                   "programa", "plan")
_SPLIT = re.compile(r"[;\|\n]+")


def load_universities(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    suffix = p.suffix.lower()
    if suffix in (".yaml", ".yml"):
        unis = _load_yaml(p)
    elif suffix == ".csv":
        unis = _load_csv(p)
    else:
        raise ValueError(f"Unsupported input format '{suffix}'. Use .csv or .yaml")

    cleaned = [_finalize(u) for u in unis]
    cleaned = [u for u in cleaned if u and u.get("catalog_urls")]
    logger.info(f"Loaded {len(cleaned)} universities from {path}")
    return cleaned


# ── YAML ──────────────────────────────────────────────────────────────────────

def _load_yaml(p: Path) -> list[dict]:
    import yaml
    with open(p, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if isinstance(data, dict):
        data = data.get("universities", [])
    if not isinstance(data, list):
        raise ValueError("YAML must be a list, or a mapping with a 'universities' list")
    return data


# ── CSV ───────────────────────────────────────────────────────────────────────

def _load_csv(p: Path) -> list[dict]:
    # Tolerate BOM and odd encodings common in exported spreadsheets.
    text = None
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            text = p.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = p.read_text(encoding="utf-8", errors="replace")

    reader = csv.DictReader(text.splitlines())
    headers = reader.fieldnames or []
    name_col = _find_col(headers, ["institution", "university", "name", "nombre"])
    country_col = _find_col(headers, ["country", "país", "pais"])
    code_col = _find_col(headers, ["country_code", "code", "iso"])
    base_col = _find_col(headers, ["website", "domain", "base_url", "url oficial",
                                   "sitio", "official"])
    lang_col = _find_col(headers, ["language", "idioma", "lang"])
    depth_col = _find_col(headers, ["max_depth", "depth"])
    pages_col = _find_col(headers, ["max_pages", "pages"])
    seed_cols = [h for h in headers if any(k in h.lower() for k in _SEED_COL_HINTS)]

    if not name_col:
        raise ValueError(f"CSV needs an institution/name column. Found: {headers}")

    out: list[dict] = []
    for row in reader:
        name = (row.get(name_col) or "").strip()
        if not name:
            continue
        seeds: list[str] = []
        base = (row.get(base_col) or "").strip() if base_col else ""
        # Seed URLs are MANUAL only when they come from dedicated seed columns;
        # the base/website column alone does not count (seed_origin tracking).
        for col in seed_cols:
            if col == base_col:
                continue
            for piece in _SPLIT.split(row.get(col) or ""):
                for sub in piece.split(","):
                    sub = sub.strip()
                    if sub.startswith("http"):
                        seeds.append(sub)
        has_manual = bool(seeds)
        # the base/website doubles as a seed (first) either way
        if base and base not in seeds:
            seeds.insert(0, base if base.startswith("http") else "https://" + base)
        out.append({
            "name": name,
            "country": (row.get(country_col) or "").strip() if country_col else "",
            "country_code": (row.get(code_col) or "").strip() if code_col else "",
            "base_url": base,
            "catalog_urls": seeds,
            "has_manual_seeds": has_manual,
            "language": (row.get(lang_col) or "").strip() if lang_col else "",
            "max_depth": _to_int(row.get(depth_col)) if depth_col else None,
            "max_pages": _to_int(row.get(pages_col)) if pages_col else None,
        })
    return out


# ── FINALIZE ──────────────────────────────────────────────────────────────────

def _finalize(u: dict) -> dict | None:
    if not isinstance(u, dict):
        return None
    name = (u.get("name") or "").strip()
    if not name:
        return None
    seeds = [normalize_url(s) for s in (u.get("catalog_urls") or []) if s]
    seeds = [s for s in seeds if s]

    # CSV loader sets this explicitly; YAML entries count as manual when they
    # declare catalog_urls. The homepage fallback below is NOT manual.
    has_manual = bool(u.get("has_manual_seeds", bool(seeds)))

    base = (u.get("base_url") or "").strip()
    if not base and seeds:
        pu = urlparse(seeds[0])
        base = f"{pu.scheme}://{pu.netloc}"
    if base and not base.startswith("http"):
        base = "https://" + base
    # If no seeds but we have a base, crawl from the homepage.
    if not seeds and base:
        seeds = [normalize_url(base)]

    country = (u.get("country") or "").strip()
    code = (u.get("country_code") or "").strip().upper()
    if not code and country:
        code = COUNTRY_TO_CODE.get(country.lower(), "")

    return {
        "name": name,
        "country": country,
        "country_code": code,
        "base_url": base,
        "catalog_urls": seeds,
        "has_manual_seeds": has_manual,
        "type": u.get("type", "web"),
        "language": (u.get("language") or "").strip(),
        "max_depth": u.get("max_depth"),
        "max_pages": u.get("max_pages"),
    }


def _find_col(headers: list[str], candidates: list[str]) -> str | None:
    for cand in candidates:
        for h in headers:
            if cand.lower() in h.lower():
                return h
    return None


def _to_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None
