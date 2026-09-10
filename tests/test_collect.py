"""
Offline verification for collect.py's parsing / scoring / spike-detection logic.
The sandbox this was written in cannot reach GDELT/Google News/Reddit directly
(network egress policy), so this test feeds realistic sample payloads straight
into the parsing/aggregation functions instead of hitting the network - it does
NOT prove the live HTTP calls succeed, only that the logic built around their
response shapes is correct. Verify the real HTTP calls by checking the first
GitHub Actions run once deployed.
"""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock as _mock
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import collect  # noqa: E402


SAMPLE_GDELT_JSON = json.dumps({
    "articles": [
        {"title": "Curfew imposed in Srinagar after clashes", "url": "https://example.com/a1",
         "domain": "example.com", "seendate": "20260910T060000Z"},
        {"title": "Record tourist footfall boosts Kashmir economy", "url": "https://example.com/a2",
         "domain": "example.com", "seendate": "20260910T061500Z"},
    ]
}).encode("utf-8")

SAMPLE_GNEWS_RSS = """<?xml version="1.0"?>
<rss><channel>
<item>
  <title>Landslide blocks Jammu-Srinagar highway - Greater Kashmir</title>
  <link>https://greaterkashmir.com/x1</link>
  <pubDate>Wed, 10 Sep 2026 06:00:00 GMT</pubDate>
  <source url="https://greaterkashmir.com">Greater Kashmir</source>
</item>
<item>
  <title>Pahalgam sees festival crowds this weekend - Daily Excelsior</title>
  <link>https://dailyexcelsior.com/x2</link>
  <pubDate>Wed, 10 Sep 2026 07:00:00 GMT</pubDate>
  <source url="https://dailyexcelsior.com">Daily Excelsior</source>
</item>
</channel></rss>"""

SAMPLE_REDDIT_JSON = json.dumps({
    "data": {"children": [
        {"data": {"title": "Anyone else worried about the internet shutdown in Kashmir?",
                   "permalink": "/r/india/comments/x/", "subreddit": "india",
                   "created_utc": 1757484000}},
    ]}
}).encode("utf-8")


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestParsing(unittest.TestCase):
    def setUp(self):
        # IMPORTANT: several functions under test (append_history,
        # update_search_index) write to collect.HISTORY_PATH /
        # collect.SEARCH_INDEX_PATH. Those point at this repo's real
        # data/history.jsonl and data/search_index.json by default, so
        # without this redirect, running these tests against a deployed
        # copy of this repo would silently overwrite real accumulated data.
        # Always run tests against temp paths, never the real data/ files.
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patchers = [
            _mock.patch.object(collect, "HISTORY_PATH", os.path.join(self._tmpdir.name, "history.jsonl")),
            _mock.patch.object(collect, "SEARCH_INDEX_PATH", os.path.join(self._tmpdir.name, "search_index.json")),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._tmpdir.cleanup()

    def test_gdelt_parse(self):
        with unittest_mock_urlopen(SAMPLE_GDELT_JSON):
            arts = collect.fetch_gdelt("Kashmir")
        self.assertEqual(len(arts), 2)
        self.assertEqual(arts[0]["source"], "gdelt")
        self.assertIn("Curfew", arts[0]["title"])

    def test_google_news_rss_parse(self):
        with unittest_mock_urlopen(SAMPLE_GNEWS_RSS.encode("utf-8")):
            arts = collect.fetch_google_news_rss("Kashmir")
        self.assertEqual(len(arts), 2)
        self.assertEqual(arts[0]["domain"], "Greater Kashmir")
        self.assertNotIn(" - Greater Kashmir", arts[0]["title"])

    def test_reddit_parse(self):
        with unittest_mock_urlopen(SAMPLE_REDDIT_JSON):
            arts = collect.fetch_reddit("Kashmir")
        self.assertEqual(len(arts), 1)
        self.assertTrue(arts[0]["domain"].startswith("r/"))

    def test_sentiment_direction_and_lexicon(self):
        neg = collect.score_text("Curfew imposed after deadly clashes and encounter")
        pos = collect.score_text("Record tourist footfall boosts Kashmir, peace and development")
        self.assertLess(neg, -0.3)
        self.assertGreater(pos, 0.3)

    def test_keyphrase_extraction_skips_region_terms(self):
        titles = ["Jammu and Kashmir sees Amarnath Yatra records", "Amarnath Yatra concludes peacefully"]
        counts = collect.extract_keyphrases(titles)
        self.assertIn("Amarnath Yatra", counts)
        self.assertNotIn("Jammu and Kashmir", counts)

    def test_spike_detection_flags_unusual_jump(self):
        history = [
            {"ts": f"t{i}", "keyword_counts": {"Amarnath Yatra": 1}, "overall_mood": 0.0, "total_mentions": 10}
            for i in range(10)
        ]
        current = {"Amarnath Yatra": 9}
        spikes = collect.compute_spikes(current, history, min_mentions=3)
        terms = [s["term"] for s in spikes]
        self.assertIn("Amarnath Yatra", terms)

    def test_spike_detection_flags_brand_new_topic(self):
        history = [{"ts": "t0", "keyword_counts": {}, "overall_mood": 0.0, "total_mentions": 5}]
        current = {"Fresh Incident": 6}
        spikes = collect.compute_spikes(current, history, min_mentions=3)
        reasons = {s["term"]: s["reason"] for s in spikes}
        self.assertEqual(reasons.get("Fresh Incident"), "new_topic")

    def test_no_data_does_not_crash_history(self):
        rows = []
        collect.append_history({"ts": "t0", "no_data": True, "keyword_counts": {},
                                 "overall_mood": None, "total_mentions": 0}, rows)
        self.assertTrue(os.path.exists(collect.HISTORY_PATH))

    def test_article_day_parses_each_source_format(self):
        self.assertEqual(collect.article_day(
            {"source": "gdelt", "seendate": "20260910T060000Z"}, "fallback"), "2026-09-10")
        self.assertEqual(collect.article_day(
            {"source": "google_news", "seendate": "Wed, 10 Sep 2026 06:00:00 GMT"}, "fallback"), "2026-09-10")
        self.assertEqual(collect.article_day(
            {"source": "youtube", "seendate": "2026-09-10T06:00:00Z"}, "fallback"), "2026-09-10")
        self.assertEqual(collect.article_day({"source": "gdelt", "seendate": ""}, "fallback"), "fallback")

    def test_search_index_dedupes_and_prunes_by_age(self):
        old = [{"title": "old", "url": "https://x/old", "domain": "d", "source": "gdelt",
                "date": "2000-01-01", "seendate": "", "sentiment": 0.0, "districts": [], "keywords": []}]
        new = [{"title": "fresh", "url": "https://x/new", "domain": "d", "source": "gdelt",
                "date": "2026-09-10", "seendate": "", "sentiment": 0.1, "districts": ["Srinagar"],
                "keywords": ["Fresh Topic"]}]
        merged = collect.update_search_index(new, old)
        urls = {a["url"] for a in merged}
        self.assertIn("https://x/new", urls)
        self.assertNotIn("https://x/old", urls)  # older than the 30-day window

    def test_search_index_updates_existing_url_instead_of_duplicating(self):
        existing = [{"title": "v1", "url": "https://x/same", "domain": "d", "source": "gdelt",
                     "date": "2026-09-09", "seendate": "", "sentiment": 0.0, "districts": [], "keywords": []}]
        updated = [{"title": "v2", "url": "https://x/same", "domain": "d", "source": "gdelt",
                    "date": "2026-09-10", "seendate": "", "sentiment": 0.2, "districts": [], "keywords": []}]
        merged = collect.update_search_index(updated, existing)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["title"], "v2")


def unittest_mock_urlopen(body):
    return _mock.patch("urllib.request.urlopen", return_value=FakeResponse(body))


if __name__ == "__main__":
    unittest.main()