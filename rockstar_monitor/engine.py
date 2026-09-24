from __future__ import annotations

from dataclasses import dataclass, field

from .http_client import SafeHttpClient
from .models import ChangeEvent, SourceResult
from .sources import source_from_definition
from .storage import Storage


@dataclass
class CycleSummary:
    checked: int = 0
    successful: int = 0
    failed: int = 0
    events: list[ChangeEvent] = field(default_factory=list)
    results: list[SourceResult] = field(default_factory=list)


class MonitorEngine:
    def __init__(self, storage: Storage, client: SafeHttpClient | None = None):
        self.storage = storage
        self.client = client or SafeHttpClient()

    def run_cycle(self, official_only: bool = False, force: bool = False, progress=None) -> CycleSummary:
        sources = self.storage.due_sources(official_only, force)
        run_id = self.storage.begin_run(official_only)
        summary = CycleSummary()
        for definition in sources:
            if progress: progress(f"Checking {definition.name}…")
            result = source_from_definition(definition).check(self.client, self.storage.cache_for(definition.key))
            summary.checked += 1
            summary.results.append(result)
            self.storage.record_source_result(definition.key, result.status_code, result.error, definition.minimum_interval_minutes, result.retry_after_seconds, result.etag or "", result.last_modified or "", result.content_hash or "")
            if result.error:
                summary.failed += 1
                self.storage.add_activity("ERROR", f"{definition.name} failed — {result.error}", definition.key)
                continue
            summary.successful += 1
            events = [] if result.not_modified else self.storage.apply_observations(definition.key, result.observations, result.status_code or 200, result.content_hash or "")
            summary.events.extend(events)
            self.storage.add_activity("CHANGE" if events else "INFO", f"{definition.name} checked — " + (f"{len(events)} change(s)" if events else "no changes"), definition.key)
        new_count = sum(e.kind == "NEW PRODUCT" for e in summary.events)
        self.storage.finish_run(run_id, summary.checked, summary.successful, summary.failed, new_count, len(summary.events) - new_count)
        return summary

