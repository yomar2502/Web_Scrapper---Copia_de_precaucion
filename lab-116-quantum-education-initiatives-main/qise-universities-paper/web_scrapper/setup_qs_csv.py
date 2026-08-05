"""
setup_qs_csv.py — Instala tu CSV de QS Rankings en el lugar correcto.

USO:
  python setup_qs_csv.py ruta/a/tu/archivo.csv

El script detecta automáticamente las columnas y valida que el archivo es correcto.
"""

import csv
import sys
import shutil
from pathlib import Path

DEST = Path("data/qs_rankings.csv")
COUNTRY_TO_CODE = {
    "Argentina": "AR", "Brazil": "BR", "Chile": "CL",
    "Colombia": "CO", "Mexico": "MX", "Peru": "PE",
    "Venezuela": "VE", "Venezuela (Bolivarian Republic of)": "VE",
    "Uruguay": "UY", "Costa Rica": "CR", "Ecuador": "EC",
    "Cuba": "CU", "Bolivia": "BO", "Paraguay": "PY",
}

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("\nEjemplo:")
        print("  python setup_qs_csv.py C:/Users/TuNombre/Downloads/qs_rankings_latam.csv")
        sys.exit(1)

    src = Path(sys.argv[1])
    if not src.exists():
        print(f"ERROR: No se encontró el archivo: {src}")
        sys.exit(1)

    print(f"\nAnalizando: {src}")

    # Leer y validar
    with open(src, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows = list(reader)

    print(f"Columnas detectadas: {headers}")
    print(f"Total de filas: {len(rows)}")

    # Detectar columna de institución y país
    name_col = None
    country_col = None
    for h in headers:
        if any(x in h.lower() for x in ["institution", "university", "name"]):
            name_col = h
        if any(x in h.lower() for x in ["country", "territory", "pais"]):
            country_col = h

    if not name_col or not country_col:
        print(f"\nERROR: No pude detectar las columnas automáticamente.")
        print(f"Columnas disponibles: {headers}")
        print(f"El CSV debe tener una columna con 'institution' y otra con 'country'")
        sys.exit(1)

    print(f"\nColumna universidad: '{name_col}'")
    print(f"Columna país: '{country_col}'")

    # Contar universidades LatAm
    latam_unis = []
    countries_found = set()
    for i, row in enumerate(rows, start=2):
        country = (row.get(country_col) or "").strip()
        name = (row.get(name_col) or "").strip()
        if not country or not name:
            continue
        code = COUNTRY_TO_CODE.get(country)
        if code:
            latam_unis.append({"row": i, "name": name, "country": country, "code": code})
            countries_found.add(code)

    print(f"\nUniversidades LatAm encontradas: {len(latam_unis)}")
    print(f"Países: {sorted(countries_found)}")
    print("\nPrimeras 10 universidades LatAm:")
    for u in latam_unis[:10]:
        print(f"  [{u['row']:4d}] {u['code']} — {u['name']}")

    # Copiar al destino
    DEST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, DEST)
    print(f"\n✓ CSV copiado a: {DEST}")
    print("\nAhora puedes correr el pipeline y el geo_enricher usará tus rankings QS.")
    print("  python main.py --universities-only --limit 50")

if __name__ == "__main__":
    main()
