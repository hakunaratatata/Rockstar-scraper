from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256

import requests

from .models import FetchResult
from .security import normalize_public_url


USER_AGENT = "GTAVIPhysicalProductMonitor/2.0 (personal passive monitor; ordinary public pages only)"


class HttpError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class SafeHttpClient:
    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/json;q=0.9"})

    def get(self, url: str, allowed_hosts: tuple[str, ...], etag: str = "", last_modified: str = "") -> FetchResult:
        current = normalize_public_url(url, allowed_hosts)
        if not current:
            raise HttpError("Source URL failed its HTTPS/domain policy.")
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        for redirect_count in range(6):
            try:
                response = self.session.get(current, headers=headers, timeout=(10, 30), allow_redirects=False)
            except requests.RequestException as exc:
                raise HttpError(f"Network error: {exc}") from exc
            if response.is_redirect or response.is_permanent_redirect:
                if redirect_count == 5:
                    raise HttpError("Too many redirects.", response.status_code)
                target = normalize_public_url(requests.compat.urljoin(current, response.headers.get("Location", "")), allowed_hosts)
                if not target:
                    raise HttpError("Redirect destination failed the source domain policy.", response.status_code)
                current = target
                continue
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            if response.status_code == 304:
                return FetchResult(304, current, "", "", not_modified=True)
            if response.status_code in (403, 429):
                default = 21_600 if response.status_code == 403 else 3_600
                raise HttpError(f"Source returned HTTP {response.status_code}; backing off.", response.status_code, retry_after or default)
            if response.status_code >= 500:
                raise HttpError(f"Source returned HTTP {response.status_code}.", response.status_code, retry_after)
            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                raise HttpError(f"Source returned HTTP {response.status_code}.", response.status_code) from exc
            return FetchResult(response.status_code, current, response.text, sha256(response.content).hexdigest(), response.headers.get("ETag"), response.headers.get("Last-Modified"))
        raise HttpError("Redirect limit exceeded.")


def parse_retry_after(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0, int((parsed - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None

