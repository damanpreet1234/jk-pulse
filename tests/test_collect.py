"""
Offline verification for collect.py's parsing / scoring / spike-detection logic.
The sandbox this was written in cannot reach GDELT/Google News/Reddit directly
(network egress policy), so this test feeds realistic sample payloads straight
into the parsing/aggregation functions instead of hitting the network - it does
NOT prove the live HTTP calls succeed, only that the logic built around their
response shapes is correct. Verify the real HTTP calls by checking the first
GitHub Actions run once deployed.

IMPORTANT: every function here that writes a file takes an explicit `path`
argument (there are no more module-level HISTORY_PATH / SEARCH_INDEX_PATH
constants to patch). Tests always pass a tempfile path - never a real path
under this repo's data/ - so running this suite can never overwrite
accumulated real data.
"""
import json
import os
import statistics
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

SAMPLE_PK_GNEWS_RSS = """<?xml version="1.0"?>
<rss><channel>
<item>
  <title>Talks resume between India and Pakistan officials - Dawn</title>
  <link>https://dawn.com/p1</link>
  <pubDate>Wed, 10 Sep 2026 06:00:00 GMT</pubDate>
  <source url="https://dawn.com">Dawn</source>
</item>
<item>
  <title>Punjab assembly passes new budget - The News International</title>
  <link>https://thenews.com.pk/p2</link>
  <pubDate>Wed, 10 Sep 2026 07:00:00 GMT</pubDate>
  <source url="https://thenews.com.pk">The News International</source>
</item>
</channel></rss>"""


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def unittest_mock_urlopen(body):
    return _mock.patch("urllib.request.urlopen", return_value=FakeResponse(body))


class TestParsing(unittest.TestCase):
    def test_gdelt_parse(self):
        with unittest_mock_urlopen(SAMPLE_GDELT_JSON):
            arts = collect.fetch_gdelt("Kashmir")
        self.assertEqual(len(arts), 2)
        self.assertEqual(arts[0]["source"], "gdelt")
        self.assertIn("Curfew", arts[0]["title"])

    def test_gdelt_extra_operator_is_appended_to_query(self):
        captured = {}

        def _fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return FakeResponse(SAMPLE_GDELT_JSON)

        with _mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            collect.fetch_gdelt("Pakistan", extra=" sourcecountry:pakistan")
        self.assertIn("sourcecountry%3Apakistan", captured["url"])

    def test_google_news_rss_parse(self):
        with unittest_mock_urlopen(SAMPLE_GNEWS_RSS.encode("utf-8")):
            arts = collect.fetch_google_news_rss("Kashmir")
        self.assertEqual(len(arts), 2)
        self.assertEqual(arts[0]["domain"], "Greater Kashmir")
        self.assertNotIn(" - Greater Kashmir", arts[0]["title"])

    def test_google_news_rss_uses_requested_edition(self):
        captured = {}

        def _fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return FakeResponse(SAMPLE_PK_GNEWS_RSS.encode("utf-8"))

        with _mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            collect.fetch_google_news_rss("Pakistan", gl="PK", ceid="PK:en")
        self.assertIn("gl=PK", captured["url"])
        self.assertIn("ceid=PK%3Aen", captured["url"])

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

    def test_sentiment_multiword_phrases_actually_affect_score(self):
        # Regression test for the VADER phrase-substitution fix: a sentence whose
        # ONLY charged content is a multi-word lexicon phrase must not score neutral.
        neg = collect.score_text("Officials confirm a border skirmish overnight")
        pos = collect.score_text("Officials confirm a goodwill gesture today")
        self.assertLess(neg, -0.2)
        self.assertGreater(pos, 0.2)

    def test_india_pakistan_relations_lexicon(self):
        neg = collect.score_text("Escalation and provocation reported along the border")
        pos = collect.score_text("De-escalation and goodwill mark the summit")
        self.assertLess(neg, -0.2)
        self.assertGreater(pos, 0.2)

    def test_successful_counter_terror_operation_is_not_scored_negative(self):
        # Regression test: "terrorist"/"militant" are (correctly) strongly
        # negative words in the lexicon, but a headline reporting security
        # forces eliminating them is conventionally a successful-operation
        # story, not bad news - it should NOT come out strongly negative
        # just because those words appear.
        score = collect.score_text("Security forces eliminate 2 terrorists in Baramulla encounter")
        self.assertGreaterEqual(score, 0.3)

        score2 = collect.score_text("3 militants killed in overnight gunfight with security forces")
        self.assertGreaterEqual(score2, 0.3)

    def test_security_casualty_headlines_still_score_negative(self):
        # The success-framing override must NOT suppress genuinely bad news -
        # if security personnel or civilians are also reported killed in the
        # same headline, it must stay negative regardless of the
        # terrorist/militant wording.
        score = collect.score_text("Terrorists attack army camp, 3 soldiers martyred")
        self.assertLess(score, -0.3)

        score2 = collect.score_text("2 militants killed but 1 jawan martyred in fierce gunfight")
        self.assertLess(score2, 0)

    def test_unrelated_headlines_are_unaffected_by_security_success_override(self):
        # Sanity check the override is narrowly scoped - ordinary negative
        # news with no terrorist/militant-casualty framing should score
        # exactly as before.
        score = collect.score_text("Curfew imposed after deadly clashes and encounter")
        self.assertLess(score, -0.3)

    def test_keyphrase_extraction_skips_excluded_terms(self):
        titles = ["Jammu and Kashmir sees Amarnath Yatra records", "Amarnath Yatra concludes peacefully"]
        counts = collect.extract_keyphrases(titles, exclude=["Jammu and Kashmir", "J&K", "Jammu", "Kashmir"])
        self.assertIn("Amarnath Yatra", counts)
        self.assertNotIn("Jammu and Kashmir", counts)

    def test_keyphrase_extraction_with_no_exclude_excludes_nothing(self):
        # Note: the capitalized-run regex only matches consecutive capitalized
        # words, so "Jammu and Kashmir" (lowercase "and" in the middle) is
        # picked up as two separate phrases, "Jammu" and "Kashmir" - that's
        # the exclude_terms list's job to filter out in the real pipeline.
        counts = collect.extract_keyphrases(["Amarnath Yatra sees record turnout"])
        self.assertIn("Amarnath Yatra", counts)

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

    def test_article_day_parses_each_source_format(self):
        self.assertEqual(collect.article_day(
            {"source": "gdelt", "seendate": "20260910T060000Z"}, "fallback"), "2026-09-10")
        self.assertEqual(collect.article_day(
            {"source": "google_news", "seendate": "Wed, 10 Sep 2026 06:00:00 GMT"}, "fallback"), "2026-09-10")
        self.assertEqual(collect.article_day(
            {"source": "youtube", "seendate": "2026-09-10T06:00:00Z"}, "fallback"), "2026-09-10")
        self.assertEqual(collect.article_day({"source": "gdelt", "seendate": ""}, "fallback"), "fallback")

    def test_mood_breakdown_empty_and_populated(self):
        empty = collect.mood_breakdown([])
        self.assertIsNone(empty["score"])
        self.assertEqual(empty["sample_size"], 0)

        populated = collect.mood_breakdown([0.6, 0.0, -0.6])
        self.assertEqual(populated["sample_size"], 3)
        self.assertAlmostEqual(populated["positive_pct"], 33.3, places=1)
        self.assertAlmostEqual(populated["negative_pct"], 33.3, places=1)


class TestHistoryAndSearchIndex(unittest.TestCase):
    """These all take an explicit path argument now - always a tempfile path,
    never anything under this repo's real data/ directory."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _path(self, name):
        return os.path.join(self._tmpdir.name, name)

    def test_no_data_does_not_crash_history(self):
        history_path = self._path("history.jsonl")
        rows = []
        collect.append_history(history_path, {"ts": "t0", "no_data": True, "keyword_counts": {},
                                                "overall_mood": None, "total_mentions": 0}, rows)
        self.assertTrue(os.path.exists(history_path))

    def test_append_history_then_load_history_roundtrip(self):
        history_path = self._path("history.jsonl")
        rows = collect.load_history(history_path)  # doesn't exist yet -> []
        self.assertEqual(rows, [])
        collect.append_history(history_path, {"ts": "t0", "keyword_counts": {"X": 1},
                                                "overall_mood": 0.1, "total_mentions": 1}, rows)
        reloaded = collect.load_history(history_path)
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0]["keyword_counts"], {"X": 1})

    def test_search_index_dedupes_and_prunes_by_age(self):
        idx_path = self._path("search_index.json")
        old = [{"title": "old", "url": "https://x/old", "domain": "d", "source": "gdelt",
                "date": "2000-01-01", "seendate": "", "sentiment": 0.0, "districts": [], "keywords": []}]
        new = [{"title": "fresh", "url": "https://x/new", "domain": "d", "source": "gdelt",
                "date": "2026-09-10", "seendate": "", "sentiment": 0.1, "districts": ["Srinagar"],
                "keywords": ["Fresh Topic"]}]
        merged = collect.update_search_index(idx_path, new, old, "districts")
        urls = {a["url"] for a in merged}
        self.assertIn("https://x/new", urls)
        self.assertNotIn("https://x/old", urls)  # older than the 30-day window
        # and it should have actually been written to disk at idx_path
        self.assertEqual(collect.load_search_index(idx_path), merged)

    def test_search_index_updates_existing_url_instead_of_duplicating(self):
        idx_path = self._path("search_index.json")
        existing = [{"title": "v1", "url": "https://x/same", "domain": "d", "source": "gdelt",
                     "date": "2026-09-09", "seendate": "", "sentiment": 0.0, "districts": [], "keywords": []}]
        updated = [{"title": "v2", "url": "https://x/same", "domain": "d", "source": "gdelt",
                    "date": "2026-09-10", "seendate": "", "sentiment": 0.2, "districts": [], "keywords": []}]
        merged = collect.update_search_index(idx_path, updated, existing, "districts")
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["title"], "v2")

    def test_search_index_carries_extra_fields(self):
        idx_path = self._path("search_index.json")
        arts = [{"title": "t", "url": "https://x/1", "domain": "d", "source": "gdelt",
                 "date": "2026-09-10", "seendate": "", "sentiment": 0.0, "provinces": ["Punjab"],
                 "keywords": [], "india_related": True}]
        merged = collect.update_search_index(idx_path, arts, [], "provinces", extra_fields=("india_related",))
        self.assertEqual(merged[0]["india_related"], True)


class TestClusterSimilarStories(unittest.TestCase):
    def test_merges_verbatim_wire_copy_across_outlets(self):
        # The clean case: many outlets running the exact same wire copy -
        # this is the "50 outlets, 1 real story" case that would otherwise
        # inflate negative/positive-coverage percentages as if 50 separate
        # things happened.
        articles = [
            {"title": "Security forces eliminate 2 terrorists in Baramulla encounter",
             "url": f"https://v/{i}", "domain": f"Outlet{i}", "date": "2026-09-29", "sentiment": 0.4}
            for i in range(5)
        ]
        merged = collect.cluster_similar_stories(articles)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["source_count"], 5)
        self.assertEqual(len(merged[0]["also_reported_by"]), 4)

    def test_reduces_heavily_paraphrased_real_world_duplicates(self):
        # Real headlines (from an actual run) covering one event - outlets
        # paraphrase to very different degrees, so this is a harder case
        # than verbatim wire copy. The matcher is deliberately conservative
        # (see cluster_similar_stories docstring): it catches the outlets
        # that share enough vocabulary or the same named entities, which
        # meaningfully reduces the count even when it can't perfectly merge
        # every paraphrase into a single entry.
        articles = [
            {"title": "India rejects Kashmir references in Turkish President's UNGA address as 'unwarranted'",
             "url": "https://a/1", "domain": "The Hindu", "date": "2026-09-29", "sentiment": -0.4,
             "keywords": ["Turkish President"]},
            {"title": "'No locus standi': India rejects Turkey President Erdogan's remarks on Kashmir at UNGA",
             "url": "https://a/2", "domain": "The Times of India", "date": "2026-09-29", "sentiment": -0.5,
             "keywords": ["Turkey President Erdogan"]},
            {"title": "India rejects Turkish President Erdogan's 'unwarranted' references to J&K at UNGA",
             "url": "https://a/3", "domain": "News On AIR", "date": "2026-09-29", "sentiment": -0.35,
             "keywords": ["Turkish President Erdogan"]},
            {"title": "'Unwarranted': India rejects references to Kashmir issue in Turkish president's UNGA address",
             "url": "https://a/4", "domain": "Deccan Herald", "date": "2026-09-29", "sentiment": -0.45,
             "keywords": ["Turkish"]},
        ]
        merged = collect.cluster_similar_stories(articles)
        # 4 raw articles reduced to 2 distinct entries, not left as 4 -
        # the 3 that share enough vocabulary/entities merge into one.
        self.assertEqual(len(merged), 2)
        source_counts = sorted(m["source_count"] for m in merged)
        self.assertEqual(source_counts, [1, 3])
        merged_one = next(m for m in merged if m["source_count"] == 3)
        self.assertAlmostEqual(merged_one["sentiment"], statistics.mean([-0.4, -0.35, -0.45]), places=3)

    def test_does_not_merge_unrelated_same_day_stories_sharing_only_generic_terms(self):
        # Two genuinely different India/Kashmir-UNGA stories about
        # different foreign leaders would otherwise look deceptively
        # similar if "India"/"UNGA"/"Kashmir" counted as real signal - they
        # must NOT merge just because both mention those ubiquitous terms.
        articles = [
            {"title": "India rejects Turkish President Erdogan's Kashmir remarks at UNGA",
             "url": "https://d/1", "domain": "Dawn", "date": "2026-09-29", "sentiment": -0.4,
             "keywords": ["Turkish President Erdogan"]},
            {"title": "India slams Pakistan PM Sharif's Kashmir remarks at UNGA session",
             "url": "https://d/2", "domain": "The Nation", "date": "2026-09-29", "sentiment": -0.3,
             "keywords": ["Sharif"]},
        ]
        merged = collect.cluster_similar_stories(articles)
        self.assertEqual(len(merged), 2)

    def test_keeps_genuinely_distinct_stories_separate(self):
        articles = [
            {"title": "Record tourist footfall boosts Kashmir economy",
             "url": "https://b/1", "domain": "Kashmir Observer", "date": "2026-09-29", "sentiment": 0.5},
            {"title": "Landslide blocks Jammu-Srinagar highway near Ramban",
             "url": "https://b/2", "domain": "Greater Kashmir", "date": "2026-09-29", "sentiment": -0.4},
            {"title": "Scholarship scheme launched for students in Anantnag",
             "url": "https://b/3", "domain": "Daily Excelsior", "date": "2026-09-29", "sentiment": 0.4},
        ]
        merged = collect.cluster_similar_stories(articles)
        self.assertEqual(len(merged), 3)
        self.assertTrue(all(m["source_count"] == 1 for m in merged))
        self.assertTrue(all(m["also_reported_by"] == [] for m in merged))

    def test_does_not_merge_same_headline_template_across_different_days(self):
        # Same/similar headline template, but on different dates - these are
        # two distinct daily occurrences (e.g. a recurring weather advisory),
        # not duplicate coverage of one event, so they must stay separate.
        articles = [
            {"title": "Landslide blocks Jammu-Srinagar highway",
             "url": "https://c/1", "domain": "Greater Kashmir", "date": "2026-09-28", "sentiment": -0.4},
            {"title": "Landslide blocks Jammu-Srinagar highway",
             "url": "https://c/2", "domain": "Greater Kashmir", "date": "2026-09-29", "sentiment": -0.4},
        ]
        merged = collect.cluster_similar_stories(articles)
        self.assertEqual(len(merged), 2)


class TestRegionConfig(unittest.TestCase):
    def test_jk_and_pakistan_regions_present_with_distinct_keys(self):
        keys = {r["key"] for r in collect.REGIONS}
        self.assertEqual(keys, {"jk", "pakistan"})

    def test_jk_area_query_disambiguates_with_jammu_or_kashmir(self):
        q = collect._jk_area_query("Baramulla")
        self.assertIn("Jammu OR Kashmir", q)
        self.assertIn("Baramulla", q)

    def test_pakistan_area_query_disambiguates_punjab_and_islamabad(self):
        self.assertIn("Pakistan", collect._pk_area_query("Punjab"))
        self.assertIn("Pakistan", collect._pk_area_query("Islamabad"))
        # a province with no ambiguity (e.g. Sindh) should NOT get a disambiguator appended
        self.assertEqual(collect._pk_area_query("Sindh").strip(), '"Sindh"')

    def test_pakistan_region_has_india_related_flag_and_terms(self):
        pk = next(r for r in collect.REGIONS if r["key"] == "pakistan")
        self.assertTrue(pk["india_related"])
        self.assertIn("India", pk["india_terms"])
        jk = next(r for r in collect.REGIONS if r["key"] == "jk")
        self.assertFalse(jk["india_related"])


class TestRunRegionEndToEnd(unittest.TestCase):
    """Mocked end-to-end pass through run_region() for both regions, since this
    sandbox can't reach GDELT/Google News/Reddit directly. Every fetcher is
    patched to return small, realistic, hand-built article lists so we can
    confirm run_region()'s aggregation/tagging/india-tone/file-writing logic
    is wired correctly - not that the live HTTP calls succeed."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_run_region_jk_writes_expected_snapshot_shape(self):
        region = dict(next(r for r in collect.REGIONS if r["key"] == "jk"))
        region["data_dir"] = os.path.join(self._tmpdir.name, "jk")
        region["areas"] = ["Srinagar", "Jammu"]  # keep it small/fast for the test

        broad = [{"title": "Curfew imposed in Srinagar after clashes", "url": "https://e/1",
                  "domain": "example.com", "seendate": "20260910T060000Z", "source": "gdelt"}]
        srinagar_arts = [{"title": "Srinagar sees peaceful festival crowds", "url": "https://e/2",
                           "domain": "example.com", "seendate": "20260910T070000Z", "source": "gdelt"}]

        def _fake_gdelt(query, timespan=collect.GDELT_TIMESPAN, maxrecords=75, extra=""):
            if "Srinagar" in query:
                return list(srinagar_arts)
            if '"Jammu"' in query:
                # the per-area query for the "Jammu" district itself - no extra hits
                return []
            return list(broad)

        with _mock.patch.object(collect, "fetch_gdelt", side_effect=_fake_gdelt), \
             _mock.patch.object(collect, "fetch_google_news_rss", return_value=[]), \
             _mock.patch.object(collect, "fetch_reddit", return_value=[]), \
             _mock.patch.object(collect, "fetch_youtube", return_value=[]):
            collect.run_region(region, youtube_key=None)

        latest_path = os.path.join(region["data_dir"], "latest.json")
        self.assertTrue(os.path.exists(latest_path))
        with open(latest_path) as f:
            snapshot = json.load(f)

        self.assertEqual(snapshot["region"], "Jammu & Kashmir")
        self.assertIsNone(snapshot["india_tone"])  # jk region never computes this
        self.assertIn("Srinagar", snapshot["areas"])
        self.assertGreaterEqual(snapshot["areas"]["Srinagar"]["mentions"], 1)
        self.assertTrue(os.path.exists(os.path.join(region["data_dir"], "history.jsonl")))
        self.assertTrue(os.path.exists(os.path.join(region["data_dir"], "search_index.json")))

    def test_run_region_collapses_duplicate_wire_coverage_before_scoring(self):
        # End-to-end version of the dedup fix: many outlets running the
        # exact same wire story about one district must count as ONE
        # story in the written snapshot/search index, not N - this is the
        # actual bug report ("1 negative story picked up by 50 outlets
        # shouldn't count as 50 negative developments").
        region = dict(next(r for r in collect.REGIONS if r["key"] == "jk"))
        region["data_dir"] = os.path.join(self._tmpdir.name, "jk_dedup")
        region["areas"] = ["Srinagar"]

        duplicated_story = [
            {"title": "Curfew imposed in Srinagar after clashes", "url": f"https://dup/{i}",
             "domain": f"Outlet{i}", "seendate": "20260910T060000Z", "source": "gdelt"}
            for i in range(6)
        ]

        with _mock.patch.object(collect, "fetch_gdelt", return_value=list(duplicated_story)), \
             _mock.patch.object(collect, "fetch_google_news_rss", return_value=[]), \
             _mock.patch.object(collect, "fetch_reddit", return_value=[]), \
             _mock.patch.object(collect, "fetch_youtube", return_value=[]):
            collect.run_region(region, youtube_key=None)

        with open(os.path.join(region["data_dir"], "latest.json")) as f:
            snapshot = json.load(f)
        with open(os.path.join(region["data_dir"], "search_index.json")) as f:
            index = json.load(f)

        # 6 near-identical articles from 6 outlets -> 1 distinct story
        self.assertEqual(snapshot["overall_mood"]["sample_size"], 1)
        self.assertEqual(snapshot["run_stats"]["raw_articles_before_dedup"], 6)
        self.assertEqual(len(index), 1)
        self.assertEqual(index[0]["source_count"], 6)
        self.assertEqual(len(index[0]["also_reported_by"]), 5)

    def test_run_region_pakistan_computes_separate_india_tone(self):
        region = dict(next(r for r in collect.REGIONS if r["key"] == "pakistan"))
        region["data_dir"] = os.path.join(self._tmpdir.name, "pakistan")
        region["areas"] = ["Punjab", "Sindh"]

        broad = [
            {"title": "Talks resume between India and Pakistan officials", "url": "https://e/10",
             "domain": "dawn.com", "seendate": "20260910T060000Z", "source": "gdelt"},
            {"title": "Escalation reported after border skirmish with India", "url": "https://e/11",
             "domain": "dawn.com", "seendate": "20260910T063000Z", "source": "gdelt"},
            {"title": "Local cricket league final draws huge crowds", "url": "https://e/12",
             "domain": "dawn.com", "seendate": "20260910T070000Z", "source": "gdelt"},
        ]

        def _fake_gdelt(query, timespan=collect.GDELT_TIMESPAN, maxrecords=75, extra=""):
            self.assertIn("sourcecountry:pakistan", extra)
            if "Punjab" in query or "Sindh" in query:
                return []
            return list(broad)

        with _mock.patch.object(collect, "fetch_gdelt", side_effect=_fake_gdelt), \
             _mock.patch.object(collect, "fetch_google_news_rss", return_value=[]), \
             _mock.patch.object(collect, "fetch_reddit", return_value=[]), \
             _mock.patch.object(collect, "fetch_youtube", return_value=[]):
            collect.run_region(region, youtube_key=None)

        latest_path = os.path.join(region["data_dir"], "latest.json")
        with open(latest_path) as f:
            snapshot = json.load(f)

        self.assertEqual(snapshot["region"], "Pakistan")
        self.assertIsNotNone(snapshot["india_tone"])
        # only 2 of the 3 broad articles mention India -> sample_size 2
        self.assertEqual(snapshot["india_tone"]["sample_size"], 2)
        self.assertEqual(snapshot["overall_mood"]["sample_size"], 3)

        idx_path = os.path.join(region["data_dir"], "search_index.json")
        with open(idx_path) as f:
            index = json.load(f)
        self.assertTrue(any("india_related" in a for a in index))

    def test_run_region_no_articles_leaves_previous_snapshot_untouched(self):
        region = dict(next(r for r in collect.REGIONS if r["key"] == "jk"))
        region["data_dir"] = os.path.join(self._tmpdir.name, "jk_nodata")
        region["areas"] = ["Srinagar"]
        os.makedirs(region["data_dir"], exist_ok=True)
        latest_path = os.path.join(region["data_dir"], "latest.json")
        sentinel = {"region": "Jammu & Kashmir", "previous_run": True}
        with open(latest_path, "w") as f:
            json.dump(sentinel, f)

        with _mock.patch.object(collect, "fetch_gdelt", return_value=[]), \
             _mock.patch.object(collect, "fetch_google_news_rss", return_value=[]), \
             _mock.patch.object(collect, "fetch_reddit", return_value=[]), \
             _mock.patch.object(collect, "fetch_youtube", return_value=[]):
            collect.run_region(region, youtube_key=None)

        with open(latest_path) as f:
            self.assertEqual(json.load(f), sentinel)  # untouched
        history_path = os.path.join(region["data_dir"], "history.jsonl")
        rows = collect.load_history(history_path)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["no_data"])


if __name__ == "__main__":
    unittest.main()
