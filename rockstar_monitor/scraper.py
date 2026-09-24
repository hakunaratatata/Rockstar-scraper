from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .models import Article
from .security import normalize_public_url

DEFAULT_URL = "https://www.rockstargames.com/newswire"
USER_AGENT = "RockstarNewsMonitor/1.0 (personal desktop change monitor)"
ARTICLE_PATH = re.compile(r"/newswire/article/", re.IGNORECASE)


class NotModified(Exception):
    pass


class ScrapeError(RuntimeError):
    def __init__(self, message: str, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _retry_after(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        try:
            seconds = int((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            return max(0, seconds)
        except (TypeError, ValueError, OverflowError):
            return None


def fetch_articles(
    url: str = DEFAULT_URL,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout: int = 30,
) -> tuple[list[Article], dict[str, str]]:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise ScrapeError(f"Network error: {exc}") from exc

    if response.status_code == 304:
        raise NotModified
    if response.status_code == 429:
        raise ScrapeError("Rockstar asked the monitor to slow down (HTTP 429).", _retry_after(response.headers.get("Retry-After")))
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise ScrapeError(f"Rockstar returned HTTP {response.status_code}.") from exc

    articles = parse_articles(response.text, response.url)
    if not articles:
        raise ScrapeError("No Newswire posts were found. Rockstar may have changed the page layout.")

    validators = {
        "etag": response.headers.get("ETag", ""),
        "last_modified": response.headers.get("Last-Modified", ""),
    }
    return articles, validators


def parse_articles(html: str, base_url: str = DEFAULT_URL) -> list[Article]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, Article] = {}

    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(node.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for item in _walk_json(payload):
            if not isinstance(item, dict):
                continue
            raw_url = item.get("url") or item.get("@id")
            title = item.get("headline") or item.get("name")
            if isinstance(raw_url, str) and isinstance(title, str):
                _add(found, title, raw_url, base_url, item.get("datePublished"))

    for link in soup.select("a[href]"):
        raw_url = link.get("href", "")
        title = link.get("aria-label") or link.get("title") or link.get_text(" ", strip=True)
        _add(found, title, raw_url, base_url)

    return list(found.values())


def _walk_json(value: object) -> Iterable[object]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _add(found: dict[str, Article], title: object, raw_url: object, base_url: str, published_at: object = None) -> None:
    if not isinstance(title, str) or not isinstance(raw_url, str):
        return
    clean_title = " ".join(title.split())
    absolute = normalize_public_url(urljoin(base_url, raw_url), ("rockstargames.com",))
    parsed = urlparse(absolute or "")
    if (
        len(clean_title) < 3
        or not ARTICLE_PATH.search(parsed.path)
        or not absolute
    ):
        return
    found.setdefault(absolute, Article(clean_title, absolute, published_at if isinstance(published_at, str) else None))
