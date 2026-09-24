from __future__ import annotations

from abc import ABC, abstractmethod
from hashlib import sha256
import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .http_client import HttpError, SafeHttpClient
from .models import Confidence, Observation, Relevance, SourceDefinition, SourceResult
from .security import normalize_public_url


STRONG_RELEVANCE = re.compile(r"\b(grand\s+theft\s+auto\s+(?:vi|6)|gta\s*(?:vi|6)|gtavi)\b", re.I)
PHYSICAL_TERMS = re.compile(r"\b(physical|collector|limited|special edition|merch|apparel|shirt|tee|hat|cap|collectible|statue|figure|accessor|keychain|sticker|vinyl|cd|album|controller|console|bundle|code-in-box|pre[- ]?order)\b", re.I)
PRICE = re.compile(r"(?P<symbol>[$£€])\s?(?P<price>\d+(?:[.,]\d{2})?)")
CURRENCY = {"$": "USD", "£": "GBP", "€": "EUR"}


BUILTIN_SOURCES = (
    SourceDefinition("rockstar_store", "Rockstar Store", "official", "https://store.rockstargames.com/en/", Confidence.OFFICIAL, ("store.rockstargames.com",), 30),
    SourceDefinition("rockstar_newswire", "Rockstar Newswire", "official", "https://www.rockstargames.com/newswire", Confidence.OFFICIAL, ("rockstargames.com",), 30),
    SourceDefinition("rockstar_gtavi", "Rockstar GTA VI", "official", "https://www.rockstargames.com/VI", Confidence.OFFICIAL, ("rockstargames.com",), 30),
    SourceDefinition("playstation_gtavi", "PlayStation GTA VI", "official", "https://www.playstation.com/en-us/games/grand-theft-auto-vi/", Confidence.OFFICIAL, ("playstation.com",), 30),
)


class MonitorSource(ABC):
    def __init__(self, definition: SourceDefinition):
        self.definition = definition

    def check(self, client: SafeHttpClient, cache: dict[str, str]) -> SourceResult:
        try:
            fetched = client.get(self.definition.url, self.definition.allowed_hosts, cache.get("etag", ""), cache.get("last_modified", ""))
        except HttpError as exc:
            return SourceResult(self.definition, status_code=exc.status_code, error=str(exc), retry_after_seconds=exc.retry_after_seconds)
        if fetched.not_modified:
            return SourceResult(self.definition, status_code=304, not_modified=True)
        observations = self.identify_items(fetched.text, fetched.final_url, fetched.content_hash)
        return SourceResult(self.definition, observations, fetched.status_code, fetched.content_hash, "json-ld+html", etag=fetched.etag, last_modified=fetched.last_modified)

    @abstractmethod
    def identify_items(self, text: str, final_url: str, content_hash: str) -> list[Observation]: ...


class PublicPageSource(MonitorSource):
    def identify_items(self, text: str, final_url: str, content_hash: str) -> list[Observation]:
        soup = BeautifulSoup(text, "html.parser")
        candidates: list[dict[str, object]] = []
        for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                payload = json.loads(node.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            for item in walk_json(payload):
                if isinstance(item, dict) and str(item.get("@type", "")).lower() in {"product", "newsarticle", "article", "offer", "creativework"}:
                    candidates.append(item)
        for link in soup.select("a[href]"):
            title = " ".join((link.get("aria-label") or link.get("title") or link.get_text(" ", strip=True)).split())
            if STRONG_RELEVANCE.search(title) and (PHYSICAL_TERMS.search(title) or self.definition.key in {"rockstar_newswire", "rockstar_gtavi"}):
                candidates.append({"@type": "Article", "name": title, "url": link.get("href")})
        page_text = " ".join(soup.get_text(" ", strip=True).split())
        if STRONG_RELEVANCE.search(page_text) and (PHYSICAL_TERMS.search(page_text) or self.definition.key == "rockstar_gtavi"):
            title = soup.title.get_text(" ", strip=True) if soup.title else self.definition.name
            candidates.append({"@type": "WebPage", "name": title, "url": final_url, "description": page_text[:1000]})
        return self._normalize(candidates, final_url)

    def _normalize(self, candidates: list[dict[str, object]], final_url: str) -> list[Observation]:
        output: dict[str, Observation] = {}
        for item in candidates:
            name = clean(item.get("name") or item.get("headline"))
            description = clean(item.get("description"))
            combined = f"{name or ''} {description or ''}"
            if not name or not STRONG_RELEVANCE.search(combined):
                continue
            raw_url = item.get("url") or item.get("@id") or final_url
            if isinstance(raw_url, dict):
                raw_url = raw_url.get("url") or raw_url.get("@id")
            canonical = normalize_public_url(urljoin(final_url, str(raw_url)), self.definition.allowed_hosts)
            if not canonical:
                continue
            offer = item.get("offers") if isinstance(item.get("offers"), dict) else {}
            price = clean(offer.get("price") if offer else item.get("price"))
            currency = clean(offer.get("priceCurrency") if offer else item.get("priceCurrency"))
            availability = clean(offer.get("availability") if offer else item.get("availability"))
            page_price = PRICE.search(combined)
            if not price and page_price:
                price, currency = page_price.group("price").replace(",", "."), CURRENCY.get(page_price.group("symbol"))
            possible = not PHYSICAL_TERMS.search(combined) and self.definition.key != "rockstar_store"
            obs = Observation(
                source_key=self.definition.key, source_name=self.definition.name, source_url=self.definition.url,
                canonical_url=canonical, name=name, confidence=self.definition.confidence,
                relevance=Relevance.POSSIBLE if possible else Relevance.CONFIRMED,
                product_id=clean(item.get("productID")), sku=clean(item.get("sku")), category=categorize(combined),
                price=price, currency=currency, availability=availability,
                preorder_status="PREORDER" if re.search(r"pre[- ]?order", combined, re.I) else None,
                release_date=clean(item.get("releaseDate") or item.get("datePublished")), image_url=image_value(item.get("image")),
                description=(description or "")[:1000] or None, variants=compact_json(item.get("isVariantOf") or item.get("hasVariant")),
                purchase_limit=clean(item.get("eligibleQuantity")), weight=compact_json(item.get("weight")),
                dimensions=compact_json(item.get("depth") or item.get("width") or item.get("height")),
                parser="json-ld" if item.get("@type") != "WebPage" else "html", evidence_excerpt=combined[:500],
                raw_fingerprint=sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest(),
            )
            output[obs.identity()] = obs
        return list(output.values())


def source_from_definition(definition: SourceDefinition) -> MonitorSource:
    return PublicPageSource(definition)


def walk_json(value: object):
    yield value
    if isinstance(value, dict):
        for child in value.values(): yield from walk_json(child)
    elif isinstance(value, list):
        for child in value: yield from walk_json(child)


def clean(value: object) -> str | None:
    if value is None or isinstance(value, (dict, list)): return None
    return " ".join(str(value).split()) or None


def compact_json(value: object) -> str | None:
    if value is None: return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) if isinstance(value, (dict, list)) else clean(value)


def image_value(value: object) -> str | None:
    if isinstance(value, str): return value
    if isinstance(value, list) and value: return image_value(value[0])
    if isinstance(value, dict): return clean(value.get("url") or value.get("contentUrl"))
    return None


def categorize(text: str) -> str:
    categories = (("MUSIC", r"\b(vinyl|album|soundtrack|cd)\b"), ("HARDWARE", r"\b(controller|console|bundle|dualsense)\b"), ("COLLECTOR/SPECIAL EDITION", r"\b(collector|limited|special edition)\b"), ("APPAREL", r"\b(apparel|shirt|tee|hat|cap|hoodie)\b"), ("MERCH", r"\b(merch|collectible|statue|figure|keychain|sticker|accessor)"), ("PHYSICAL GAME", r"\b(physical|code-in-box)\b"))
    for category, pattern in categories:
        if re.search(pattern, text, re.I): return category
    return "OTHER"
