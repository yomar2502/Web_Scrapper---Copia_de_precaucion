"""
geo_enricher.py — Geopolitical and research-capacity metadata per country.
 
Fuentes de datos:
  1. Tu CSV de QS Rankings (columna A = ranking, col B = Institution Name, col C = Country)
     → Cargado automáticamente desde data/qs_rankings.csv
     → El sistema toma el MEJOR ranking (número más bajo) por país
 
  2. World Bank API (gratis, sin auth)
     → GDP per capita, R&D expenditure, internet penetration, researchers/million
 
  3. Datos manuales (IBM Q Network, Scimago, Quantum Initiatives)
     → Difíciles de obtener por API, actualizables anualmente
"""
 
import csv
import json
import time
from pathlib import Path
 
import requests
 
from utils import get_logger
 
logger = get_logger("geo_enricher")
 
WORLD_BANK_API = "https://api.worldbank.org/v2"
 
# Mapeo de nombres de país del CSV QS → código ISO
COUNTRY_TO_CODE = {
    "Argentina": "AR",
    "Brazil": "BR",
    "Chile": "CL",
    "Colombia": "CO",
    "Mexico": "MX",
    "Peru": "PE",
    "Venezuela": "VE",
    "Venezuela (Bolivarian Republic of)": "VE",
    "Uruguay": "UY",
    "Costa Rica": "CR",
    "Ecuador": "EC",
    "Bolivia": "BO",
    "Paraguay": "PY",
    "Cuba": "CU",
    "Panama": "PA",
    "Guatemala": "GT",
    "Honduras": "HN",
    "El Salvador": "SV",
    "Nicaragua": "NI",
    "Dominican Republic": "DO",
    "Puerto Rico": "PR",
}
 
# ── DATOS MANUALES (cosas difíciles de obtener por API) ───────────────────────
# Actualizar anualmente. Fuentes: IBM Q Network, Scimago 2024, UNESCO
MANUAL_DATA = {
    "AR": {
        "times_ranking_top_university": 601,
        "scimago_country_rank_physics": 32,
        "ibm_quantum_network_member": True,
        "national_quantum_initiative": False,
        "latam_quantum_alliance_member": True,
    },
    "BR": {
        "times_ranking_top_university": 501,
        "scimago_country_rank_physics": 13,
        "ibm_quantum_network_member": True,
        "national_quantum_initiative": False,
        "latam_quantum_alliance_member": True,
    },
    "CL": {
        "times_ranking_top_university": 601,
        "scimago_country_rank_physics": 39,
        "ibm_quantum_network_member": False,
        "national_quantum_initiative": False,
        "latam_quantum_alliance_member": True,
    },
    "CO": {
        "times_ranking_top_university": 801,
        "scimago_country_rank_physics": 47,
        "ibm_quantum_network_member": False,
        "national_quantum_initiative": False,
        "latam_quantum_alliance_member": True,
    },
    "MX": {
        "times_ranking_top_university": 601,
        "scimago_country_rank_physics": 26,
        "ibm_quantum_network_member": True,
        "national_quantum_initiative": False,
        "latam_quantum_alliance_member": True,
    },
    "PE": {
        "times_ranking_top_university": 1001,
        "scimago_country_rank_physics": 62,
        "ibm_quantum_network_member": False,
        "national_quantum_initiative": False,
        "latam_quantum_alliance_member": False,
    },
    "VE": {
        "times_ranking_top_university": 1001,
        "scimago_country_rank_physics": 71,
        "ibm_quantum_network_member": False,
        "national_quantum_initiative": False,
        "latam_quantum_alliance_member": False,
    },
    "UY": {
        "times_ranking_top_university": 1001,
        "scimago_country_rank_physics": 58,
        "ibm_quantum_network_member": False,
        "national_quantum_initiative": False,
        "latam_quantum_alliance_member": False,
    },
    "CR": {
        "times_ranking_top_university": 1001,
        "scimago_country_rank_physics": 78,
        "ibm_quantum_network_member": False,
        "national_quantum_initiative": False,
        "latam_quantum_alliance_member": False,
    },
    "EC": {
        "times_ranking_top_university": 1001,
        "scimago_country_rank_physics": 85,
        "ibm_quantum_network_member": False,
        "national_quantum_initiative": False,
        "latam_quantum_alliance_member": False,
    },
    "CU": {
        "times_ranking_top_university": 1001,
        "scimago_country_rank_physics": 90,
        "ibm_quantum_network_member": False,
        "national_quantum_initiative": False,
        "latam_quantum_alliance_member": False,
    },
}
 
WB_INDICATORS = {
    "gdp_per_capita_usd": "NY.GDP.PCAP.CD",
    "rd_expenditure_pct_gdp": "GB.XPD.RSDV.GD.ZS",
    "internet_penetration_pct": "IT.NET.USER.ZS",
    "researchers_per_million": "SP.POP.SCIE.RD.P6",
}
 
 
class GeoEnricher:
 
    def __init__(self, cfg: dict, cache_path: str = "data/processed/wb_cache.json"):
        self.cfg = cfg
        self.cache_path = Path(cache_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache = self._load_cache()
        self.session = requests.Session()
        self.session.headers["User-Agent"] = cfg["scraper"]["user_agent"]
 
        # Cargar QS rankings desde tu CSV
        self.qs_by_country = self._load_qs_csv()
 
    # ── API PÚBLICA ───────────────────────────────────────────────────────────
 
    def enrich_dataset(self, country_codes: list[str]) -> dict[str, dict]:
        result = {}
        for code in set(country_codes):
            result[code] = self._get_country_data(code)
        return result
 
    def save_geo_metadata(self, country_codes: list[str], output_path: str) -> None:
        data = self.enrich_dataset(country_codes)
        if not data:
            return
 
        all_fields = set()
        for row in data.values():
            all_fields.update(row.keys())
        fieldnames = ["country_code"] + sorted(all_fields - {"country_code"})
 
        with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for code, row in sorted(data.items()):
                writer.writerow({"country_code": code, **row})
 
        logger.info(f"Geo metadata guardado: {output_path} ({len(data)} países)")
 
    # ── CARGA DE TU CSV QS ────────────────────────────────────────────────────
 
    def _load_qs_csv(self) -> dict[str, dict]:
        """
        Lee tu CSV de QS Rankings.
        Estructura esperada:
          - La fila del encabezado tiene "Institution Name" en col B, "Country" en col C
          - El número de ranking QS es el número de fila (col A cuando se exporta a CSV)
          - O bien hay una columna numérica de ranking
 
        Devuelve dict: country_code → {qs_top_rank, qs_top_university, qs_universities_count}
        """
        qs_path = Path("data/qs_rankings.csv")
        if not qs_path.exists():
            logger.warning(
                "No se encontró data/qs_rankings.csv — usando rankings manuales.\n"
                "  Para usar tu CSV: copia el archivo a data/qs_rankings.csv"
            )
            return {}
 
        by_country: dict[str, list] = {}
 
        # Lee con múltiples encodings + errors=replace para tolerar bytes corruptos
        import io as _io
        raw = None
        for enc in ["utf-8-sig", "utf-8", "latin-1", "cp1252"]:
            try:
                candidate = open(qs_path, encoding=enc, errors="replace").read()
                replacements = candidate.count("\ufffd")
                logger.debug(f"Encoding {enc}: {replacements} bytes reemplazados")
                if raw is None or replacements < raw[1]:
                    raw = (candidate, replacements)
            except Exception as e:
                logger.debug(f"Encoding {enc} falló: {e}")
        if raw is None:
            logger.error("No se pudo leer el CSV QS")
            return {}
        raw_text, bad_bytes = raw
        logger.info(f"QS CSV leído ({bad_bytes} bytes corruptos ignorados)")
 
        reader = csv.DictReader(_io.StringIO(raw_text))
        headers = reader.fieldnames or []
        logger.info(f"QS CSV columnas detectadas: {headers}")
 
        rank_col    = self._find_col(headers, ["rank", "ranking", "#", "position"])
        name_col    = self._find_col(headers, ["institution name", "institution", "university", "name"])
        country_col = self._find_col(headers, ["country", "country/territory", "territory", "pais"])
 
        if not name_col or not country_col:
            logger.error(
                f"No pude detectar columnas en QS CSV. Columnas: {headers}\n"
                "  Necesito una columna 'Institution Name' y una 'Country/Territory'"
            )
            return {}
 
        logger.info(f"QS CSV: rank='{rank_col}' name='{name_col}' country='{country_col}'")
 
        for i, row in enumerate(reader, start=2):
            country_str = (row.get(country_col) or "").strip()
            name_str    = (row.get(name_col) or "").strip()
            if not country_str or not name_str:
                continue
 
            if rank_col and row.get(rank_col):
                rank_str = str(row[rank_col]).strip().replace("+", "").replace("=", "")
                try:
                    rank = int(rank_str.split("-")[0])
                except ValueError:
                    rank = i
            else:
                rank = i
 
            code = COUNTRY_TO_CODE.get(country_str)
            if not code:
                for k, v in COUNTRY_TO_CODE.items():
                    if k.lower() in country_str.lower() or country_str.lower() in k.lower():
                        code = v
                        break
            if not code:
                continue
 
            if code not in by_country:
                by_country[code] = []
            by_country[code].append({"rank": rank, "name": name_str})
 
        # Resumir por país: mejor ranking + cantidad de universidades rankeadas
        result = {}
        for code, unis in by_country.items():
            unis_sorted = sorted(unis, key=lambda x: x["rank"])
            result[code] = {
                "qs_top_rank": unis_sorted[0]["rank"],
                "qs_top_university": unis_sorted[0]["name"],
                "qs_universities_in_ranking": len(unis),
            }
            logger.info(
                f"QS {code}: top={unis_sorted[0]['rank']} ({unis_sorted[0]['name']}), "
                f"total={len(unis)} universidades"
            )
 
        logger.info(f"QS CSV cargado: {len(result)} países, {sum(len(v) for v in by_country.values())} universidades")
        return result
 
    @staticmethod
    def _find_col(headers: list[str], candidates: list[str]) -> str | None:
        """Busca la primera columna cuyo nombre contenga alguno de los candidatos."""
        for h in headers:
            for c in candidates:
                if c.lower() in h.lower():
                    return h
        return None
 
    # ── ENSAMBLE DE DATOS POR PAÍS ─────────────────────────────────────────────
 
    def _get_country_data(self, country_code: str) -> dict:
        wb_data  = self._fetch_worldbank(country_code)
        manual   = MANUAL_DATA.get(country_code, {})
        qs_data  = self.qs_by_country.get(country_code, {})
 
        # Prioridad: tu CSV QS > datos manuales
        return {
            "country_code": country_code,
            **wb_data,
            **manual,
            **qs_data,   # sobreescribe qs_world_ranking_top_university si existe en manual
        }
 
    # ── WORLD BANK ─────────────────────────────────────────────────────────────
 
    def _fetch_worldbank(self, country_code: str) -> dict:
        result = {}
        for var_name, indicator in WB_INDICATORS.items():
            cache_key = f"{country_code}_{indicator}"
            if cache_key in self._cache:
                result[var_name] = self._cache[cache_key]
                continue
            value = self._wb_api_call(country_code, indicator)
            self._cache[cache_key] = value
            result[var_name] = value
            time.sleep(0.5)
        self._save_cache()
        return result
 
    def _wb_api_call(self, country_code: str, indicator: str) -> float | None:
        url = f"{WORLD_BANK_API}/country/{country_code}/indicator/{indicator}"
        params = {"format": "json", "mrv": 5, "per_page": 5}
        try:
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if len(data) < 2 or not data[1]:
                return None
            for entry in data[1]:
                if entry.get("value") is not None:
                    return round(float(entry["value"]), 4)
        except Exception as e:
            logger.warning(f"World Bank API {country_code}/{indicator}: {e}")
        return None
 
    def _load_cache(self) -> dict:
        if self.cache_path.exists():
            try:
                return json.loads(self.cache_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}
 
    def _save_cache(self) -> None:
        try:
            self.cache_path.write_text(
                json.dumps(self._cache, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"No pude guardar cache WB: {e}")