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
import unittest
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


import unittest.mock as _mock  # noqa: E402


def unittest_mock_urlopen(body):
    return _mock.patch("urllib.request.urlopen", return_value=FakeResponse(body))


if __name__ == "__main__":
    unittest.main()
