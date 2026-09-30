#!/usr/bin/env python3
"""
J&K Pulse - free data collector.

Pulls recent coverage for two tracked regions - Jammu & Kashmir, and
(from Pakistani-sourced outlets) Pakistan - from free, no-key-required
sources (GDELT, Google News RSS) plus best-effort optional sources
(Reddit, YouTube if a free API key secret is provided). Scores tone,
tracks per-area mention counts, flags sudden spikes ("what's getting
highlighted right now"), and for the Pakistan region additionally scores
the tone specifically of coverage that mentions India.

Designed to run unattended on GitHub Actions (free tier). Every network
call is wrapped so one flaky source never kills the run. If literally
nothing could be fetched for a region, that region's previous good
snapshot is left in place.
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

USER_AGENT = "jk-pulse-tracker/1.0 (personal open-source project; contact via GitHub repo issues)"
HTTP_TIMEOUT = 12          # kept short so one slow/blocked host can't stall the whole run
AREA_WORKERS = 8           # per-area queries run concurrently so a region's areas don't run serially
GDELT_TIMESPAN = os.environ.get("JKP_TIMESPAN", "6hours")   # matches a several-times-a-day schedule
GNEWS_WHEN = os.environ.get("JKP_WHEN", "1d")               # google news recency filter
MAX_HISTORY_LINES = 2000                                     # keep the "database" file small forever
SEARCH_INDEX_MAX_AGE_DAYS = 30   # rolling window - keeps the file small forever
SEARCH_INDEX_MAX_ITEMS = 4000

# ---------------------------------------------------------------------------
# Geography / region configuration
# ---------------------------------------------------------------------------

with open(os.path.join(HERE, "geo_reference.json"), encoding="utf-8") as f:
    GEO = json.load(f)

_JK_DISTRICTS = []
for _division, _dl in GEO["priority_region"]["divisions"].items():
    _JK_DISTRICTS.extend(_dl)

_PK = GEO["pakistan_region"]
_PK_PROVINCES = _PK["provinces"]
_PK_DISAMBIG = _PK.get("province_disambiguation", {})
_PK_INDIA_TERMS = _PK["india_related_terms"]


def _jk_area_query(area):
    return f'"{area}" (Jammu OR Kashmir)'


def _pk_area_query(area):
    disambig = _PK_DISAMBIG.get(area)
    return f'"{area}" {disambig}' if disambig else f'"{area}"'


REGIONS = [
    {
        "key": "jk",
        "label": "Jammu & Kashmir",
        "data_dir": DATA_DIR,                       # kept at repo root - this is the original, already-deployed dataset
        "broad_terms": ["Jammu and Kashmir", "J&K"],
        "areas": _JK_DISTRICTS,
        "area_label": "district",
        "area_field": "districts",
        "area_query_fn": _jk_area_query,
        "exclude_terms": ["Jammu and Kashmir", "J&K", "Jammu", "Kashmir"] + _JK_DISTRICTS,
        "gdelt_extra": "",
        "gnews_gl": "IN", "gnews_ceid": "IN:en",
        "india_related": False,
        "methodology_extra": "",
    },
    {
        "key": "pakistan",
        "label": "Pakistan",
        "data_dir": os.path.join(DATA_DIR, "pakistan"),
        "broad_terms": ["Pakistan"],
        "areas": _PK_PROVINCES,
        "area_label": "province",
        "area_field": "provinces",
        "area_query_fn": _pk_area_query,
        "exclude_terms": ["Pakistan"] + _PK_PROVINCES,
        # GDELT's sourcecountry: operator restricts results to outlets based in that
        # country, so this reflects what's circulating IN Pakistani media - not just
        # global coverage about Pakistan from anywhere.
        "gdelt_extra": " sourcecountry:pakistan",
        "gnews_gl": "PK", "gnews_ceid": "PK:en",
        "india_related": True,
        "india_terms": _PK_INDIA_TERMS,
        "methodology_extra": (
            " Coverage is restricted to Pakistani-based outlets (GDELT sourcecountry "
            "filter + Google News' Pakistan edition), so this reflects what is "
            "circulating in Pakistani media specifically. \"Tone toward India\" is a "
            "separate score computed only from the subset of that coverage which "
            "mentions India/Modi/New Delhi/Kashmir - it is not the same number as the "
            "region's general tone."
        ),
    },
]

# ---------------------------------------------------------------------------
# Domain sentiment lexicon (nudges VADER toward this region's news idiom)
# ---------------------------------------------------------------------------
#
# IMPORTANT: VADER scores text token-by-token, so a multi-word key only ever
# matches if we substitute it for a single placeholder token before scoring
# (done below in score_text via _PHRASE_RE). Do not assume a multi-word key
# added straight to analyzer.lexicon will ever be seen - it won't.

DOMAIN_LEXICON = {
    # single words - matched directly by VADER's tokenizer
    "curfew": -2.5, "crackdown": -2.5, "encounter": -2.2, "shutdown": -2.0,
    "unrest": -2.5, "protest": -1.5, "clash": -2.0, "clashes": -2.0,
    "killed": -3.2, "martyred": -2.0, "injured": -2.0, "grenade": -3.0,
    "militant": -2.0, "militants": -2.0, "terrorist": -3.0, "terrorists": -3.0,
    "attack": -2.5, "blast": -2.8, "infiltration": -2.0,
    "arrested": -1.2, "detained": -1.2, "landslide": -2.0, "flood": -2.0,
    "avalanche": -2.2, "restrictions": -1.3, "strike": -1.0,
    "inaugurated": 1.5, "inaugurates": 1.5, "development": 1.2, "investment": 1.5,
    "peaceful": 2.0, "peace": 1.6, "growth": 1.4, "boost": 1.3, "festival": 1.2,
    "record": 0.6, "employment": 1.2, "scholarship": 1.4,
    "restored": 1.0, "reopens": 1.0, "reopened": 1.0, "normalcy": 1.5,
    "hostile": -2.0, "provocation": -2.5, "escalation": -2.0, "de-escalation": 1.5,
    "sanctions": -1.5, "hotline": 0.5, "backchannel": 0.8, "goodwill": 1.2,
    "summit": 0.6,
    # multi-word phrases - only take effect via the placeholder-substitution
    # in score_text(); listing them here keeps one source of truth for valence.
    "record tourist": 2.5, "tourist footfall": 1.8, "record footfall": 2.5,
    "grand success": 2.0, "internet ban": -1.8, "ceasefire violation": -2.2,
    "diplomatic row": -1.8, "border skirmish": -2.3, "talks resume": 1.6,
    "trade resumes": 1.6, "goodwill gesture": 1.5, "war of words": -1.5,
    "hate speech": -2.0, "visa relaxation": 1.3,
}

_WORD_LEXICON = {k: v for k, v in DOMAIN_LEXICON.items() if " " not in k}
_PHRASE_LEXICON = {k: v for k, v in DOMAIN_LEXICON.items() if " " in k}

_analyzer = SentimentIntensityAnalyzer()
_analyzer.lexicon.update(_WORD_LEXICON)

_PHRASE_TOKENS = {}
for _i, _phrase in enumerate(sorted(_PHRASE_LEXICON, key=len, reverse=True)):
    _token = f"xphrasetoken{_i}"
    _PHRASE_TOKENS[_phrase] = _token
    _analyzer.lexicon[_token] = _PHRASE_LEXICON[_phrase]

_PHRASE_RE = (re.compile("|".join(re.escape(p) for p in _PHRASE_TOKENS), re.IGNORECASE)
              if _PHRASE_TOKENS else None)


def score_text(text):
    if not text:
        return 0.0
    if _PHRASE_RE:
        text = _PHRASE_RE.sub(lambda m: _PHRASE_TOKENS[m.group(0).lower()], text)
    return _analyzer.polarity_scores(text)["compound"]


# ---------------------------------------------------------------------------
# Fetchers - each one is defensive: on any failure, return [] and log why.
# ---------------------------------------------------------------------------

def _http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


def fetch_gdelt(query, timespan=GDELT_TIMESPAN, maxrecords=75, extra=""):
    """GDELT DOC 2.0 API - free, no key. Full-text news search with location tagging.
    `extra` appends raw GDELT query operators, e.g. " sourcecountry:pakistan"."""
    params = {
        "query": query + extra,
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


def fetch_google_news_rss(query, when=GNEWS_WHEN, gl="IN", ceid="IN:en"):
    """Google News RSS - free, no key. hl/gl/ceid pin the edition (IN = India, PK = Pakistan)."""
    q = f"{query} when:{when}"
    hl = "en-IN" if gl == "IN" else "en-PK" if gl == "PK" else "en"
    url = ("https://news.google.com/rss/search?" +
           urllib.parse.urlencode({"q": q, "hl": hl, "gl": gl, "ceid": ceid}))
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


def fetch_youtube(query, api_key, region_code="IN"):
    """Optional. Only runs if YOUTUBE_API_KEY secret is configured. Free quota (~10k units/day)."""
    if not api_key:
        return []
    params = {
        "part": "snippet", "q": query, "order": "date", "maxResults": "10",
        "relevanceLanguage": "en", "regionCode": region_code, "key": api_key,
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


def extract_keyphrases(titles, exclude=()):
    exclude_set = set(exclude)
    counts = Counter()
    for title in titles:
        for match in CAP_PHRASE_RE.findall(title):
            words = [w for w in match.split() if w not in STOPWORDS]
            if not words:
                continue
            phrase = " ".join(words)
            if len(phrase) < 4:
                continue
            if phrase in exclude_set:
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


def load_search_index(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def update_search_index(path, tagged_articles, existing, area_field, extra_fields=()):
    """Rolling, deduped (by URL) article index used by the dashboard's search
    and date-picker. Bounded by both age and count so it stays small forever
    even though it accumulates across every run."""
    by_url = {a["url"]: a for a in existing if a.get("url")}
    for a in tagged_articles:
        if not a.get("url"):
            continue
        entry = {
            "title": a["title"], "url": a["url"], "domain": a.get("domain", ""),
            "source": a.get("source", ""), "date": a["date"], "seendate": a.get("seendate", ""),
            "sentiment": a.get("sentiment", 0.0), area_field: a.get(area_field, []),
            "keywords": a.get("keywords", []),
        }
        for ef in extra_fields:
            entry[ef] = a.get(ef)
        by_url[a["url"]] = entry
    merged = list(by_url.values())
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SEARCH_INDEX_MAX_AGE_DAYS)).date().isoformat()
    merged = [a for a in merged if a.get("date", "") >= cutoff]
    merged.sort(key=lambda a: a.get("date", ""), reverse=True)
    merged = merged[:SEARCH_INDEX_MAX_ITEMS]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False)
    return merged


# ---------------------------------------------------------------------------
# History / spike detection
# ---------------------------------------------------------------------------

def load_history(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def append_history(path, row, history_rows):
    history_rows.append(row)
    # keep the file small forever - trim from the front
    trimmed = history_rows[-MAX_HISTORY_LINES:]
    with open(path, "w", encoding="utf-8") as f:
        for r in trimmed:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def compute_spikes(current_counts, history_rows, min_mentions=3, z_threshold=1.8, lookback=14):
    """Flag keyphrases whose mention count today is a statistically unusual
    jump versus their own recent baseline. Also flags brand-new terms that
    weren't present at all in the lookback window but show up with volume now."""
    past = defaultdict(list)
    for row in history_rows[-lookback:]:
        for term, cnt in row.get("keyword_counts", {}).items():
            past[term].append(cnt)
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


def mood_label(score):
    if score is None:
        return "no data"
    if score >= 0.35:
        return "strongly positive"
    if score >= 0.1:
        return "positive"
    if score > -0.1:
        return "mixed / neutral"
    if score > -0.35:
        return "negative"
    return "strongly negative"


def mood_breakdown(scores):
    """Shared by overall-mood and india-tone: pos/neu/neg split + label."""
    if not scores:
        return {"score": None, "label": mood_label(None), "positive_pct": None,
                "neutral_pct": None, "negative_pct": None, "sample_size": 0}
    score = round(statistics.mean(scores), 3)
    pos = sum(1 for s in scores if s >= 0.05)
    neg = sum(1 for s in scores if s <= -0.05)
    neu = len(scores) - pos - neg
    return {
        "score": score, "label": mood_label(score),
        "positive_pct": round(100 * pos / len(scores), 1),
        "neutral_pct": round(100 * neu / len(scores), 1),
        "negative_pct": round(100 * neg / len(scores), 1),
        "sample_size": len(scores),
    }


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


def collect_for_query(query, region, youtube_key=None, include_reddit=True):
    arts = []
    arts += fetch_gdelt(query, extra=region["gdelt_extra"])
    arts += fetch_google_news_rss(query, gl=region["gnews_gl"], ceid=region["gnews_ceid"])
    if include_reddit:
        arts += fetch_reddit(query)
    if youtube_key:
        arts += fetch_youtube(query, youtube_key, region_code=region["gnews_gl"])
    arts = dedupe(arts)
    for a in arts:
        a["sentiment"] = score_text(a["title"])
    return arts


def run_region(region, youtube_key):
    data_dir = region["data_dir"]
    raw_dir = os.path.join(data_dir, "raw")
    latest_path = os.path.join(data_dir, "latest.json")
    history_path = os.path.join(data_dir, "history.jsonl")
    search_index_path = os.path.join(data_dir, "search_index.json")
    area_field = region["area_field"]
    areas = region["areas"]

    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(raw_dir, exist_ok=True)

    run_started = datetime.now(timezone.utc)
    sources_seen = set()

    # 1) Broad region query - for overall mood + emergent topic discovery
    broad_articles = []
    for term in region["broad_terms"]:
        broad_articles += collect_for_query(term, region, youtube_key)
    broad_articles = dedupe(broad_articles)

    # 2) Per-area queries, concurrent so N areas don't run one-by-one and
    # risk hitting the job's time limit. No Reddit/YouTube here - kept only
    # on the broad query to bound total request volume.
    area_articles = {}

    def _fetch_area(area):
        q = region["area_query_fn"](area)
        return area, collect_for_query(q, region, youtube_key=None, include_reddit=False)

    with concurrent.futures.ThreadPoolExecutor(max_workers=AREA_WORKERS) as pool:
        for area, arts in pool.map(_fetch_area, areas):
            area_articles[area] = arts

    all_articles = dedupe(broad_articles + [a for lst in area_articles.values() for a in lst])
    for a in all_articles:
        sources_seen.add(a["source"])

    history_rows = load_history(history_path)

    if not all_articles:
        print(f"[{region['key']}] No articles fetched this run - leaving previous snapshot in place.",
              file=sys.stderr)
        append_history(history_path, {
            "ts": run_started.isoformat(), "no_data": True,
            "keyword_counts": {}, "overall_mood": None, "total_mentions": 0,
        }, history_rows)
        return

    # Tag every article with its day (for the date picker), which area(s) it
    # mentions, its own keyphrases, and (Pakistan only) whether it's
    # India-related - this is what lets the dashboard show real source links
    # under a topic/area instead of just a count, and compute the separate
    # "tone toward India" metric.
    today_str = run_started.date().isoformat()
    for a in all_articles:
        a["date"] = article_day(a, fallback=today_str)
        title_lower = a["title"].lower()
        a[area_field] = [ar for ar in areas if ar.lower() in title_lower]
        a["keywords"] = list(extract_keyphrases([a["title"]], exclude=region["exclude_terms"]).keys())
        if region["india_related"]:
            a["india_related"] = any(t.lower() in title_lower for t in region["india_terms"])

    overall_scores = [a["sentiment"] for a in all_articles]
    overall_mood = mood_breakdown(overall_scores)

    india_tone = None
    if region["india_related"]:
        india_scores = [a["sentiment"] for a in all_articles if a.get("india_related")]
        india_tone = mood_breakdown(india_scores)

    # Per-area aggregation - derived from the tagged articles, so an area's
    # figures include every article that mentions it by name, not just the
    # ones its own dedicated query happened to return.
    area_summary = {}
    for area in areas:
        arts = [a for a in all_articles if area in a[area_field]]
        if not arts:
            area_summary[area] = {"mentions": 0, "sentiment": None, "top_terms": []}
            continue
        scores = [a["sentiment"] for a in arts]
        titles = [a["title"] for a in arts]
        top_terms = [t for t, _ in extract_keyphrases(titles, exclude=region["exclude_terms"]).most_common(5)]
        area_summary[area] = {
            "mentions": len(arts),
            "sentiment": round(statistics.mean(scores), 3),
            "top_terms": top_terms,
        }

    keyword_counts = extract_keyphrases([a["title"] for a in all_articles], exclude=region["exclude_terms"])
    top_keywords = []
    for term, cnt in keyword_counts.most_common(20):
        term_scores = [a["sentiment"] for a in all_articles if term in a["keywords"]]
        top_keywords.append({
            "term": term, "mentions": cnt,
            "sentiment": round(statistics.mean(term_scores), 3) if term_scores else 0.0,
        })

    source_breakdown = [{"domain": d, "count": c}
                         for d, c in Counter(a["domain"] for a in all_articles if a.get("domain")).most_common(20)]

    spikes = compute_spikes(dict(keyword_counts), history_rows)

    base_methodology = (
        "Article tone (not necessarily public mood) is scored automatically from "
        f"headlines using a lexicon-based model tuned for this region's news idiom. "
        f"{region['area_label'].capitalize()}-level figures depend on {region['area_label']} "
        "names appearing in coverage and are best-effort, not exhaustive. Sources: GDELT + "
        "Google News (always-on, no key needed), Reddit (best-effort, may be unavailable), "
        "YouTube (only if a free API key is configured). X/Twitter and Instagram/Facebook "
        "are not included - neither offers a free public search API as of 2026."
    ) + region["methodology_extra"]

    snapshot = {
        "generated_at": run_started.isoformat(),
        "date": today_str,
        "region": region["label"],
        "area_label": region["area_label"],
        "collection_window": GDELT_TIMESPAN,
        "overall_mood": overall_mood,
        "india_tone": india_tone,
        "top_keywords": top_keywords,
        "emerging": spikes,
        "areas": area_summary,
        "sources_used": sorted(sources_seen),
        "source_breakdown": source_breakdown,
        "run_stats": {"articles_fetched": len(all_articles), "errors": []},
        "methodology_note": base_methodology,
    }

    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    with open(os.path.join(raw_dir, "latest_raw.json"), "w", encoding="utf-8") as f:
        json.dump(all_articles, f, ensure_ascii=False, indent=2)

    extra_fields = ("india_related",) if region["india_related"] else ()
    update_search_index(search_index_path, all_articles, load_search_index(search_index_path),
                         area_field, extra_fields=extra_fields)

    append_history(history_path, {
        "ts": run_started.isoformat(), "no_data": False,
        "keyword_counts": dict(keyword_counts),
        "overall_mood": overall_mood["score"], "total_mentions": len(all_articles),
    }, history_rows)

    print(f"OK [{region['key']}] - {len(all_articles)} articles, mood={overall_mood['score']} "
          f"({overall_mood['label']}), {len(spikes)} spikes flagged, sources={sorted(sources_seen)}"
          + (f", india_tone={india_tone['score']}" if india_tone else ""))


def main():
    youtube_key = os.environ.get("YOUTUBE_API_KEY", "").strip() or None
    for region in REGIONS:
        run_region(region, youtube_key)


if __name__ == "__main__":
    main()
