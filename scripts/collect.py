#!/usr/bin/env python3
"""
J&K Pulse - free data collector.

Pulls recent J&K-related coverage from free, no-key-required sources
(GDELT, Google News RSS) plus best-effort optional sources (Reddit,
YouTube if a free API key secret is provided), scores tone, tracks
per-district and per-topic mention counts, and flags sudden spikes
("what's getting highlighted right now").

Designed to run unattended on GitHub Actions (free tier). Every network
call is wrapped so one flaky source never kills the run. If literally
nothing could be fetched, the previous good snapshot is left in place.
"""

import concurrent.futures
import json
import os
import re
import sys
import statistics
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
except ImportError:
    print("vaderSentiment not installed - run: pip install -r requirements.txt", file=sys.stderr)
    raise

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(REPO_ROOT, "data")
RAW_DIR = os.path.join(DATA_DIR, "raw")
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
SEARCH_INDEX_PATH = os.path.join(DATA_DIR, "search_index.json")
SEARCH_INDEX_MAX_AGE_DAYS = 30   # rolling window - keeps the file small forever
SEARCH_INDEX_MAX_ITEMS = 4000

USER_AGENT = "jk-pulse-tracker/1.0 (personal open-source project; contact via GitHub repo issues)"
HTTP_TIMEOUT = 12          # kept short so one slow/blocked host can't stall the whole run
DISTRICT_WORKERS = 8       # district queries run concurrently so 20 districts don't run serially
GDELT_TIMESPAN = os.environ.get("JKP_TIMESPAN", "6hours")   # matches a several-times-a-day schedule
GNEWS_WHEN = os.environ.get("JKP_WHEN", "1d")               # google news recency filter
MAX_HISTORY_LINES = 2000                                     # keep the "database" file small forever

# ---------------------------------------------------------------------------
# Geography
# ---------------------------------------------------------------------------

with open(os.path.join(HERE, "geo_reference.json"), encoding="utf-8") as f:
    GEO = json.load(f)

DISTRICTS = []
for _division, _dl in GEO["priority_region"]["divisions"].items():
    DISTRICTS.extend(_dl)

REGION_TERMS = ["Jammu and Kashmir", "J&K", "Jammu", "Kashmir"]

# ---------------------------------------------------------------------------
# Domain sentiment lexicon (nudges VADER toward this region's news idiom)
# ---------------------------------------------------------------------------

DOMAIN_LEXICON = {
    "curfew": -2.5, "crackdown": -2.5, "encounter": -2.2, "shutdown": -2.0,
    "unrest": -2.5, "protest": -1.5, "clash": -2.0, "clashes": -2.0,
    "killed": -3.2, "martyred": -2.0, "injured": -2.0, "grenade": -3.0,
    "militant": -2.0, "militants": -2.0, "terrorist": -3.0, "terrorists": -3.0,
    "attack": -2.5, "blast": -2.8, "infiltration": -2.0, "ceasefire violation": -2.0,
    "arrested": -1.2, "detained": -1.2, "landslide": -2.0, "flood": -2.0,
    "avalanche": -2.2, "restrictions": -1.3, "internet ban": -1.8, "strike": -1.0,
    "record tourist": 2.5, "tourist footfall": 1.8, "record footfall": 2.5,
    "inaugurated": 1.5, "inaugurates": 1.5, "development": 1.2, "investment": 1.5,
    "peaceful": 2.0, "peace": 1.6, "growth": 1.4, "boost": 1.3, "festival": 1.2,
    "grand success": 2.0, "record": 0.6, "employment": 1.2, "scholarship": 1.4,
    "restored": 1.0, "reopens": 1.0, "reopened": 1.0, "normalcy": 1.5,
}

_analyzer = SentimentIntensityAnalyzer()
_analyzer.lexicon.update(DOMAIN_LEXICON)


def score_text(text):
    if not text:
        return 0.0
    return _analyzer.polarity_scores(text)["compound"]


# ---------------------------------------------------------------------------
# Fetchers - each one is defensive: on any failure, return [] and log why.
# ---------------------------------------------------------------------------

def _http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


def fetch_gdelt(query, timespan=GDELT_TIMESPAN, maxrecords=75):
    """GDELT DOC 2.0 API - free, no key. Full-text news search with location tagging."""
    params = {
        "query": query,
        "mode": "artlist",
        "maxrecords": str(maxrecords),
        "format": "json",
        "sort": "datedesc",
        "timespan": timespan,
    }
    url = "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode(params)
    try:
        raw = _http_get(url)
        data = json.loads(raw)
        out = []
        for art in data.get("articles", []):
            out.append({
                "title": art.get("title", ""),
                "url": art.get("url", ""),
                "domain": art.get("domain", ""),
                "seendate": art.get("seendate", ""),
                "source": "gdelt",
            })
        return out
    except Exception as e:
        print(f"[gdelt] failed for query={query!r}: {e}", file=sys.stderr)
        return []


def fetch_google_news_rss(query, when=GNEWS_WHEN):
    """Google News RSS - free, no key. hl/gl/ceid pinned to English/India edition."""
    q = f"{query} when:{when}"
    url = ("https://news.google.com/rss/search?" +
           urllib.parse.urlencode({"q": q, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}))
    try:
        raw = _http_get(url)
        root = ET.fromstring(raw)
        out = []
        for item in root.findall(".//item"):
            title_full = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pubdate = (item.findtext("pubDate") or "").strip()
            # Google News titles are usually "Headline - Source"
            source_el = item.find("source")
            if source_el is not None and source_el.text:
                source_name = source_el.text.strip()
                title = title_full[: title_full.rfind(" - ")] if " - " in title_full else title_full
            elif " - " in title_full:
                title, source_name = title_full.rsplit(" - ", 1)
            else:
                title, source_name = title_full, ""
            out.append({
                "title": title.strip(),
                "url": link,
                "domain": source_name.strip(),
                "seendate": pubdate,
                "source": "google_news",
            })
        return out
    except Exception as e:
        print(f"[google_news] failed for query={query!r}: {e}", file=sys.stderr)
        return []


def fetch_reddit(query):
    """Best-effort, no key required. Reddit's public JSON endpoints can rate-limit
    or block without notice - failures here are expected sometimes and non-fatal."""
    url = "https://www.reddit.com/search.json?" + urllib.parse.urlencode({
        "q": query, "sort": "new", "t": "day", "limit": 25,
    })
    try:
        raw = _http_get(url, headers={"User-Agent": USER_AGENT})
        data = json.loads(raw)
        out = []
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            out.append({
                "title": d.get("title", ""),
                "url": "https://reddit.com" + d.get("permalink", ""),
                "domain": "r/" + d.get("subreddit", ""),
                "seendate": d.get("created_utc", ""),
                "source": "reddit",
            })
        return out
    except Exception as e:
        print(f"[reddit] skipped for query={query!r}: {e}", file=sys.stderr)
        return []


def fetch_youtube(query, api_key):
    """Optional. Only runs if YOUTUBE_API_KEY secret is configured. Free quota (~10k units/day)."""
    if not api_key:
        return []
    params = {
        "part": "snippet", "q": query, "order": "date", "maxResults": "10",
        "relevanceLanguage": "en", "regionCode": "IN", "key": api_key,
    }
    url = "https://www.googleapis.com/youtube/v3/search?" + urllib.parse.urlencode(params)
    try:
        raw = _http_get(url)
        data = json.loads(raw)
        out = []
        for item in data.get("items", []):
            sn = item.get("snippet", {})
            out.append({
                "title": sn.get("title", ""),
                "url": "https://youtube.com/watch?v=" + item.get("id", {}).get("videoId", ""),
                "domain": sn.get("channelTitle", ""),
                "seendate": sn.get("publishedAt", ""),
                "source": "youtube",
            })
        return out
    except Exception as e:
        print(f"[youtube] failed for query={query!r}: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Topic / keyphrase extraction (lightweight, no heavy NLP dependency)
# ---------------------------------------------------------------------------

STOPWORDS = {
    "The", "A", "An", "In", "On", "At", "For", "To", "Of", "And", "Or", "But",
    "With", "From", "By", "As", "Is", "Are", "Was", "Were", "Be", "Been",
    "This", "That", "These", "Those", "It", "Its", "After", "Before", "Amid",
    "Over", "Into", "About", "Says", "Say", "Said", "New", "News",
}

CAP_PHRASE_RE = re.compile(r"\b([A-Z][a-zA-Z\.]+(?:\s+[A-Z][a-zA-Z\.]+){0,3})\b")


def extract_keyphrases(titles):
    counts = Counter()
    for title in titles:
        for match in CAP_PHRASE_RE.findall(title):
            words = [w for w in match.split() if w not in STOPWORDS]
            if not words:
                continue
            phrase = " ".join(words)
            if len(phrase) < 4:
                continue
            if phrase in REGION_TERMS or phrase in DISTRICTS:
                continue
            counts[phrase] += 1
    return counts


def article_day(article, fallback):
    """Best-effort: normalize each source's own date format to a YYYY-MM-DD
    string for grouping/filtering. Falls back to this run's date if a
    particular article's timestamp can't be parsed."""
    s = article.get("seendate", "")
    src = article.get("source")
    try:
        if src == "gdelt" and len(s) >= 8:
            return datetime.strptime(s[:8], "%Y%m%d").date().isoformat()
        if src == "google_news" and s:
            return parsedate_to_datetime(s).date().isoformat()
        if src == "reddit" and s:
            return datetime.fromtimestamp(float(s), tz=timezone.utc).date().isoformat()
        if src == "youtube" and s:
            return s[:10]
    except Exception:
        pass
    return fallback


def load_search_index():
    if not os.path.exists(SEARCH_INDEX_PATH):
        return []
    try:
        with open(SEARCH_INDEX_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def update_search_index(tagged_articles, existing):
    """Rolling, deduped (by URL) article index used by the dashboard's search
    and date-picker. Bounded by both age and count so it stays small forever
    even though it accumulates across every run."""
    by_url = {a["url"]: a for a in existing if a.get("url")}
    for a in tagged_articles:
        if not a.get("url"):
            continue
        by_url[a["url"]] = {
            "title": a["title"], "url": a["url"], "domain": a.get("domain", ""),
            "source": a.get("source", ""), "date": a["date"], "seendate": a.get("seendate", ""),
            "sentiment": a.get("sentiment", 0.0), "districts": a.get("districts", []),
            "keywords": a.get("keywords", []),
        }
    merged = list(by_url.values())
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SEARCH_INDEX_MAX_AGE_DAYS)).date().isoformat()
    merged = [a for a in merged if a.get("date", "") >= cutoff]
    merged.sort(key=lambda a: a.get("date", ""), reverse=True)
    merged = merged[:SEARCH_INDEX_MAX_ITEMS]
    with open(SEARCH_INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False)
    return merged


# ---------------------------------------------------------------------------
# History / spike detection
# ---------------------------------------------------------------------------

def load_history():
    if not os.path.exists(HISTORY_PATH):
        return []
    rows = []
    with open(HISTORY_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def append_history(row, history_rows):
    history_rows.append(row)
    # keep the file small forever - trim from the front
    trimmed = history_rows[-MAX_HISTORY_LINES:]
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        for r in trimmed:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def compute_spikes(current_counts, history_rows, min_mentions=3, z_threshold=1.8, lookback=14):
    """Flag keyphrases whose mention count today is a statistically unusual
    jump versus their own recent baseline. Also flags brand-new terms that
    weren't present at all in the lookback window but show up with volume now."""
    past = defaultdict(list)
    for row in history_rows[-lookback:]:
        seen_this_run = set(row.get("keyword_counts", {}).keys())
        for term, cnt in row.get("keyword_counts", {}).items():
            past[term].append(cnt)
        # terms that existed historically but not in this run count as 0
    spikes = []
    for term, count in current_counts.items():
        if count < min_mentions:
            continue
        history_for_term = past.get(term, [])
        if len(history_for_term) < 3:
            # not enough history to judge - only flag if it's a strong new entrant
            if count >= min_mentions + 2:
                spikes.append({"term": term, "mentions_now": count, "baseline_avg": 0.0,
                                "zscore": None, "reason": "new_topic"})
            continue
        mean = statistics.mean(history_for_term)
        stdev = statistics.pstdev(history_for_term) or 0.5
        z = (count - mean) / stdev
        if z >= z_threshold:
            spikes.append({"term": term, "mentions_now": count, "baseline_avg": round(mean, 2),
                            "zscore": round(z, 2), "reason": "spike"})
    spikes.sort(key=lambda s: (s["zscore"] is None, -(s["zscore"] or 0), -s["mentions_now"]))
    return spikes[:15]


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def dedupe(articles):
    seen = set()
    out = []
    for a in articles:
        key = a.get("url") or a.get("title")
        if key and key not in seen:
            seen.add(key)
            out.append(a)
    return out


def collect_for_query(query, youtube_key=None, include_reddit=True):
    arts = []
    arts += fetch_gdelt(query)
    arts += fetch_google_news_rss(query)
    if include_reddit:
        arts += fetch_reddit(query)
    if youtube_key:
        arts += fetch_youtube(query, youtube_key)
    arts = dedupe(arts)
    for a in arts:
        a["sentiment"] = score_text(a["title"])
    return arts


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(RAW_DIR, exist_ok=True)
    youtube_key = os.environ.get("YOUTUBE_API_KEY", "").strip() or None

    run_started = datetime.now(timezone.utc)
    errors = []
    sources_seen = set()

    # 1) Broad region query - for overall mood + emergent topic discovery
    broad_articles = []
    for term in ["Jammu and Kashmir", "J&K"]:
        broad_articles += collect_for_query(term, youtube_key)
    broad_articles = dedupe(broad_articles)

    # 2) Per-district queries - district name + region context to disambiguate.
    # Run concurrently (GDELT + Google News only, no Reddit/YouTube here) so 20
    # districts don't run one-by-one and risk hitting the job's time limit.
    district_articles = {}

    def _fetch_district(district):
        q = f'"{district}" (Jammu OR Kashmir)'
        return district, collect_for_query(q, youtube_key=None, include_reddit=False)

    with concurrent.futures.ThreadPoolExecutor(max_workers=DISTRICT_WORKERS) as pool:
        for district, arts in pool.map(_fetch_district, DISTRICTS):
            district_articles[district] = arts

    all_articles = dedupe(broad_articles + [a for lst in district_articles.values() for a in lst])
    for a in all_articles:
        sources_seen.add(a["source"])

    if not all_articles:
        print("No articles fetched from any source this run - leaving previous snapshot in place.",
              file=sys.stderr)
        append_history({
            "ts": run_started.isoformat(),
            "no_data": True,
            "keyword_counts": {},
            "overall_mood": None,
            "total_mentions": 0,
        }, load_history())
        sys.exit(0)

    # Tag every article with its day (for the date picker), which district(s)
    # it mentions, and its own keyphrases - this is what lets the dashboard
    # show real source links under a topic/district instead of just a count.
    today_str = run_started.date().isoformat()
    for a in all_articles:
        a["date"] = article_day(a, fallback=today_str)
        title_lower = a["title"].lower()
        a["districts"] = [d for d in DISTRICTS if d.lower() in title_lower]
        a["keywords"] = list(extract_keyphrases([a["title"]]).keys())

    # Sentiment was scored per-article inside collect_for_query(); aggregate now.
    overall_scores = [a["sentiment"] for a in all_articles]
    overall_mood_score = round(statistics.mean(overall_scores), 3)
    pos = sum(1 for s in overall_scores if s >= 0.05)
    neg = sum(1 for s in overall_scores if s <= -0.05)
    neu = len(overall_scores) - pos - neg

    def mood_label(score):
        if score >= 0.35:
            return "strongly positive"
        if score >= 0.1:
            return "positive"
        if score > -0.1:
            return "mixed / neutral"
        if score > -0.35:
            return "negative"
        return "strongly negative"

    # Per-district aggregation - derived from the tagged articles, so a
    # district's figures include every article that mentions it by name,
    # not just the ones its own dedicated query happened to return.
    district_summary = {}
    for district in DISTRICTS:
        arts = [a for a in all_articles if district in a["districts"]]
        if not arts:
            district_summary[district] = {"mentions": 0, "sentiment": None, "top_terms": []}
            continue
        scores = [a["sentiment"] for a in arts]
        titles = [a["title"] for a in arts]
        top_terms = [t for t, _ in extract_keyphrases(titles).most_common(5)]
        district_summary[district] = {
            "mentions": len(arts),
            "sentiment": round(statistics.mean(scores), 3),
            "top_terms": top_terms,
        }

    # Keyphrase frequency across everything (for trending + spike detection)
    keyword_counts = extract_keyphrases([a["title"] for a in all_articles])
    top_keywords = []
    for term, cnt in keyword_counts.most_common(20):
        term_scores = [a["sentiment"] for a in all_articles if term in a["keywords"]]
        top_keywords.append({
            "term": term,
            "mentions": cnt,
            "sentiment": round(statistics.mean(term_scores), 3) if term_scores else 0.0,
        })

    source_breakdown = [{"domain": d, "count": c}
                         for d, c in Counter(a["domain"] for a in all_articles if a.get("domain")).most_common(20)]

    history_rows = load_history()
    spikes = compute_spikes(dict(keyword_counts), history_rows)

    snapshot = {
        "generated_at": run_started.isoformat(),
        "date": today_str,
        "region": "Jammu & Kashmir",
        "collection_window": GDELT_TIMESPAN,
        "overall_mood": {
            "score": overall_mood_score,
            "label": mood_label(overall_mood_score),
            "positive_pct": round(100 * pos / len(overall_scores), 1),
            "neutral_pct": round(100 * neu / len(overall_scores), 1),
            "negative_pct": round(100 * neg / len(overall_scores), 1),
            "sample_size": len(overall_scores),
        },
        "top_keywords": top_keywords,
        "emerging": spikes,
        "districts": district_summary,
        "sources_used": sorted(sources_seen),
        "source_breakdown": source_breakdown,
        "run_stats": {
            "articles_fetched": len(all_articles),
            "errors": errors,
        },
        "methodology_note": (
            "Article tone (not necessarily public mood) is scored automatically from "
            "headlines using a lexicon-based model tuned for this region's news idiom. "
            "District figures depend on district names appearing in coverage and are "
            "best-effort, not exhaustive. Sources: GDELT + Google News (always-on, "
            "no key needed), Reddit (best-effort, may be unavailable), YouTube (only "
            "if a free API key is configured). X/Twitter and Instagram/Facebook are not "
            "included - neither offers a free public search API as of 2026."
        ),
    }

    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    with open(os.path.join(RAW_DIR, "latest_raw.json"), "w", encoding="utf-8") as f:
        json.dump(all_articles, f, ensure_ascii=False, indent=2)

    update_search_index(all_articles, load_search_index())

    append_history({
        "ts": run_started.isoformat(),
        "no_data": False,
        "keyword_counts": dict(keyword_counts),
        "overall_mood": overall_mood_score,
        "total_mentions": len(all_articles),
    }, history_rows)

    print(f"OK - {len(all_articles)} articles, mood={overall_mood_score} "
          f"({mood_label(overall_mood_score)}), {len(spikes)} spikes flagged, "
          f"sources={sorted(sources_seen)}")


if __name__ == "__main__":
    main()