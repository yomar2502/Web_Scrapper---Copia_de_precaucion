"""
main.py — CLI entry point for the QISE-LatAm scraper.

Typical use:
    python main.py --input data/universities.csv --output data/qise_candidates.csv

Useful flags:
    --max-depth 2                 crawl depth per institution
    --max-pages-per-domain 100    hard cap on pages fetched per institution
    --download-pdfs true|false    fetch & parse linked PDFs (default: true)
    --country Peru                only process institutions from this country
    --resume true|false           skip institutions already in the output file
    --limit 200                   stop after N fragments (quick smoke test)
    --dry-run                     classify but do not write output files
    --no-cache                    ignore the on-disk download cache
    --no-robots                   do not consult robots.txt (use responsibly)

Input may be CSV or YAML. If --input is omitted the loader falls back to
--sources (default sources.yaml).
"""

import argparse
import sys

from pipeline import Pipeline
from utils import get_logger

logger = get_logger("main")


def _str2bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def parse_args():
    p = argparse.ArgumentParser(
        description="QISE-LatAm scraper: harvest auditable quantum-coursework "
                    "evidence from Latin American universities.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--input", default=None,
                   help="University list (CSV or YAML). Falls back to --sources.")
    p.add_argument("--output", default="data/qise_candidates.csv",
                   help="Output CSV path (a .json sibling is also written).")
    p.add_argument("--config", default="config.yaml", help="Config YAML path.")
    p.add_argument("--sources", default="sources.yaml",
                   help="Fallback source list if --input is not given.")
    p.add_argument("--max-depth", type=int, default=None)
    p.add_argument("--max-pages-per-domain", type=int, default=None)
    p.add_argument("--download-pdfs", type=_str2bool, default=None,
                   metavar="true|false")
    p.add_argument("--country", default=None, help="Filter by country name or code.")
    p.add_argument("--resume", type=_str2bool, default=False, metavar="true|false")
    p.add_argument("--limit", type=int, default=None,
                   help="Stop after N fragments (smoke test).")
    p.add_argument("--dry-run", action="store_true",
                   help="Run without writing output files.")
    p.add_argument("--no-cache", action="store_true",
                   help="Ignore the on-disk download cache.")
    p.add_argument("--no-robots", action="store_true",
                   help="Skip robots.txt checks (use responsibly).")
    p.add_argument("--include-news", action="store_true",
                   help="Also crawl news_sources from the YAML sources file.")
    p.add_argument("--include-social", action="store_true",
                   help="Also query social_sources (needs API tokens).")
    p.add_argument("--discover-seeds-only", action="store_true",
                   help="Run automatic seed discovery, print/write the "
                        "discovered seeds, and exit without crawling.")
    p.add_argument("--auto-discover", type=_str2bool, default=None,
                   metavar="true|false",
                   help="Enable/disable automatic seed discovery for "
                        "institutions without seed URLs (default: config).")
    p.add_argument("--force-discover", action="store_true",
                   help="Run seed discovery even for institutions that have "
                        "manual seed URLs (discovered seeds replace them — "
                        "for evaluating discovery quality).")
    return p.parse_args()


def main():
    args = parse_args()

    overrides = {
        "max_depth": args.max_depth,
        "max_pages_per_university": args.max_pages_per_domain,
        "download_pdfs": args.download_pdfs,
        "use_cache": False if args.no_cache else None,
        "respect_robots": False if args.no_robots else None,
        "auto_discover_seeds": args.auto_discover,
    }

    logger.info(f"Input   : {args.input or args.sources}")
    logger.info(f"Output  : {args.output}")
    logger.info(f"Country : {args.country or 'all'}")
    logger.info(f"Resume  : {args.resume} | dry-run: {args.dry_run} | "
                f"limit: {args.limit or 'none'}")

    try:
        pipeline = Pipeline(
            config_path=args.config,
            input_path=args.input,
            sources_path=args.sources,
            overrides=overrides,
        )

        if args.discover_seeds_only:
            n = pipeline.discover_only(country=args.country,
                                       force=args.force_discover)
            sys.exit(0 if n else 1)

        summary = pipeline.run(
            output_path=args.output,
            dry_run=args.dry_run,
            limit=args.limit,
            country=args.country,
            resume=args.resume,
            include_news=args.include_news,
            include_social=args.include_social,
            force_discover=args.force_discover,
        )

        if summary["candidate_rows"] == 0:
            logger.warning("No candidate evidence found. Check that seed URLs are "
                           "reachable and that --max-depth/--max-pages allow crawling.")
            sys.exit(1)
        sys.exit(0)

    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(2)
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(3)


if __name__ == "__main__":
    main()
