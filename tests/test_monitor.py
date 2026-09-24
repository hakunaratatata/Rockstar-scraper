import json
import tempfile
import unittest
from pathlib import Path

import requests

from rockstar_monitor.http_client import HttpError, SafeHttpClient, parse_retry_after
from rockstar_monitor.models import Confidence, Observation, Relevance
from rockstar_monitor.security import normalize_public_url
from rockstar_monitor.sources import BUILTIN_SOURCES, PublicPageSource
from rockstar_monitor.storage import SCHEMA_VERSION, Storage


class FakeResponse:
    def __init__(self, status=200, text="ok", headers=None):
        self.status_code=status; self.text=text; self.content=text.encode(); self.headers=headers or {}; self.is_redirect=status in (301,302,303,307,308); self.is_permanent_redirect=status in (301,308)
    def raise_for_status(self):
        if self.status_code>=400: raise requests.HTTPError()


class FakeSession:
    def __init__(self, responses): self.responses=list(responses); self.headers={}; self.calls=[]
    def get(self,url,**kwargs): self.calls.append((url,kwargs)); return self.responses.pop(0)


class SecurityTests(unittest.TestCase):
    def test_rejects_lookalike_hosts(self):
        allowed=("rockstargames.com",)
        self.assertIsNone(normalize_public_url("https://evilrockstargames.com/x",allowed))
        self.assertIsNone(normalize_public_url("https://rockstargames.com.attacker.example/x",allowed))
        self.assertIsNone(normalize_public_url("https://127.0.0.1/x",("127.0.0.1",)))
        self.assertIsNone(normalize_public_url("https://localhost/x",("localhost",)))
        self.assertIsNotNone(normalize_public_url("https://store.rockstargames.com/x",allowed))

    def test_rejects_malicious_redirect(self):
        client=SafeHttpClient(FakeSession([FakeResponse(302,headers={"Location":"https://evil.example/x"})]))
        with self.assertRaises(HttpError): client.get("https://rockstargames.com/",("rockstargames.com",))

    def test_http_statuses_and_retry_after(self):
        for status in (403,429,500):
            with self.subTest(status=status):
                with self.assertRaises(HttpError): SafeHttpClient(FakeSession([FakeResponse(status,headers={"Retry-After":"120"})])).get("https://rockstargames.com/",("rockstargames.com",))
        result=SafeHttpClient(FakeSession([FakeResponse(304)])).get("https://rockstargames.com/",("rockstargames.com",))
        self.assertTrue(result.not_modified); self.assertEqual(parse_retry_after("120"),120)


class ProviderTests(unittest.TestCase):
    def test_json_ld_product_and_irrelevant_product(self):
        html='''<script type="application/ld+json">[{"@type":"Product","name":"Grand Theft Auto VI Physical Collector Box","sku":"VI-1","url":"/product/vi","offers":{"price":"99.99","priceCurrency":"USD","availability":"InStock"}},{"@type":"Product","name":"Red Dead Tee","url":"/product/rdr"}]</script>'''
        items=PublicPageSource(BUILTIN_SOURCES[0]).identify_items(html,"https://store.rockstargames.com/en/","hash")
        self.assertEqual(len(items),1); self.assertEqual(items[0].sku,"VI-1"); self.assertEqual(items[0].price,"99.99")

    def test_malformed_json_is_safe(self):
        self.assertEqual(PublicPageSource(BUILTIN_SOURCES[0]).identify_items('<script type="application/ld+json">{bad</script>',BUILTIN_SOURCES[0].url,"x"),[])

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.store=Storage(Path(self.temp.name)/"monitor.db"); self.store.sync_sources(BUILTIN_SOURCES)
    def tearDown(self): self.temp.cleanup()
    def observation(self,price="10",availability="OutOfStock",preorder=None,sku="sku1"):
        return Observation("rockstar_store","Rockstar Store",BUILTIN_SOURCES[0].url,f"https://store.rockstargames.com/product/{sku}",f"GTA VI Physical Edition {sku}",Confidence.OFFICIAL,Relevance.CONFIRMED,sku=sku,category="PHYSICAL GAME",price=price,currency="USD",availability=availability,preorder_status=preorder)
    def test_schema_and_first_run_baseline(self):
        with self.store.connect() as db: self.assertEqual(db.execute("SELECT version FROM schema_version").fetchone()[0],SCHEMA_VERSION)
        self.assertEqual(self.store.apply_observations("rockstar_store",[self.observation()],200,"a"),[])
        self.assertEqual(self.store.apply_observations("rockstar_store",[self.observation()],200,"a"),[])

    def test_migrates_original_schema_without_destroying_history(self):
        import sqlite3
        path=Path(self.temp.name)/"legacy.db"
        db=sqlite3.connect(path); db.executescript("CREATE TABLE articles(url TEXT PRIMARY KEY,title TEXT NOT NULL,published_at TEXT,first_seen TEXT NOT NULL); CREATE TABLE checks(id INTEGER PRIMARY KEY,checked_at TEXT,status TEXT,message TEXT,article_count INTEGER); CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL); INSERT INTO articles VALUES('https://example.test','Old post',NULL,'2025-01-01');"); db.close()
        migrated=Storage(path)
        self.assertEqual(migrated.recent_articles()[0]["title"],"Old post")
        with migrated.connect() as db: self.assertEqual(db.execute("SELECT version FROM schema_version").fetchone()[0],SCHEMA_VERSION)
    def test_price_availability_and_preorder_changes(self):
        self.store.apply_observations("rockstar_store",[self.observation()],200,"a")
        events=self.store.apply_observations("rockstar_store",[self.observation("12","InStock")],200,"b")
        self.assertEqual(events[0].kind,"PRICE CHANGE")
        events=self.store.apply_observations("rockstar_store",[self.observation("12","InStock","PREORDER")],200,"c")
        self.assertEqual(events[0].kind,"PREORDER OPEN")
    def test_new_product_after_empty_baseline(self):
        self.store.apply_observations("rockstar_store",[],200,"a")
        events=self.store.apply_observations("rockstar_store",[self.observation()],200,"b")
        self.assertEqual(events[0].kind,"NEW PRODUCT")
    def test_removal_requires_three_confirmations(self):
        one,two=self.observation(sku="sku1"),self.observation(sku="sku2")
        self.store.apply_observations("rockstar_store",[one,two],200,"a")
        self.assertEqual(self.store.apply_observations("rockstar_store",[two],200,"b"),[])
        self.assertEqual(self.store.apply_observations("rockstar_store",[two],200,"c"),[])
        events=self.store.apply_observations("rockstar_store",[two],200,"d")
        self.assertEqual(events[0].kind,"PRODUCT REMOVED")
        # Completely empty parses do not count as missing because they may indicate parser failure.


if __name__=="__main__": unittest.main()
