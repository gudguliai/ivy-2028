#!/usr/bin/env python3
"""
Ivy-2028 v2 Coordinator
Reads JSON artifacts from /tmp/ivy-2028/<date>/, deduplicates, ranks, generates HTML+CSV.
"""

import json
import csv
import os
import re
import sys
import unicodedata
import urllib.request
import urllib.error
from datetime import datetime, date, timedelta
from typing import Optional

from db import (
    init_db, insert_run, get_previous_run_dates, get_run_opportunities,
    compute_wow_statuses, save_opportunity, save_run_opportunities,
    detect_gone, get_gone_opportunities, get_all_opportunities, detect_ethnic_tags,
    rename_opportunity_url,
)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")

CATEGORY_LABELS = {
    "science_math": "Science & Math",
    "scholarships": "Scholarships",
    "law_civics": "Law & Civics",
    "exams": "Exams",
    "writing_humanities": "Writing & Humanities",
    "summer_programs": "Summer Programs",
    "general_competitions": "General",
    "ct_local": "CT Local",
    "tech_ai": "Tech / AI",
    "fencing": "Fencing",
    "worldbuilding": "World-Building / Interactive Fiction",
}

MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

PREP_PATTERNS = {
    "Essay": r"\bessay\b",
    "Rec Letter": r"\brecommendation\b|\brec letter\b|\breference\b",
    "Portfolio": r"\bportfolio\b|\bwriting sample\b",
    "Test Score": r"\bSAT\b|\bACT\b|\bPSAT\b|\bscore\b|\btest scores?\b",
    "Research Abstract": r"\babstract\b|\bresearch paper\b",
    "Audition": r"\baudition\b",
    "Nomination": r"\bnomination\b|\bnominated\b",
    "Transcript": r"\btranscript\b",
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"\bu\s?\.\s?s\s?\.(?=\s|$)", "united states", s)  # "U.S." -> "united states"
    s = re.sub(r"[()]", " ", s)   # keep parenthetical words, drop the parens
    s = s.replace("-", " ")       # "world-building" -> "world building"
    s = re.sub(r"\s+", " ", s).strip()
    return s


def days_until(deadline_str: str) -> Optional[int]:
    if not deadline_str or deadline_str in ("TBD", "rolling", "Rolling"):
        return None
    try:
        dt = datetime.strptime(deadline_str, "%Y-%m-%d").date()
        return (dt - date.today()).days
    except ValueError:
        return None


def load_artifacts(artifacts_dir: str) -> list[dict]:
    """Load all JSON artifacts from artifacts_dir."""
    if not os.path.isdir(artifacts_dir):
        print(f"Artifacts directory not found: {artifacts_dir}", file=sys.stderr)
        return []

    all_opps = []
    for fname in sorted(os.listdir(artifacts_dir)):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(artifacts_dir, fname)
        try:
            with open(fpath) as f:
                data = json.load(f)
            if isinstance(data, list):
                all_opps.extend(data)
                print(f"  Loaded {len(data)} from {fname}", flush=True)
            elif isinstance(data, dict) and "opportunities" in data:
                all_opps.extend(data["opportunities"])
                print(f"  Loaded {len(data['opportunities'])} from {fname}", flush=True)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  Error reading {fname}: {e}", file=sys.stderr)

    return all_opps


def _jaccard_match(a: str, b: str) -> bool:
    """Name-similarity check shared by both dedupe passes."""
    words1 = set(a.split())
    words2 = set(b.split())
    if len(words1) > 3 and len(words2) > 3:
        return len(words1 & words2) / max(len(words1 | words2), 1) > 0.85
    return a == b


def deduplicate(opps: list[dict]) -> list[dict]:
    seen_urls = set()
    seen_names: list[str] = []
    unique = []

    for opp in opps:
        url = (opp.get("url") or "").rstrip("/")
        if url and url in seen_urls:
            continue
        url and seen_urls.add(url)

        name = _norm(opp.get("name", ""))
        if not name:
            continue

        dup = any(_jaccard_match(name, seen) for seen in seen_names)

        if dup:
            continue

        seen_names.append(name)
        unique.append(opp)

    return unique


def _strip_year(s: str) -> str:
    """Remove standalone year tokens so 'X 2027' matches archive row 'X'."""
    return re.sub(r"\s*\b(19|20)\d{2}\b", "", s).strip()


def dedupe_against_db(opps: list[dict], db_path: str | None = None) -> list[dict]:
    """Cross-run name dedupe: link re-found programs to their canonical DB row.

    deduplicate() only dedupes within one run, so the same program re-found under
    a different URL in a later run creates a duplicate row (e.g. "Coolidge
    Scholarship" under two URLs). For each opp, compare its normalized name
    against every opportunity already in the DB using the same jaccard > 0.85
    word-set logic (year-suffixed names like "Davidson Fellows 2027" also match
    the archive row "Davidson Fellows").

    On a match with a DIFFERENT url, the archive URL is recorded as
    opp["_canonical_url"] but the researcher's fresh URL is KEPT — the stale
    canonical must never silently override a live link. Reconciliation happens
    after URL validation in resolve_canonical_urls(): a live fresh URL renames
    the archive row; a dead fresh URL falls back to the canonical if alive.
    A match with the SAME url is left alone. On an empty/missing DB this is
    a no-op.
    """
    import sqlite3 as _sqlite3

    try:
        existing = get_all_opportunities(db_path)
    except (_sqlite3.OperationalError, _sqlite3.DatabaseError):
        return opps  # no tables yet — first run
    except Exception:
        return opps
    if not existing:
        return opps

    linked = 0
    for opp in opps:
        name = _norm(opp.get("name", ""))
        if not name:
            continue
        name_noyear = _strip_year(name)
        url = (opp.get("url") or "").rstrip("/")
        for row in existing:
            ename = _norm(row.get("name", ""))
            if not ename:
                continue
            if not (_jaccard_match(name, ename)
                    or _jaccard_match(name_noyear, _strip_year(ename))):
                continue
            eurl = (row.get("url") or "").rstrip("/")
            if eurl and eurl != url:
                opp["_canonical_url"] = eurl
                opp["notes"] = (opp.get("notes") or "") + (
                    f"; archive canonical: {eurl} "
                    f"(first seen {row.get('first_seen_run_date', '?')})"
                )
                linked += 1
            break
    if linked:
        print(f"  Cross-run dedupe: {linked} opportunities linked to archive rows (URLs reconciled post-validation)", flush=True)
    return opps


def _quick_url_alive(url: str, timeout: int = 5) -> bool:
    """Minimal HEAD->GET liveness probe (same backstop as validate_urls)."""
    import socket
    socket.setdefaulttimeout(timeout)
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method)
            req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)")
            resp = urllib.request.urlopen(req, timeout=timeout)
            if resp.status < 400 or resp.status in (403, 429):
                return True
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                return True
        except Exception:
            pass
    return False


def resolve_canonical_urls(opps: list[dict], db_path: str | None = None) -> list[dict]:
    """Reconcile fresh researcher URLs with archive canonicals (post-validation).

    - Fresh URL alive + differs from canonical: rename the archive row to the
      fresh URL (one row per program, always the live link; history preserved).
    - Fresh URL dead + canonical alive: restore the canonical URL.
    - Both dead: keep the researcher's find (may be a transient outage).

    Rename chains are followed: if two opps in one run link to the same
    archive row (e.g. two URLs for "Cameron Impact Scholarship"), the second
    rename targets the first's result, converging to a single row.
    """
    renames: dict[str, str] = {}

    def _chain(url: str) -> str:
        seen = set()
        while url in renames and url not in seen:
            seen.add(url)
            url = renames[url]
        return url

    for opp in opps:
        canonical = opp.pop("_canonical_url", None)
        if not canonical:
            continue
        canonical = _chain(canonical)
        fresh = (opp.get("url") or "").rstrip("/")
        fresh_alive = bool(fresh) and opp.get("url_status") != "dead"
        if fresh_alive and fresh != canonical:
            try:
                if rename_opportunity_url(canonical, fresh, opp.get("name", ""),
                                          opp.get("category", "unknown"), db_path=db_path):
                    renames[canonical] = fresh
                    opp["notes"] = (opp.get("notes") or "") + "; archive URL updated to live link"
                    print(f"  refresh URL: {opp.get('name', '?')}", flush=True)
                else:
                    print(f"  (no archive row to refresh for {opp.get('name', '?')})", flush=True)
            except Exception as e:
                print(f"  ! URL rename failed for {opp.get('name', '?')}: {e}", flush=True)
        elif not fresh_alive and _quick_url_alive(canonical):
            canonical = _chain(canonical)
            opp["url"] = canonical
            opp["url_status"] = "restored"
            opp["url_verified"] = True
            opp["url_confidence"] = "HIGH"
            opp["notes"] = (opp.get("notes") or "") + f"; fresh URL failed, archive canonical restored: {canonical}"
            print(f"  restore canonical: {opp.get('name', '?')}", flush=True)

    # Remap every opp to its final URL, then merge opps that converged onto
    # the same program (e.g. two artifact entries for "Davidson Fellows"
    # with different URLs). Dead-link opps (empty URL) are never merged.
    for opp in opps:
        if opp.get("url"):
            opp["url"] = _chain(opp["url"].rstrip("/"))
    merged: list[dict] = []
    by_url: dict[str, dict] = {}
    for opp in opps:
        url = (opp.get("url") or "").rstrip("/")
        if not url or url not in by_url:
            if url:
                by_url[url] = opp
            merged.append(opp)
            continue
        base = by_url[url]
        bd, od = base.get("deadline"), opp.get("deadline")
        if od and od not in ("TBD", "rolling", None) and (
            not bd or bd in ("TBD", "rolling", None) or od < bd
        ):
            base["deadline"] = od
        base["notes"] = (base.get("notes") or "") + " | " + (opp.get("notes") or "")
        if len(opp.get("name", "")) > len(base.get("name", "")):
            base["name"] = opp["name"]
        print(f"  merge duplicate: {opp.get('name', '?')}", flush=True)
    return merged


# (regex pattern, weight) — each group contributes its weight at most once
SPIKE_SIGNALS = [
    (r"world[\s-]?building|interactive fiction|computational narrative|procedural generation"
     r"|game design|game dev|unreal engine|\btwine\b|game jam|narrative design|storytelling|simulation", 40),
    (r"writing|novel|published author|essay contest|scholastic|youngarts|memoir|short stor"
     r"|creative writing|poetry", 25),
    (r"presidential scholars|coca[\s-]?cola|davidson fellows|regeneron|conrad challenge"
     r"|congressional app challenge", 15),
    (r"\bisef\b|\busabo\b|biology olympiad|\bamc\b|american mathematics|science olympiad|science bowl", 10),
    (r"connecticut|new england|hartford|new haven|stamford|\bct\b", 5),
    (r"volunteer|community service|leadership|\bservice\b", 5),
]


def spike_fit(opp: dict) -> int:
    """Score 0-100 for fit with the student's admissions spike.

    The spike is world-building / computational narrative / interactive
    storytelling; secondary strengths are writing (published novelist) and
    fencing; CT-local matters.

    Weights (transparent heuristic):
    - world-building / interactive fiction / computational narrative /
      procedural generation / game design / Unreal Engine / Twine / game jam: +40
    - writing / novel / published author / essay contest / Scholastic /
      YoungArts: +25
    - national prestige markers (Presidential Scholars, Coca-Cola, Davidson
      Fellows, Regeneron, Conrad, Congressional App Challenge): +15
    - STEM research competitions (ISEF, USABO, AMC, Science Olympiad): +10
    - CT / New England local: +5
    - service / leadership / volunteer: +5

    Each group contributes at most once; total capped at 100. Matched against
    the normalized name + snippet + notes.
    """
    combined = _norm(
        str(opp.get("name") or "") + " "
        + str(opp.get("snippet") or "") + " "
        + str(opp.get("notes") or "")
    )
    score = 0
    for pattern, weight in SPIKE_SIGNALS:
        if re.search(pattern, combined):
            score += weight
    return min(score, 100)


def filter_eligible(opps: list[dict]) -> list[dict]:
    """Remove low-income-only, college-only, and grade-ineligible."""
    filtered = []
    for opp in opps:
        combined = _norm(str(opp.get("snippet") or "") + " " + str(opp.get("name") or "") + " " + str(opp.get("eligibility_note") or "") + " " + str(opp.get("notes") or ""))

        # Remove low-income only
        low_income_patterns = [
            r"\blow[-\s]income\b",
            r"\bpell grant\b",
            r"\bfree lunch\b",
            r"\bfrl\b.*\blunch\b",
            r"\bhousehold income\b.*\bunder\b",
            r"\beconomic[ally]?\s+disadvantaged\b",
            r"\bfirst[-\s]generation\b.*\blow[-\s]income\b",
            r"\bincome\s+below\b",
            r"\bfederal\s+poverty\b",
            r"\bneed[-\s]based\b",
        ]
        low_income = any(re.search(p, combined) for p in low_income_patterns)
        if low_income:
            continue

        # Remove college-only
        if re.search(r"\b(?:undergraduate|graduate|college student)\b", combined) and not re.search(
            r"\b(?:high school|secondary)\b", combined
        ):
            continue

        # Explicit senior-only declarations win outright: researcher notes like
        # "SENIORS ONLY ... ineligible as a junior" must not be rescued by the
        # mere mention of "junior" in the ineligibility explanation.
        if re.search(r"\bseniors?\s+only\b", combined):
            continue
        if re.search(r"\bineligible[^.\n]{0,80}\bjuniors?\b", combined) or \
           re.search(r"\bjuniors?\b[^.\n]{0,80}\bineligible\b", combined):
            continue

        # Remove senior-only if no younger grade mentioned (plural-tolerant:
        # "juniors" must count as junior-eligible, "seniors" must count as senior-only)
        _YOUNGER = r"\b(?:sophomores?|juniors?|freshm[ae]n|underclassm[ae]n|9th|10th|11th|grades?\s*[-\s]?(?:9|10|11)|class of 2028)\b"
        if re.search(r"\bgrade\s*12\b", combined) and not re.search(_YOUNGER, combined):
            continue
        if re.search(r"\bseniors?\b", combined) and not re.search(_YOUNGER, combined):
            continue
        # Stronger senior-only signals (e.g. "final year of secondary school",
        # "graduating seniors", "class of 2027") — combined is already _norm'd
        # so these lowercase patterns are case-insensitive
        senior_only_patterns = [
            r"\bfinal years?\b",
            r"\b12th grades?\b",
            r"\b12th graders?\b",
            r"\bclass of 2027\b",
            r"\bgraduating seniors?\b",
            r"\bseniors? only\b",
            r"\brising seniors?\b",
        ]
        if any(re.search(p, combined) for p in senior_only_patterns) and not re.search(_YOUNGER, combined):
            continue

        # Remove expired deadlines — but keep annual programs that repeat
        deadline = opp.get("deadline")
        if deadline:
            d = days_until(deadline)
            if d is not None and d < -90:
                # Over 90 days past — last year's date. Keep but mark stale.
                opp["stale"] = True
                opp["deadline_display"] = f"{deadline} (prior year)"
                opp["deadline"] = "TBD"
                opp["notes"] = (opp.get("notes") or "") + "; repeats annually, check for 2027 dates"
            elif d is not None and d < -30:
                # 30-90 days past — could still be valid but suspicious
                opp["stale"] = True
                opp["deadline_display"] = f"{deadline} (prior year)"
                opp["deadline"] = "TBD"
                opp["notes"] = (opp.get("notes") or "") + "; deadline may have passed"
            else:
                opp["stale"] = False
                opp["deadline_display"] = deadline
        else:
            opp["stale"] = False
            opp["deadline_display"] = opp.get("deadline", "TBD")

        filtered.append(opp)
    return filtered


def tag_prep(snippet: str, name: str, notes: str = "") -> list[str]:
    combined = _norm(snippet + " " + name + " " + notes)
    tags = []
    for tag_name, pattern in PREP_PATTERNS.items():
        if re.search(pattern, combined):
            tags.append(tag_name)
    return tags


_FIRE_FETCH_TIMEOUT = 15  # seconds per URL

# NOTE (Aditya's directive, Sep 25 2026): no third-party fetch services.
# URL verification runs on our own tools only: researchers open every URL
# with their own browser tools during research and set url_verified=true on
# the artifact; validate_urls() below honors that and uses a plain-HTTP
# HEAD/GET backstop for anything not pre-verified. Keenable and Firecrawl
# were removed from this pipeline on purpose — do not re-add them.


def validate_urls(opps: list[dict], timeout: int = 5) -> list[dict]:
    """Check URLs are real, using our own tooling only.

    Researchers verify each URL with their own browser tools during research
    and mark the artifact url_verified=true (url_source="browser"). This
    function honors that. Anything not pre-verified gets a plain-HTTP
    HEAD/GET check as a backstop. On DNS / 404 / timeout the URL is cleared
    and the opportunity is annotated with [DEAD: url].
    """
    import socket
    socket.setdefaulttimeout(timeout)

    validated = []
    for opp in opps:
        url = opp.get("url", "")
        if not url or not url.startswith("http"):
            opp["url_verified"] = True
            validated.append(opp)
            continue

        # ── Pre-verified by researcher with own browser tools ─────
        if opp.get("url_verified") is True:
            opp.setdefault("url_status", "verified")
            opp.setdefault("url_confidence", "HIGH")
            print(f"  ✓ {opp.get('name','?')} (researcher-verified)",
                  flush=True)
            validated.append(opp)
            continue

        # ── Plain-HTTP backstop ───────────────────────────────────
        opp["url_verified"] = False
        http_ok = False
        try:
            req = urllib.request.Request(url, method="HEAD")
            req.add_header(
                "User-Agent",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            )
            resp = urllib.request.urlopen(req, timeout=timeout)
            http_ok = resp.status < 400
            # HEAD success — fall through to http_ok check
            if resp.status in (403, 429):
                opp["notes"] = (opp.get("notes") or "") + \
                    f"; [URL may block bots: {url}]"
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                http_ok = True
                opp["notes"] = (opp.get("notes") or "") + \
                    f"; [URL may block bots: {url}]"
            elif e.code in (302, 303, 307, 308):
                # Redirect — GET fallback
                try:
                    req2 = urllib.request.Request(url, method="GET")
                    req2.add_header(
                        "User-Agent",
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                    )
                    resp2 = urllib.request.urlopen(req2, timeout=timeout)
                    http_ok = resp2.status < 400
                except urllib.error.HTTPError as e2:
                    if e2.code in (403, 429):
                        http_ok = True
                        opp["notes"] = (opp.get("notes") or "") + \
                            f"; [URL may block bots: {url}]"
                except Exception:
                    pass
            else:
                # Any other HTTP error — try GET as fallback
                try:
                    req2 = urllib.request.Request(url, method="GET")
                    req2.add_header(
                        "User-Agent",
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                    )
                    resp2 = urllib.request.urlopen(req2, timeout=timeout)
                    http_ok = resp2.status < 400
                except Exception:
                    pass
        except Exception:
            # HEAD network error — try GET
            try:
                req2 = urllib.request.Request(url, method="GET")
                req2.add_header(
                    "User-Agent",
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                )
                resp2 = urllib.request.urlopen(req2, timeout=timeout)
                http_ok = resp2.status < 400
            except Exception:
                pass

        if http_ok:
            opp["url_verified"] = True
            validated.append(opp)
            continue

        # Deadad
        print(f"  ! DEAD LINK: {opp.get('name','?')} -> {url}",
              flush=True)
        opp["url_status"] = "dead"; opp["url_confidence"] = "LOW"
        opp["url"] = ""
        opp["notes"] = (opp.get("notes") or "") + f"; [DEAD: {url}]"
        opp["url_verified"] = False
        validated.append(opp)

    return validated


def assign_tier(opp: dict) -> tuple[str, str]:
    deadline = opp.get("deadline")
    d = days_until(deadline) if deadline else None

    if d is not None and d <= 21:
        return "action", "🔴 Action This Week"
    if d is not None and d <= 60:
        return "prep", "🟡 Prep Now"

    combined = _norm(opp.get("snippet", "") + " " + opp.get("name", "") + " " + (opp.get("notes") or ""))
    prep_needed = any(re.search(p, combined) for p in PREP_PATTERNS.values())
    if prep_needed and d is not None and d <= 90:
        return "prep", "🟡 Prep Now"

    # CT / Local
    if re.search(r"\b(?:CT|Connecticut|Hartford|New Haven|Stamford|Fairfield)\b", combined):
        return "ct_local", "📍 CT / Local"

    if d is not None:
        return "watch", "🟢 Watch List"

    return "watch", "🟢 Watch List"


# ─── HTML Generation ─────────────────────────────────────────────────────

def _render_name_and_badge(opp: dict) -> str:
    name = str(opp.get("name","")).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;").replace("'","&#39;")
    url = opp.get("url","")
    status = opp.get("url_status","")
    confidence = opp.get("url_confidence","")
    # Build badges based on validation status
    badges = []
    if confidence == "LOW":
        badges.append("<span style=\"background:#e74c3c;color:#fff;padding:1px 8px;border-radius:12px;font-size:10px;font-weight:700;margin-left:8px;vertical-align:middle;\">LOW</span>")
    if status == "dead":
        badges.append("<span style=\"background:#e74c3c;color:#fff;padding:1px 8px;border-radius:12px;font-size:10px;font-weight:700;margin-left:8px;vertical-align:middle;\">DEAD LINK</span>")
    elif status == "empty_page":
        badges.append("<span style=\"background:#e67e22;color:#fff;padding:1px 8px;border-radius:12px;font-size:10px;font-weight:700;margin-left:8px;vertical-align:middle;\">EMPTY PAGE</span>")
    elif status == "fc_error":
        badges.append("<span style=\"background:#95a5a6;color:#fff;padding:1px 8px;border-radius:12px;font-size:10px;font-weight:700;margin-left:8px;vertical-align:middle;\">FC ERROR</span>")
    badge_str = " ".join(badges)
    # Always render as link when URL exists — don't gate on validation
    if url:
        url_escaped = url.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;").replace("'","&#39;")
        return f"<a href=\"{url_escaped}\" target=\"_blank\" rel=\"noopener\" style=\"color:#e8eaed;text-decoration:none;font-weight:500;font-size:14px;\">{name}</a>{badge_str}"
    return f"<span style=\"color:#9aa0a6;font-weight:500;font-size:14px;\">{name}{badge_str}</span>"


def generate_html(opps: list[dict], run_date: str, agent_name: str = "ivy_2028", gone_opps: list[dict] | None = None, archive_opps: list[dict] | None = None) -> str:
    """Dark theme HTML matching the v2 spec aesthetic."""
    by_cat: dict[str, list[dict]] = {}
    for opp in opps:
        cat = opp.get("category", "other")
        by_cat.setdefault(cat, []).append(opp)

    for cat in by_cat:
        by_cat[cat].sort(key=lambda o: (days_until(o.get("deadline")) or 9999, o.get("name", "").lower()))

    attention = [o for o in opps if assign_tier(o)[0] == "action"]
    attention.sort(key=lambda o: (days_until(o.get("deadline")) or 9999))

    def _top10(opps):
        # Rank by spike fit, then urgency; exclude stale and passed deadlines
        cands = []
        for o in opps:
            if o.get("stale"):
                continue
            d = days_until(o.get("deadline"))
            if d is not None and d < 0:
                continue
            cands.append(o)
        cands.sort(key=lambda o: (-(o.get("spike_fit") or 0), days_until(o.get("deadline")) or 9999))
        return cands[:10]

    top10 = _top10(opps)

    total = len(opps)
    urgent = sum(1 for o in opps if assign_tier(o)[0] == "action")
    prep_count = sum(1 for o in opps if tag_prep(o.get("snippet", ""), o.get("name", "")))

    def _escape(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    def _urgency_color(deadline):
        d = days_until(deadline)
        if d is None:
            return "#4a5260"
        if d <= 7:
            return "#ff5c5c"
        if d <= 21:
            return "#f0a500"
        if d <= 60:
            return "#f0c040"
        return "#3ecf8e"

    def _cat_label(key):
        return CATEGORY_LABELS.get(key, key.replace("_", " ").title())

    _WOW_COLORS = {
        "new": ("#0a2e1a", "#3ecf8e", "🆕 New"),
        "updated": ("#1a2e00", "#a3e635", "🔄 Updated"),
        "stale": ("#3a3000", "#f0c040", "⏸ Stale"),
    }

    def _wow_badge(status):
        if status not in _WOW_COLORS:
            return ""
        bg, fg, label = _WOW_COLORS[status]
        return f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:4px;font-size:10px;font-weight:500;margin-left:6px;">{label}</span>'

    def _spike_badge(opp):
        s = opp.get("spike_fit")
        if s is None:
            return ""
        return f'<span style="background:#2a1a3a;color:#c084fc;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;margin-left:6px;">Spike fit: {s}</span>'

    def _deadline_hint(opp):
        # Researchers supply deadline_confidence ("high"/"low"); missing = low.
        # Nudge verification for near-term deadlines that aren't high-confidence.
        d = days_until(opp.get("deadline"))
        if d is not None and 0 <= d <= 30 and opp.get("deadline_confidence") != "high":
            return ' <span style="color:#f0a500;font-size:11px;">&#9888; confirm date on official site</span>'
        return ""

    def _attention_card(opp, show_spike=False):
        d = days_until(opp.get("deadline"))
        color = _urgency_color(opp.get("deadline"))
        tags = tag_prep(opp.get("snippet", ""), opp.get("name", ""), opp.get("notes", ""))
        tag_badges = ""
        tmap = {"Essay": "#fce7f3,#be185d", "Rec Letter": "#fef3c7,#92400e", "Portfolio": "#dbeafe,#1e40af",
                "Test Score": "#ede9fe,#5b21b6", "Research Abstract": "#d1fae5,#065f46",
                "Audition": "#fce7f3,#be185d", "Nomination": "#fef3c7,#92400e", "Transcript": "#e0e7ff,#4338ca"}
        for t in tags:
            colors = tmap.get(t, "#f1f5f9,#64748b")
            bg, fg = colors.split(",")
            tag_badges += f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:4px;font-size:10px;font-weight:500;">{t}</span> '

        deadline_str = opp.get("deadline", "TBD")
        stale = opp.get("stale", False)
        stale_badge = '<span style="background:#3a3000;color:#f0c040;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:500;margin-left:6px;">⚠ prior year date</span>' if stale else ""
        wow_badge = _wow_badge(opp.get("wow_status", ""))
        et = opp.get("ethnic_tags", "")
        ethnic_badges = "".join(
            f'<span style="background:#1a1a3a;color:#8b8bff;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:500;">{_escape(t.strip())}</span> '
            for t in et.split(",") if t.strip()
        )
        snippet = _escape(opp.get("snippet", "")[:200])

        return f"""<div style="background:#1c1f23;border:1px solid #2a2d33;border-left:3px solid {color};border-radius:8px;padding:16px 20px;">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:8px;">
    {_render_name_and_badge(opp)}
    <span style="background:{color};color:#fff;padding:2px 10px;border-radius:20px;font-size:11px;font-weight:600;white-space:nowrap;">{deadline_str}{f' ({d}d)' if d is not None else ''}</span>{stale_badge}{wow_badge}{_spike_badge(opp) if show_spike else ''}{_deadline_hint(opp)}
  </div>
  <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px;">
    <span style="background:#1e3a5f;color:#6b9fff;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:500;">{_cat_label(opp.get('category',''))}</span>
    {tag_badges}{ethnic_badges}
    {f'<span style="background:#1a2e1a;color:#3ecf8e;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:500;">Aid Available</span>' if opp.get('aid') else ''}
  </div>
  <p style="color:#8a9099;font-size:12px;line-height:1.5;margin:0;">{snippet}</p>
</div>"""

    top10_cards = "\n".join(_attention_card(o, show_spike=True) for o in top10) \
        if top10 else '<p style="color:#4a5260;">No qualifying opportunities this week.</p>'

    def _table_rows(items):
        rows = ""
        for opp in items:
            d = days_until(opp.get("deadline"))
            color = _urgency_color(opp.get("deadline"))
            dl = opp.get("deadline", "TBD")
            stale = opp.get("stale", False)
            stale_badge = '<span style="background:#3a3000;color:#f0c040;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:500;margin-left:6px;">⚠ prior year date</span>' if stale else ""
            wow_badge = _wow_badge(opp.get("wow_status", ""))
            et = opp.get("ethnic_tags", "")
            ethnic_badges = "".join(
                f'<span style="background:#1a1a3a;color:#8b8bff;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:500;margin-left:4px;">{_escape(t.strip())}</span> '
                for t in et.split(",") if t.strip()
            )
            tags = tag_prep(opp.get("snippet", ""), opp.get("name", ""), opp.get("notes", ""))
            tag_str = ", ".join(tags) if tags else "—"
            rows += f"""<tr>
  <td style="padding:10px 12px;border-bottom:1px solid #2a2d33;">{_render_name_and_badge(opp)}</td>
  <td style="padding:10px 12px;border-bottom:1px solid #2a2d33;color:#8a9099;font-size:13px;"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{color};margin-right:6px;"></span>{dl}{f' ({d}d)' if d is not None else ''}{stale_badge}{wow_badge}{_deadline_hint(opp)}</td>
  <td style="padding:10px 12px;border-bottom:1px solid #2a2d33;color:#8a9099;font-size:13px;">{_escape(opp.get('cost','—')) if opp.get('cost') else '—'}</td>
  <td style="padding:10px 12px;border-bottom:1px solid #2a2d33;color:#8a9099;font-size:13px;">{tag_str}</td>
  <td style="padding:10px 12px;border-bottom:1px solid #2a2d33;color:#8a9099;font-size:12px;">{ethnic_badges}</td>
</tr>"""
        return rows

    def _archive_panel(archive_opps):
        if not archive_opps:
            return ""
        rows = "".join(
            f"""<tr>
  <td style="padding:10px 12px;border-bottom:1px solid #2a2d33;color:#8a9099;font-size:13px;">{_escape(a.get('name','Unknown'))}</td>
  <td style="padding:10px 12px;border-bottom:1px solid #2a2d33;color:#8a9099;font-size:13px;">{_cat_label(a.get('category','unknown'))}</td>
  <td style="padding:10px 12px;border-bottom:1px solid #2a2d33;color:#4a5260;font-size:12px;">Since {a.get('first_seen_run_date','?')}</td>
  <td style="padding:10px 12px;border-bottom:1px solid #2a2d33;"><a href="{_escape(a.get('url',''))}" target="_blank" rel="noopener" style="color:#38bdf8;font-size:12px;text-decoration:none;">Open &rarr;</a></td>
</tr>"""
            for a in archive_opps
        )
        return f"""<div class="tab-panel" id="tab-archive" style="display:none;">
  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:16px;">
    <h2 style="font-family:'DM Serif Display',serif;font-size:22px;font-weight:400;color:#8a9099;">🗂️ Archive — Every Opportunity Ever Found</h2>
    <span style="color:#4a5260;font-size:13px;">{len(archive_opps)} programs</span>
  </div>
  <p style="color:#4a5260;font-size:12px;margin-bottom:16px;">All opportunities accumulated across every run — kept forever with live links, even if not in this week's search.</p>
  <input id="archiveSearch" type="text" placeholder="🔍 Search archive (name, category, link)…" oninput="filterArchive(this)"
    style="width:100%;max-width:420px;padding:10px 14px;margin-bottom:16px;background:#15171a;border:1px solid #2a2d33;border-radius:6px;color:#e8eaed;font-size:13px;outline:none;">
  <div style="overflow-x:auto;">
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead><tr style="border-bottom:1px solid #353940;">
        <th style="padding:10px 12px;text-align:left;color:#4a5260;font-size:10px;text-transform:uppercase;letter-spacing:0.05em;font-weight:400;">Name</th>
        <th style="padding:10px 12px;text-align:left;color:#4a5260;font-size:10px;text-transform:uppercase;letter-spacing:0.05em;font-weight:400;">Category</th>
        <th style="padding:10px 12px;text-align:left;color:#4a5260;font-size:10px;text-transform:uppercase;letter-spacing:0.05em;font-weight:400;">Since</th>
        <th style="padding:10px 12px;text-align:left;color:#4a5260;font-size:10px;text-transform:uppercase;letter-spacing:0.05em;font-weight:400;">Link</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</div>
<script>
function filterArchive(inp){{
  var q = (inp.value || '').toLowerCase();
  document.querySelectorAll('#tab-archive tbody tr').forEach(function(tr){{
    tr.style.display = tr.textContent.toLowerCase().indexOf(q) > -1 ? '' : 'none';
  }});
}}
</script>"""

    def _gone_panel(gone_opps):
        if not gone_opps:
            return ""
        rows = "".join(
            f"""<tr>
  <td style="padding:10px 12px;border-bottom:1px solid #2a2d33;color:#8a9099;font-size:13px;">{_escape(g.get('name','Unknown'))}</td>
  <td style="padding:10px 12px;border-bottom:1px solid #2a2d33;color:#8a9099;font-size:13px;">{_cat_label(g.get('category','unknown'))}</td>
  <td style="padding:10px 12px;border-bottom:1px solid #2a2d33;color:#4a5260;font-size:12px;">Last seen {g.get('last_seen_run_date','?')}</td>
</tr>"""
            for g in gone_opps
        )
        return f"""<div class="tab-panel" id="tab-gone" style="display:none;">
  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:16px;">
    <h2 style="font-family:'DM Serif Display',serif;font-size:22px;font-weight:400;color:#8a9099;">💀 No Longer Found</h2>
    <span style="color:#4a5260;font-size:13px;">{len(gone_opps)} programs</span>
  </div>
  <p style="color:#4a5260;font-size:12px;margin-bottom:16px;">These opportunities appeared in previous runs but were not found in this week's search. May be seasonal, expired, or renamed.</p>
  <div style="overflow-x:auto;">
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead><tr style="border-bottom:1px solid #353940;">
        <th style="padding:10px 12px;text-align:left;color:#4a5260;font-size:10px;text-transform:uppercase;letter-spacing:0.05em;font-weight:400;">Name</th>
        <th style="padding:10px 12px;text-align:left;color:#4a5260;font-size:10px;text-transform:uppercase;letter-spacing:0.05em;font-weight:400;">Category</th>
        <th style="padding:10px 12px;text-align:left;color:#4a5260;font-size:10px;text-transform:uppercase;letter-spacing:0.05em;font-weight:400;">Status</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</div>"""

    cat_tabs = ""
    cat_panels = ""
    first = True
    for cat_key in ["science_math", "scholarships", "law_civics", "exams", "writing_humanities", "summer_programs", "general_competitions", "ct_local", "tech_ai", "fencing", "worldbuilding"]:
        items = by_cat.get(cat_key, [])
        label = _cat_label(cat_key)
        active = " background:#1c1f23;color:#e8eaed;" if first else ""
        first = False
        cat_tabs += f'<button class="tab" data-tab="{cat_key}" style="color:#64748b;{active}">{label} ({len(items)})</button>\n'
        table_rows = _table_rows(items)
        display = "block" if first else "none"
        cat_panels += f"""<div class="tab-panel" id="tab-{cat_key}" style="display:{display};">
  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:16px;">
    <h2 style="font-size:18px;font-weight:500;color:#e8eaed;font-family:'DM Serif Display',serif;">{label}</h2>
    <span style="color:#4a5260;font-size:13px;">{len(items)} opportunities</span>
  </div>
  <div style="overflow-x:auto;">
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead><tr style="border-bottom:1px solid #353940;">
        <th style="padding:10px 12px;text-align:left;color:#4a5260;font-size:10px;text-transform:uppercase;letter-spacing:0.05em;font-weight:400;">Opportunity</th>
        <th style="padding:10px 12px;text-align:left;color:#4a5260;font-size:10px;text-transform:uppercase;letter-spacing:0.05em;font-weight:400;">Deadline</th>
        <th style="padding:10px 12px;text-align:left;color:#4a5260;font-size:10px;text-transform:uppercase;letter-spacing:0.05em;font-weight:400;">Cost</th>
        <th style="padding:10px 12px;text-align:left;color:#4a5260;font-size:10px;text-transform:uppercase;letter-spacing:0.05em;font-weight:400;">Prep Needed</th>
        <th style="padding:10px 12px;text-align:left;color:#4a5260;font-size:10px;text-transform:uppercase;letter-spacing:0.05em;font-weight:400;">Ethnic</th>
      </tr></thead>
      <tbody>{table_rows}</tbody>
    </table>
  </div>
</div>"""

    # Attention panel
    attention_cards = "\n".join(_attention_card(o) for o in attention) if attention else '<p style="color:#4a5260;">Nothing urgent this week.</p>'

    # Build header stats
    cat_stats = "".join(
        f'<div style="background:#1a1d23;border:1px solid #2a2d33;border-radius:6px;padding:8px 14px;">'
        f'<span style="display:block;font-size:18px;font-weight:600;color:#e8eaed;">{len(by_cat.get(k,[]))}</span>'
        f'<span style="font-size:11px;color:#4a5260;">{_cat_label(k)}</span></div>'
        for k in ["science_math", "scholarships", "law_civics", "exams", "writing_humanities", "summer_programs", "general_competitions", "ct_local", "tech_ai", "fencing", "worldbuilding"]
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{agent_name.replace("_"," ").title()} Digest — {run_date}</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=DM+Sans:wght@300;400;500&display=swap');
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:#0e0f11; color:#e8eaed; font-family:'DM Sans',sans-serif; font-size:14px; line-height:1.6; min-height:100vh; }}
body::before {{ content:''; position:fixed; inset:0; background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.04'/%3E%3C/svg%3E"); pointer-events:none; z-index:100; opacity:0.4; }}
.page {{ max-width:1100px; margin:0 auto; padding:40px 32px 80px; position:relative; z-index:1; }}
.header {{ border-bottom:1px solid #2a2d33; padding-bottom:32px; margin-bottom:40px; }}
.header-label {{ font-size:11px; color:#f0a500; letter-spacing:0.15em; text-transform:uppercase; margin-bottom:12px; font-weight:500; }}
h1 {{ font-family:'DM Serif Display',serif; font-size:38px; font-weight:400; line-height:1.15; color:#e8eaed; margin-bottom:8px; }}
h1 em {{ color:#f0a500; font-style:italic; }}
.header-sub {{ color:#8a9099; font-size:13px; max-width:560px; }}
.notice {{ background:#1a1700; border:1px solid #f0a500; border-radius:6px; padding:12px 16px; margin-top:16px; font-size:12px; color:#f0c040; line-height:1.5; }}
.notice strong {{ color:#f0a500; }}
.stats-row {{ display:flex; gap:10px; margin-top:20px; flex-wrap:wrap; }}
.tab-bar {{ display:flex; gap:4px; margin-bottom:24px; flex-wrap:wrap; }}
.tab {{ padding:8px 16px; font-size:12px; font-weight:500; border:1px solid #2a2d33; background:#15171a; cursor:pointer; color:#64748b; border-radius:6px; white-space:nowrap; }}
.tab:hover {{ background:#1c1f23; color:#e8eaed; }}
.tab-panel {{ }}
.attention-grid {{ display:grid; gap:12px; }}
footer {{ text-align:center; padding:24px; color:#4a5260; font-size:11px; border-top:1px solid #2a2d33; margin-top:40px; }}
</style>
</head>
<body>
<div class="page">
  <div class="header">
    <div class="header-label">Weekly Digest</div>
    <h1>Ivy-<em>2028</em></h1>
    <div class="header-sub">{run_date} — Grade 10 &rarr; 11 (Class of 2028) — CT + National</div>
    <div class="stats-row">
      <div style="background:#1a1d23;border:1px solid #2a2d33;border-radius:6px;padding:8px 14px;">
        <span style="display:block;font-size:18px;font-weight:600;color:#e8eaed;">{total}</span>
        <span style="font-size:11px;color:#4a5260;">Total</span>
      </div>
      <div style="background:#1a1d23;border:1px solid #2a2d33;border-radius:6px;padding:8px 14px;">
        <span style="display:block;font-size:18px;font-weight:600;color:#ff5c5c;">{urgent}</span>
        <span style="font-size:11px;color:#4a5260;">Urgent</span>
      </div>
      <div style="background:#1a1d23;border:1px solid #2a2d33;border-radius:6px;padding:8px 14px;">
        <span style="display:block;font-size:18px;font-weight:600;color:#f0c040;">{prep_count}</span>
        <span style="font-size:11px;color:#4a5260;">Prep Needed</span>
      </div>
      {cat_stats}
    </div>
  </div>

  <div class="tab-bar">
    <button class="tab active" data-tab="attention" style="color:#f0a500;border-color:#f0a500;background:#1a1700;">⚠️ Attention ({len(attention)})</button>
    {cat_tabs}
    {f'<button class="tab" data-tab="gone" style="color:#4a5260;">💀 Gone ({len(gone_opps)})</button>' if gone_opps else ''}
    {f'<button class="tab" data-tab="archive" style="color:#4a5260;">🗂️ Archive ({len(archive_opps)})</button>' if archive_opps else ''}
  </div>

  <div style="margin-bottom:40px;">
    <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:16px;">
      <h2 style="font-family:'DM Serif Display',serif;font-size:22px;font-weight:400;color:#e8eaed;">🎯 Top 10 for the Strategy</h2>
      <span style="color:#4a5260;font-size:13px;">ranked by spike fit</span>
    </div>
    <div class="attention-grid">{top10_cards}</div>
  </div>

  <div class="tab-panel" id="tab-attention" style="display:block;">
    <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:16px;">
      <h2 style="font-family:'DM Serif Display',serif;font-size:22px;font-weight:400;color:#e8eaed;">⚠️ Attention</h2>
      <span style="color:#4a5260;font-size:13px;">{len(attention)} urgent items</span>
    </div>
    <div class="attention-grid">{attention_cards}</div>
  </div>

  {cat_panels}

  {_gone_panel(gone_opps) if gone_opps else ''}

  {_archive_panel(archive_opps) if archive_opps else ''}

  <footer>
    <p>{agent_name.replace('_',' ').title()} Weekly Researcher — Generated {run_date}</p>
    <p style="margin-top:4px;">Many programs show prior-year dates. Official 2026-2027 deadlines are published as sites update. Verify on official sites before acting.</p>
  </footer>
</div>

<script>
(function() {{
  var tabs = document.querySelectorAll('.tab');
  var panels = document.querySelectorAll('.tab-panel');
  tabs.forEach(function(tab) {{
    tab.addEventListener('click', function() {{
      tabs.forEach(function(t) {{ t.style.background='#15171a'; t.style.color='#64748b'; t.style.borderColor='#2a2d33'; }});
      this.style.background='#1c1f23'; this.style.color='#e8eaed'; this.style.borderColor='#353940';
      panels.forEach(function(p) {{ p.style.display='none'; }});
      var target = document.getElementById('tab-' + this.getAttribute('data-tab'));
      if (target) target.style.display='block';
    }});
  }});
}})();
</script>
</body>
</html>"""
    return html


def generate_csv(opps: list[dict]) -> str:
    fieldnames = ["name", "category", "tier", "deadline", "eligibility", "cost", "aid", "notes", "url", "source", "spike_fit", "effort"]
    import io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for opp in opps:
        tier_key, tier_label = assign_tier(opp)
        opp["tier"] = tier_label
        opp["spike_fit"] = opp.get("spike_fit", 0)
        opp["effort"] = ", ".join(tag_prep(opp.get("snippet", ""), opp.get("name", ""), opp.get("notes", "")))
        writer.writerow(opp)
    return buf.getvalue()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", default="ivy_2028", help="Agent name (maps to artifacts path, DB, and output)")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--validate-urls", action="store_true", help="Check every URL resolves via HTTP HEAD/GET")
    parser.add_argument("--firecrawl", "--fc", nargs="?", const="env", help="DEPRECATED and ignored: third-party fetch services were removed from this pipeline (Sep 2026). Kept only so old commands don't crash.")
    args = parser.parse_args()

    agent_name = args.agent
    run_date = args.date
    artifacts_dir = f"/tmp/ivy-2028/{agent_name}/{run_date}"
    db_path = os.path.join(PROJECT_DIR, "data", f"{agent_name}.db")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"{agent_name} v2 Coordinator — {run_date}", flush=True)
    print("=" * 50, flush=True)

    opps = load_artifacts(artifacts_dir)
    if not opps:
        print(f"No artifacts found at {artifacts_dir}. Subagents may not have run yet.", file=sys.stderr)
        sys.exit(1)

    print(f"\nLoaded {len(opps)} raw opportunities.", flush=True)

    opps = deduplicate(opps)
    print(f"After dedup: {len(opps)}", flush=True)

    opps = dedupe_against_db(opps, db_path)
    print(f"After cross-run dedupe: {len(opps)}", flush=True)

    opps = filter_eligible(opps)
    print(f"After eligibility filter: {len(opps)}", flush=True)

    for opp in opps:
        opp["spike_fit"] = spike_fit(opp)
    top_fit = sorted(opps, key=lambda o: -(o.get("spike_fit") or 0))[:10]
    print("\nTop spike-fit opportunities:", flush=True)
    for o in top_fit:
        print(f"  {o.get('spike_fit', 0):3d}  {o.get('name', '?')}", flush=True)

    if args.validate_urls:
        print("\nValidating URLs (own tooling: researcher-verified + plain-HTTP backstop)...", flush=True)
        opps = validate_urls(opps)
        dead = sum(1 for o in opps if "[DEAD:" in (o.get("notes") or ""))
        bot_blocked = sum(1 for o in opps if "[URL may block bots" in (o.get("notes") or ""))
        # Dead URLs kept in list (url cleared, [DEAD] annotated) but remain visible in digest
        print(f"After URL validation: {dead} dead links flagged, {bot_blocked} bot-blocked (URL kept), {len(opps)} entries total.\n", flush=True)

    # Reconcile fresh researcher URLs with archive canonicals (post-validation):
    # live fresh URLs rename the archive row; dead fresh URLs fall back to a
    # live canonical. Must run before the DB section below.
    opps = resolve_canonical_urls(opps, db_path)

    # — DB & WoW tracking —    # — DB & WoW tracking —
    init_db(db_path)
    insert_run(run_date, db_path=db_path)

    # Enrich with ethnic tags
    for opp in opps:
        opp["ethnic_tags"] = detect_ethnic_tags(opp)
        url = opp.get("url", "").rstrip("/")
        if url:
            save_opportunity(url, opp.get("name", ""), opp.get("category", "unknown"), run_date, opp["ethnic_tags"], db_path=db_path)

    # Compute WoW vs previous run
    prev_dates = get_previous_run_dates(run_date, db_path=db_path)
    wow_statuses: dict[str, str] = {}
    for opp in opps:
        url = opp.get("url", "").rstrip("/")
        opp["wow_status"] = "new"
        if url:
            wow_statuses[url] = "new"
    if prev_dates:
        prev_opps = get_run_opportunities(prev_dates[0], db_path=db_path)
        wow_statuses = compute_wow_statuses(opps, prev_opps)
        for opp in opps:
            url = opp.get("url", "").rstrip("/")
            opp["wow_status"] = wow_statuses.get(url, "new")

    # Persist this run and detect gone
    save_run_opportunities(run_date, opps, wow_statuses, db_path=db_path)
    current_urls = set(opp.get("url", "").rstrip("/") for opp in opps if opp.get("url"))
    if prev_dates:
        detect_gone(run_date, current_urls, prev_dates[0], db_path=db_path)

    gone_opps = get_gone_opportunities(run_date, db_path=db_path)
    archive_opps = get_all_opportunities(db_path=db_path)

    tier_counts: dict[str, int] = {}
    for opp in opps:
        tk, _ = assign_tier(opp)
        tier_counts[tk] = tier_counts.get(tk, 0) + 1
    for tk in ["action", "prep", "watch", "ct_local"]:
        print(f"  {tk}: {tier_counts.get(tk, 0)}", flush=True)

    html = generate_html(opps, run_date, agent_name, gone_opps, archive_opps)
    csv_content = generate_csv(opps)

    html_path = os.path.join(OUTPUT_DIR, f"{run_date}-{agent_name}-digest.html")
    csv_path = os.path.join(OUTPUT_DIR, f"{run_date}-{agent_name}-results.csv")

    with open(html_path, "w") as f:
        f.write(html)
    with open(csv_path, "w") as f:
        f.write(csv_content)

    # Stable alias so the site can always link to the latest digest
    latest_path = os.path.join(OUTPUT_DIR, f"latest-{agent_name}-digest.html")
    with open(latest_path, "w") as f:
        f.write(html)
    print(f"Stable alias: {latest_path}", flush=True)

    print(f"\nHTML: {html_path}", flush=True)
    print(f"CSV:  {csv_path}", flush=True)


if __name__ == "__main__":
    main()
