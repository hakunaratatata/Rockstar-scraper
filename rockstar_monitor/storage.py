from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Article, ChangeEvent, Confidence, Observation, SourceDefinition

SCHEMA_VERSION = 2
TRACKED_FIELDS = ("name", "category", "price", "currency", "availability", "preorder_status", "release_date", "image_url", "description", "variants", "purchase_limit", "weight", "dimensions")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return result


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        try:
            with self.connect() as db:
                db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
                row = db.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
                if row is None:
                    db.execute("INSERT INTO schema_version(version) VALUES (0)")
                    version = 0
                else:
                    version = row[0]
                self._migrate(db, version)
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(f"The monitoring database could not be opened: {exc}") from exc

    def _migrate(self, db: sqlite3.Connection, version: int) -> None:
        if version < 1:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS articles (url TEXT PRIMARY KEY, title TEXT NOT NULL, published_at TEXT, first_seen TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS checks (id INTEGER PRIMARY KEY AUTOINCREMENT, checked_at TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL, article_count INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
        if version < 2:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sources (
                    key TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, url TEXT NOT NULL,
                    confidence TEXT NOT NULL, allowed_hosts TEXT NOT NULL, minimum_interval_minutes INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, last_checked TEXT, last_success TEXT, last_http_status INTEGER,
                    next_eligible TEXT, error TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS products (
                    product_key TEXT PRIMARY KEY, source_key TEXT NOT NULL, source_name TEXT NOT NULL, source_url TEXT NOT NULL,
                    canonical_url TEXT NOT NULL, product_id TEXT, sku TEXT, name TEXT NOT NULL, category TEXT,
                    price TEXT, currency TEXT, availability TEXT, preorder_status TEXT, release_date TEXT, image_url TEXT,
                    description TEXT, variants TEXT, purchase_limit TEXT, weight TEXT, dimensions TEXT,
                    confidence TEXT NOT NULL, relevance TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    last_changed TEXT NOT NULL, fingerprint TEXT NOT NULL, missing_count INTEGER NOT NULL DEFAULT 0,
                    removed INTEGER NOT NULL DEFAULT 0, FOREIGN KEY(source_key) REFERENCES sources(key)
                );
                CREATE TABLE IF NOT EXISTS product_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, product_key TEXT NOT NULL, observed_at TEXT NOT NULL,
                    normalized_json TEXT NOT NULL, http_status INTEGER, content_hash TEXT, parser TEXT,
                    evidence_excerpt TEXT, FOREIGN KEY(product_key) REFERENCES products(product_key)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, kind TEXT NOT NULL,
                    product_key TEXT NOT NULL, product_name TEXT NOT NULL, source_key TEXT NOT NULL, confidence TEXT NOT NULL,
                    previous_json TEXT, current_json TEXT, changed_fields TEXT NOT NULL, notified INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS check_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT,
                    official_only INTEGER NOT NULL DEFAULT 0, sources_checked INTEGER NOT NULL DEFAULT 0,
                    successful_sources INTEGER NOT NULL DEFAULT 0, failed_sources INTEGER NOT NULL DEFAULT 0,
                    new_products INTEGER NOT NULL DEFAULT 0, changed_products INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, level TEXT NOT NULL,
                    source_key TEXT, message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS http_cache (
                    source_key TEXT PRIMARY KEY, etag TEXT, last_modified TEXT, content_hash TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_observations_product ON product_observations(product_key, observed_at DESC);
            """)
        db.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))

    def sync_sources(self, definitions: tuple[SourceDefinition, ...]) -> None:
        with self.connect() as db:
            for source in definitions:
                db.execute("""INSERT INTO sources(key,name,kind,url,confidence,allowed_hosts,minimum_interval_minutes,enabled)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET name=excluded.name,kind=excluded.kind,
                    url=excluded.url,confidence=excluded.confidence,allowed_hosts=excluded.allowed_hosts,
                    minimum_interval_minutes=excluded.minimum_interval_minutes""",
                    (source.key, source.name, source.kind, source.url, source.confidence.value, json.dumps(source.allowed_hosts), source.minimum_interval_minutes, int(source.enabled)))

    def source_rows(self) -> list[sqlite3.Row]:
        with self.connect() as db: return db.execute("SELECT * FROM sources ORDER BY name").fetchall()

    def set_source_enabled(self, key: str, enabled: bool) -> None:
        with self.connect() as db: db.execute("UPDATE sources SET enabled=? WHERE key=?", (int(enabled), key))

    def add_source(self, source: SourceDefinition) -> None:
        self.sync_sources((source,))

    def due_sources(self, official_only: bool = False, force: bool = False) -> list[SourceDefinition]:
        now = utcnow()
        query = "SELECT * FROM sources WHERE enabled=1"
        args: list[object] = []
        if official_only: query += " AND confidence=?"; args.append(Confidence.OFFICIAL.value)
        if not force: query += " AND (next_eligible IS NULL OR next_eligible<=?)"; args.append(now)
        with self.connect() as db: rows = db.execute(query + " ORDER BY name", args).fetchall()
        return [SourceDefinition(r["key"], r["name"], r["kind"], r["url"], Confidence(r["confidence"]), tuple(json.loads(r["allowed_hosts"])), r["minimum_interval_minutes"], bool(r["enabled"])) for r in rows]

    def cache_for(self, key: str) -> dict[str, str]:
        with self.connect() as db: row = db.execute("SELECT * FROM http_cache WHERE source_key=?", (key,)).fetchone()
        return dict(row) if row else {}

    def record_source_result(self, key: str, status: int | None, error: str | None, minimum_minutes: int, retry_after: int | None, etag: str = "", last_modified: str = "", content_hash: str = "") -> None:
        now = datetime.now(timezone.utc)
        cooldown = max(minimum_minutes * 60, retry_after or 0)
        with self.connect() as db:
            db.execute("""UPDATE sources SET last_checked=?,last_success=CASE WHEN ? IS NULL THEN ? ELSE last_success END,
                last_http_status=?,next_eligible=?,error=?,consecutive_failures=CASE WHEN ? IS NULL THEN 0 ELSE consecutive_failures+1 END WHERE key=?""",
                (now.isoformat(), error, now.isoformat(), status, (now + timedelta(seconds=cooldown)).isoformat(), error, error, key))
            if not error and status != 304:
                db.execute("INSERT INTO http_cache(source_key,etag,last_modified,content_hash) VALUES(?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET etag=excluded.etag,last_modified=excluded.last_modified,content_hash=excluded.content_hash", (key, etag, last_modified, content_hash))

    def begin_run(self, official_only: bool) -> int:
        with self.connect() as db:
            return db.execute("INSERT INTO check_runs(started_at,official_only) VALUES(?,?)", (utcnow(), int(official_only))).lastrowid

    def finish_run(self, run_id: int, checked: int, successes: int, failures: int, new: int, changed: int) -> None:
        with self.connect() as db: db.execute("UPDATE check_runs SET finished_at=?,sources_checked=?,successful_sources=?,failed_sources=?,new_products=?,changed_products=? WHERE id=?", (utcnow(), checked, successes, failures, new, changed, run_id))

    def apply_observations(self, source_key: str, observations: list[Observation], http_status: int, content_hash: str) -> list[ChangeEvent]:
        now = utcnow(); events: list[ChangeEvent] = []; seen: set[str] = set()
        with self.connect() as db:
            baseline_key = f"source_baseline:{source_key}"
            baseline = db.execute("SELECT value FROM metadata WHERE key=?", (baseline_key,)).fetchone() is not None
            for obs in observations:
                key = obs.identity(); seen.add(key); current = obs.normalized(); old_row = db.execute("SELECT * FROM products WHERE product_key=?", (key,)).fetchone()
                old = dict(old_row) if old_row else None
                changed_fields = [field for field in TRACKED_FIELDS if old and old.get(field) != current.get(field)]
                kind = None
                if not old: kind = "NEW PRODUCT" if baseline else None
                elif changed_fields:
                    if "preorder_status" in changed_fields and current.get("preorder_status") == "PREORDER": kind = "PREORDER OPEN"
                    elif "price" in changed_fields: kind = "PRICE CHANGE"
                    elif "availability" in changed_fields:
                        value = str(current.get("availability") or "").lower(); kind = "SOLD OUT" if any(x in value for x in ("outofstock", "sold out", "unavailable")) else "AVAILABLE"
                    else: kind = "PRODUCT UPDATED"
                values = [key, obs.source_key, obs.source_name, obs.source_url, obs.canonical_url, obs.product_id, obs.sku, obs.name, obs.category, obs.price, obs.currency, obs.availability, obs.preorder_status, obs.release_date, obs.image_url, obs.description, obs.variants, obs.purchase_limit, obs.weight, obs.dimensions, obs.confidence.value, obs.relevance.value]
                if old:
                    db.execute("""UPDATE products SET source_name=?,source_url=?,canonical_url=?,product_id=?,sku=?,name=?,category=?,price=?,currency=?,availability=?,preorder_status=?,release_date=?,image_url=?,description=?,variants=?,purchase_limit=?,weight=?,dimensions=?,confidence=?,relevance=?,last_seen=?,last_changed=?,fingerprint=?,missing_count=0,removed=0 WHERE product_key=?""", (*values[2:], now, now if changed_fields else old["last_changed"], obs.fingerprint(), key))
                else:
                    db.execute("""INSERT INTO products(product_key,source_key,source_name,source_url,canonical_url,product_id,sku,name,category,price,currency,availability,preorder_status,release_date,image_url,description,variants,purchase_limit,weight,dimensions,confidence,relevance,first_seen,last_seen,last_changed,fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (*values, now, now, now, obs.fingerprint()))
                db.execute("INSERT INTO product_observations(product_key,observed_at,normalized_json,http_status,content_hash,parser,evidence_excerpt) VALUES(?,?,?,?,?,?,?)", (key, now, json.dumps(current, ensure_ascii=False), http_status, content_hash, obs.parser, obs.evidence_excerpt))
                if kind:
                    event = ChangeEvent(kind, key, obs.name, source_key, obs.confidence.value, {f: old.get(f) for f in TRACKED_FIELDS} if old else None, current, changed_fields)
                    self._insert_event(db, event, now); events.append(event)
            if observations:
                missing = db.execute("SELECT product_key,name,confidence,missing_count FROM products WHERE source_key=? AND removed=0", (source_key,)).fetchall()
                for row in missing:
                    if row["product_key"] in seen: continue
                    count = row["missing_count"] + 1
                    db.execute("UPDATE products SET missing_count=? WHERE product_key=?", (count, row["product_key"]))
                    if count >= 3:
                        event = ChangeEvent("PRODUCT REMOVED", row["product_key"], row["name"], source_key, row["confidence"], None, None, ["removed"])
                        db.execute("UPDATE products SET removed=1,last_changed=? WHERE product_key=?", (now, row["product_key"])); self._insert_event(db, event, now); events.append(event)
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)", (baseline_key, now))
            db.execute("DELETE FROM product_observations WHERE observed_at < datetime('now','-90 days')")
        return events

    def _insert_event(self, db: sqlite3.Connection, event: ChangeEvent, now: str) -> None:
        db.execute("INSERT INTO events(created_at,kind,product_key,product_name,source_key,confidence,previous_json,current_json,changed_fields) VALUES(?,?,?,?,?,?,?,?,?)", (now,event.kind,event.product_key,event.product_name,event.source_key,event.confidence,json.dumps(event.previous),json.dumps(event.current),json.dumps(event.changed_fields)))

    def add_activity(self, level: str, message: str, source_key: str | None = None) -> None:
        with self.connect() as db: db.execute("INSERT INTO activity(created_at,level,source_key,message) VALUES(?,?,?,?)", (utcnow(),level,source_key,message))

    def product_rows(self, limit: int = 500) -> list[sqlite3.Row]:
        with self.connect() as db: return db.execute("SELECT * FROM products ORDER BY last_changed DESC LIMIT ?", (limit,)).fetchall()

    def event_rows(self, limit: int = 300) -> list[sqlite3.Row]:
        with self.connect() as db: return db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def activity_rows(self, limit: int = 300) -> list[sqlite3.Row]:
        with self.connect() as db: return db.execute("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def latest_run(self) -> sqlite3.Row | None:
        with self.connect() as db: return db.execute("SELECT * FROM check_runs ORDER BY id DESC LIMIT 1").fetchone()

    def export_json(self, path: Path) -> None:
        payload = {"products": [dict(r) for r in self.product_rows()], "events": [dict(r) for r in self.event_rows(10000)]}
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def export_csv(self, path: Path) -> None:
        rows = [dict(r) for r in self.product_rows(10000)]
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["product_key"]); writer.writeheader(); writer.writerows(rows)

    # Original Newswire compatibility methods.
    def has_baseline(self) -> bool: return self.get_metadata("baseline_complete") == "1"
    def save_articles(self, articles: list[Article]) -> list[Article]:
        now=utcnow(); new=[]
        with self.connect() as db:
            for a in articles:
                if not db.execute("SELECT 1 FROM articles WHERE url=?",(a.url,)).fetchone(): db.execute("INSERT INTO articles VALUES(?,?,?,?)",(a.url,a.title,a.published_at,now)); new.append(a)
                else: db.execute("UPDATE articles SET title=?,published_at=COALESCE(?,published_at) WHERE url=?",(a.title,a.published_at,a.url))
            db.execute("INSERT OR REPLACE INTO metadata VALUES('baseline_complete','1')")
        return new
    def recent_articles(self, limit: int=100):
        with self.connect() as db: return db.execute("SELECT * FROM articles ORDER BY first_seen DESC LIMIT ?",(limit,)).fetchall()
    def record_check(self,status: str,message: str,article_count: int=0):
        with self.connect() as db: db.execute("INSERT INTO checks(checked_at,status,message,article_count) VALUES(?,?,?,?)",(utcnow(),status,message,article_count))
    def recent_checks(self,limit: int=30):
        with self.connect() as db: return db.execute("SELECT * FROM checks ORDER BY id DESC LIMIT ?",(limit,)).fetchall()
    def get_metadata(self,key: str)->str:
        with self.connect() as db: row=db.execute("SELECT value FROM metadata WHERE key=?",(key,)).fetchone(); return row["value"] if row else ""
    def set_metadata(self,key: str,value: str):
        with self.connect() as db: db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)",(key,value))
