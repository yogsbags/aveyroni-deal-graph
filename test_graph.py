"""Classifier and graph growth, without calling Apify."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from classify import classify
from ingest import ingest_post
from normalize import money_phrase, normalize_post
from store import GraphStore

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "sample_posts.json").read_text())


class MoneyTests(unittest.TestCase):
    def test_ranges_currencies_and_non_tickets(self):
        self.assertEqual(money_phrase("ticket €15-80M in Italy"), "€15M–€80M")
        self.assertEqual(money_phrase("Rs 2.5 Cr/guntha (~Rs 100 Cr)"), "₹100 Cr")
        self.assertEqual(money_phrase("Previously generating £400K+ p.a. Asking £2M. Valued at £4.4M"), "£2M–£4.4M")
        self.assertEqual(money_phrase("a target of 40M hotel guests"), "")


class ClassifyTests(unittest.TestCase):
    def test_founder_sale_is_sell_side(self):
        result = classify(FIXTURES[0]["content"], FIXTURES[0]["author_headline"])
        self.assertEqual(result.side, "sell")
        self.assertEqual(result.person_side, "sell")
        self.assertEqual(result.sector, "Manufacturing")
        self.assertFalse(result.skip)

    def test_advisor_posting_a_sale_stays_an_intermediary(self):
        result = classify(FIXTURES[1]["content"], FIXTURES[1]["author_headline"])
        self.assertEqual(result.side, "sell")
        self.assertEqual(result.person_side, "intermediary")

    def test_acquisition_mandate_is_buy_side(self):
        result = classify(FIXTURES[2]["content"], FIXTURES[2]["author_headline"])
        self.assertEqual(result.side, "buy")
        self.assertEqual(result.person_side, "buy")

    def test_merger_commentary_is_a_signal(self):
        result = classify(FIXTURES[3]["content"], FIXTURES[3]["author_headline"])
        self.assertEqual(result.side, "signal")
        self.assertFalse(result.skip)

    def test_hiring_is_dropped(self):
        result = classify(FIXTURES[4]["content"], FIXTURES[4]["author_headline"])
        self.assertTrue(result.skip)


class ImportTests(unittest.TestCase):
    def test_csv_replaces_the_sample_and_keeps_ids(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        fields = ["id", "kind", "side", "label", "short", "sub", "value", "posted", "url", "profile", "degree", "phone", "email", "status", "note", "seq"]
        with (folder / "nodes.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow({"id": "root", "kind": "root", "label": "Aveyroni Partners", "seq": "1"})
            writer.writerow({"id": "d-fino", "kind": "deal", "side": "sell", "label": "Fino PO", "short": "Fino", "value": "Rs 11 Cr", "status": "Open", "seq": "10"})
            writer.writerow({"id": "p-raahul", "kind": "person", "side": "intermediary", "label": "Raahul Jain", "profile": "https://www.linkedin.com/in/raahul", "degree": "2nd", "phone": "9810000000", "seq": "11"})
        with (folder / "edges.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["id", "from", "from_label", "to", "to_label", "rel", "note"])
            writer.writeheader()
            writer.writerow({"id": "root__d", "from": "root", "from_label": "Aveyroni", "to": "d-fino", "to_label": "Fino", "rel": "sector"})
            writer.writerow({"id": "p-raahul__d-fino", "from": "p-raahul", "from_label": "Raahul", "to": "d-fino", "to_label": "Fino", "rel": "posted"})
        store = GraphStore(folder / "graph.db")
        self.addCleanup(store.close)
        ingest_post(store, FIXTURES[0], "", "sample")
        loaded = store.replace_from_csv(folder / "nodes.csv", folder / "edges.csv")
        self.assertEqual(loaded, {"nodes": 3, "edges": 2})
        labels = [node["label"] for node in store.snapshot()["nodes"]]
        self.assertEqual(labels, ["Aveyroni Partners", "Fino PO", "Raahul Jain"])
        self.assertNotIn("Sample Founder", labels)
        self.assertEqual(store.root_id(), "root")
        raahul = store.get_node("p-raahul")
        self.assertEqual(raahul["phone"], "9810000000")
        self.assertEqual(raahul["degree"], "2nd")


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = GraphStore(Path(self.tmp.name) / "graph.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_sample_posts_grow_the_network_once(self):
        first = [ingest_post(self.store, post, "", "sample") for post in FIXTURES]
        self.assertFalse(first[0]["skipped"])
        self.assertTrue(first[4]["skipped"])
        counts = self.store.counts()
        self.assertEqual(counts["deals"], 4)
        self.assertEqual(counts["sell"], 1)
        self.assertEqual(counts["buy"], 2)
        self.assertEqual(counts["intermediary"], 1)
        self.assertGreaterEqual(counts["matches"], 2)
        again = ingest_post(self.store, FIXTURES[0], "", "sample")
        self.assertTrue(again["skipped"])
        self.assertEqual(self.store.counts()["deals"], 4)

    def test_sample_fixture_keeps_the_author(self):
        post = normalize_post(FIXTURES[0])
        self.assertEqual(post["author_name"], "Sample Founder")
        self.assertEqual(post["author_profile"], "https://www.linkedin.com/in/sample-founder")
        self.assertEqual(post["comments"][0]["profile"], "https://www.linkedin.com/in/sample-buyer")
        result = ingest_post(self.store, post, "", "sample")
        self.assertFalse(result["skipped"])
        labels = [node["label"] for node in self.store.snapshot()["nodes"]]
        self.assertIn("Sample Founder", labels)
        self.assertNotIn("Unknown", labels)

    def test_harvest_payload_normalizes(self):
        post = normalize_post({
            "id": "7328",
            "linkedinUrl": "https://www.linkedin.com/posts/ada_activity-555",
            "content": "Looking to acquire a logistics platform.",
            "author": {
                "name": "Ada Example",
                "info": "Partner",
                "linkedinUrl": "https://www.linkedin.com/in/ada-example",
            },
            "postedAt": {"date": "2026-10-01"},
        })
        self.assertEqual(post["author_name"], "Ada Example")
        result = ingest_post(self.store, post, "Logistics")
        self.assertEqual(result["side"], "buy")
        self.assertEqual(self.store.counts()["deals"], 1)


if __name__ == "__main__":
    unittest.main()
