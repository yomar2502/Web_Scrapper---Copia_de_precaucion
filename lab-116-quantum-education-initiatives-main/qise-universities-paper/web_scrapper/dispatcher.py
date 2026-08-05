"""
dispatcher.py — Routes each source config entry to the right crawler,
then passes raw content to the right extractor.

Returns a unified stream of extracted text records ready for classification.
"""

import os
from typing import Generator

from crawler import WebCrawler, RSSCrawler, TwitterCrawler, RedditCrawler
from extractor import (
    extract_from_html,
    extract_from_pdf,
    extract_from_xlsx,
    extract_from_rss_entry,
    extract_from_tweet,
    extract_from_reddit_post,
)
from utils import get_logger

logger = get_logger("dispatcher")


class Dispatcher:

    def __init__(self, cfg: dict, sources: dict):
        self.cfg = cfg
        self.sources = sources

        # Instantiate crawlers
        self.web_crawler = WebCrawler(cfg)
        self.rss_crawler = RSSCrawler(cfg)

        # Twitter: read bearer token from environment variable
        twitter_token = os.environ.get("TWITTER_BEARER_TOKEN")
        if not twitter_token:
            logger.warning(
                "TWITTER_BEARER_TOKEN not set. Twitter source will be skipped.\n"
                "  → Set it with: export TWITTER_BEARER_TOKEN=your_token_here"
            )
        self.twitter_crawler = TwitterCrawler(cfg, bearer_token=twitter_token)

        # Reddit: optional PRAW credentials
        praw_cfg = None
        if os.environ.get("REDDIT_CLIENT_ID"):
            praw_cfg = {
                "client_id": os.environ["REDDIT_CLIENT_ID"],
                "client_secret": os.environ.get("REDDIT_CLIENT_SECRET", ""),
            }
        self.reddit_crawler = RedditCrawler(cfg, praw_cfg=praw_cfg)

    def stream_all_records(self) -> Generator[dict, None, None]:
        """
        Main entry point. Yields extracted text records from all source types.
        """
        yield from self._stream_universities()
        yield from self._stream_news()
        yield from self._stream_social()

    def _stream_universities(self) -> Generator[dict, None, None]:
        universities = self.sources.get("universities", [])
        logger.info(f"Dispatching {len(universities)} universities")
        for univ in universities:
            if univ.get("type") != "web":
                continue

            
            for raw in self.web_crawler.crawl_university(univ):
                records = self._extract_raw(raw, univ)
                yield from records

    def _stream_news(self) -> Generator[dict, None, None]:
        news_sources = self.sources.get("news_sources", [])
        logger.info(f"Dispatching {len(news_sources)} news/RSS sources")
        for source in news_sources:
            if source.get("type") == "rss":
                for raw in self.rss_crawler.crawl_feed(source):
                    entry = raw["content"]
                    record = extract_from_rss_entry(entry, source["name"])
                    if record["raw_text"]:
                        yield record
            elif source.get("type") == "web":
                # Treat news web pages like university pages (no university metadata)
                dummy_univ = {
                    "name": source["name"],
                    "country": "",
                    "country_code": "",
                    "base_url": source["url"],
                    "catalog_urls": [source["url"]],
                }
                for raw in self.web_crawler.crawl_university(dummy_univ):
                    records = self._extract_raw(raw, dummy_univ)
                    yield from records

    def _stream_social(self) -> Generator[dict, None, None]:
        social_sources = self.sources.get("social_sources", [])
        logger.info(f"Dispatching {len(social_sources)} social sources")

        for source in social_sources:
            source_type = source.get("type")

            if source_type == "twitter":
                for raw in self.twitter_crawler.crawl_queries(source):
                    tweet = raw["content"]
                    record = extract_from_tweet(tweet, raw.get("search_query", ""))
                    if record["raw_text"]:
                        yield record

            elif source_type == "reddit":
                for raw in self.reddit_crawler.crawl_source(source):
                    post = raw["content"]
                    record = extract_from_reddit_post(post, raw.get("subreddit", ""))
                    if record["raw_text"]:
                        yield record

            elif source_type == "linkedin":
                logger.warning(
                    f"LinkedIn source '{source['name']}' skipped. "
                    "LinkedIn blocks automated scraping. "
                    "Use LinkedIn API (requires app approval) or manual collection."
                )

    def _extract_raw(self, raw: dict, university: dict) -> list[dict]:
        """Route a raw crawler output to the correct extractor."""
        raw_type = raw.get("type")
        url = raw.get("url", "")
        content = raw.get("content", "")
        found_on = raw.get("found_on", "")

        if raw_type == "html":
            return extract_from_html(content, url, university, found_on_page=found_on)

        elif raw_type == "pdf":
            pdf_bytes = content if isinstance(content, bytes) else content.encode()
            return extract_from_pdf(pdf_bytes, url, university, found_on_page=found_on)

        elif raw_type in ("xlsx", "xls"):
            doc_bytes = content if isinstance(content, bytes) else content.encode()
            return extract_from_xlsx(doc_bytes, url, university, found_on_page=found_on)

        else:
            logger.warning(f"Unknown raw type: {raw_type} for URL {url}")
            return []

