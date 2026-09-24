from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from hashlib import sha256
import json


class Confidence(StrEnum):
    OFFICIAL = "OFFICIAL"
    RETAILER = "RETAILER"
    SECONDARY_REPORT = "SECONDARY REPORT"
    UNVERIFIED = "UNVERIFIED"


class Relevance(StrEnum):
    CONFIRMED = "CONFIRMED"
    POSSIBLE = "POSSIBLE MATCH"


@dataclass(frozen=True)
class Observation:
    source_key: str
    source_name: str
    source_url: str
    canonical_url: str
    name: str
    confidence: Confidence
    relevance: Relevance
    product_id: str | None = None
    sku: str | None = None
    category: str | None = None
    price: str | None = None
    currency: str | None = None
    availability: str | None = None
    preorder_status: str | None = None
    release_date: str | None = None
    image_url: str | None = None
    description: str | None = None
    variants: str | None = None
    purchase_limit: str | None = None
    weight: str | None = None
    dimensions: str | None = None
    parser: str = "html"
    evidence_excerpt: str | None = None
    raw_fingerprint: str = ""

    def identity(self) -> str:
        return f"{self.source_key}:{self.sku or self.product_id or self.canonical_url}"

    def normalized(self) -> dict[str, object]:
        value = asdict(self)
        value["confidence"] = self.confidence.value
        value["relevance"] = self.relevance.value
        return value

    def fingerprint(self) -> str:
        if self.raw_fingerprint:
            return self.raw_fingerprint
        ignored = {"raw_fingerprint", "evidence_excerpt", "parser", "source_url"}
        payload = {key: value for key, value in self.normalized().items() if key not in ignored}
        return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class SourceDefinition:
    key: str
    name: str
    kind: str
    url: str
    confidence: Confidence
    allowed_hosts: tuple[str, ...]
    minimum_interval_minutes: int
    enabled: bool = True


@dataclass
class FetchResult:
    status_code: int
    final_url: str
    text: str
    content_hash: str
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


@dataclass
class SourceResult:
    source: SourceDefinition
    observations: list[Observation] = field(default_factory=list)
    status_code: int | None = None
    content_hash: str | None = None
    parser: str | None = None
    error: str | None = None
    retry_after_seconds: int | None = None
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None


@dataclass
class ChangeEvent:
    kind: str
    product_key: str
    product_name: str
    source_key: str
    confidence: str
    previous: dict[str, object] | None
    current: dict[str, object] | None
    changed_fields: list[str]


@dataclass(frozen=True)
class Article:
    title: str
    url: str
    published_at: str | None = None
