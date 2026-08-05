"""
manual_input.py — Ingreso manual de cursos cuando el scraper no puede acceder al sitio.

USO:
  python manual_input.py

El script te pregunta los datos del curso y los agrega directamente al dataset
con el mismo formato que el scraper, incluyendo clasificación QISE automática.

Útil para:
  - Sitios que bloquean bots (UTEC, PUCP, UNMSM)
  - Cursos que sabes que existen pero el crawler no llegó
  - Verificación manual del dataset
"""

import csv
from pathlib import Path
from qise_classifier import QISEClassifier
from pipeline import OUTPUT_FIELDS, Pipeline, _CONF_RANK
from utils import get_logger, now_iso

logger = get_logger("manual_input")

DATASET_PATH = Path("data/qise_candidates.csv")

LEVELS = ["undergrad", "masters", "phd", "specialization", "diploma"]
PROGRAMS = ["Physics", "CS", "Engineering", "Mathematics", "Interdisciplinary", "Other"]


def input_course() -> dict:
    print("\n" + "="*55)
    print("  NUEVO CURSO — ingresa los datos (Enter para omitir)")
    print("="*55)

    university  = input("Universidad: ").strip()
    country     = input("País: ").strip()
    country_code = input("Código país (AR/BR/CL/CO/MX/PE...): ").strip().upper()
    course_name = input("Nombre del curso: ").strip()

    print(f"Nivel ({'/'.join(LEVELS)}): ", end="")
    level = input().strip() or "undergrad"

    print(f"Programa ({'/'.join(PROGRAMS)}): ", end="")
    program = input().strip() or "Physics"

    source_url = input("URL de referencia (página del curso): ").strip()

    print("Descripción / contenido del curso (pega el texto, Enter x2 para terminar):")
    lines = []
    while True:
        line = input()
        if line == "" and lines and lines[-1] == "":
            break
        lines.append(line)
    description = "\n".join(lines).strip()

    # Si no hay descripción, construir una mínima del nombre del curso
    if not description:
        description = course_name

    # Build an extractor-style "fragment" so the classifier sees the same shape
    # it gets from the crawler.
    return {
        "media_type":         "html",
        "source_url":         source_url,
        "found_on_page":      "",
        "pdf_url":            "",
        "pdf_page":           None,
        "source_type":        "syllabus" if description != course_name else "course_list",
        "title":              course_name,
        "raw_text":           f"{course_name}\n{description}",
        "university":         university,
        "country":            country,
        "country_code":       country_code,
        "language":           "es",
        "extraction_status":  "extracted",
        # extra annotations (ignored by the CSV writer, handy for notes)
        "program":            program,
        "level":              level,
        # kept for the console print
        "_course_name":       course_name,
    }


def append_to_dataset(row: dict, path: Path):
    """Append one output row to the CSV (creating it with a header if needed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)
    print(f"  → Saved to {path}")


def _best_row(rows: list[dict]) -> dict | None:
    """Pick the most informative candidate: prefer qise_core, then confidence."""
    if not rows:
        return None
    order = {"qise_core": 0, "quantum_foundations_or_adjacent": 1,
             "unclear": 2, "non_course_or_contextual": 3}
    return sorted(rows, key=lambda r: (
        order.get(r.get("classification"), 9),
        -_CONF_RANK.get(r.get("confidence"), 0),
    ))[0]


def main():
    classifier = QISEClassifier({})

    print("\nQISE-LatAm — manual course entry")
    print("   For universities that block the scraper (UTEC, PUCP, UNMSM, etc.)")

    while True:
        fragment = input_course()
        if not fragment["university"] or not fragment["_course_name"]:
            print("University and course name are required.")
            continue

        cand = _best_row(classifier.classify(fragment))
        if cand is None:
            print("  No quantum-related evidence detected — nothing to classify.")
        else:
            row = Pipeline._to_row(cand, now_iso())
            print("\n  Classification:")
            print(f"    classification : {row['classification']}")
            print(f"    confidence     : {row['confidence']}")
            print(f"    category       : {row['semantic_category'] or '(none)'}")
            print(f"    matched        : {row['matched_keywords'] or '(none)'}")

            if input("\n  Save this course? (y/n): ").strip().lower() in ("y", "s"):
                append_to_dataset(row, DATASET_PATH)
                print(f"  ✓ Saved '{fragment['_course_name']}'.")
            else:
                print("  Discarded.")

        if input("\nEnter another course? (y/n): ").strip().lower() not in ("y", "s"):
            break

    print("\n✓ Manual entry finished.")


if __name__ == "__main__":
    main()
