#!/usr/bin/env python3
"""
Ivy-2028 v2 Source Verifier
Reads the latest results CSV from output/, fetches each row's source URL with a
browser User-Agent, classifies the response, checks for claimed data, and writes
a per-row confidence verdict to a new CSV plus a short stdout summary.
"""

import argparse
import csv
import gzip
import html
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")

# Regex matching both new (ivy_2028) and legacy (ivy-2028) result CSV filenames.
_RE_CSV = re.compile(r"^(\d{4}-\d{2}-\d{2})-ivy[_-]2028-results\.csv$")

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Connection": "close",
}

# Lazy-loaded Firecrawl instance (set by --firecrawl flag).
_firecrawl_app = None


@dataclass
class FetchResult:
    status: int  # 0 if no response (DNS, conn refused, timeout)
    final_url: str
    body_text: str
    error: Optional[str]
    elapsed_ms: int


def _decode_body(body: bytes, charset: str | None = None) -> str:
    """Decode *body* with *charset*, falling back to utf-8 with replacement."""
    if charset:
        try:
            return body.decode(charset, errors="replace")
        except (LookupError, ValueError):
            pass
    return body.decode("utf-8", errors="replace")


def _decompress(body: bytes, content_encoding: str | None) -> bytes:
    """Decompress gzip/deflate *body* if *content_encoding* indicates it."""
    if not content_encoding:
        return body
    if content_encoding == "gzip":
        try:
            return gzip.decompress(body)
        except OSError:
            return body
    if content_encoding == "deflate":
        try:
            return gzip.decompress(body)
        except OSError:
            pass
        try:
            import zlib
            return zlib.decompress(body)
        except zlib.error:
            return body
    return body


def fetch(url: str, timeout: int = 15) -> FetchResult:
    """Fetch *url* with browser-like headers and return a ``FetchResult``.

    Network errors produce ``status=0`` with a populated ``error`` field.
    HTTP errors return the real status code and response body when available.
    Gzip/deflate content is transparently decompressed.  The response body is
    best-effort decoded (response charset, then utf-8 with replacement) and
    truncated to 500 KB.
    """
    start = time.monotonic()
    req = urllib.request.Request(url, headers=_BROWSER_HEADERS)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final_url = resp.url
            status = resp.status
            raw = resp.read()
            elapsed_ms = int((time.monotonic() - start) * 1000)

            content_encoding = resp.headers.get("Content-Encoding")
            raw = _decompress(raw, content_encoding)

            charset = None
            ct = resp.headers.get("Content-Type", "")
            m = re.search(r"charset=([\w-]+)", ct)
            if m:
                charset = m.group(1)

            body_text = _decode_body(raw, charset)
            if len(body_text) > 500_000:
                body_text = body_text[:500_000]

            return FetchResult(status=status, final_url=final_url,
                               body_text=body_text, error=None,
                               elapsed_ms=elapsed_ms)

    except urllib.error.HTTPError as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        status = e.code
        final_url = getattr(e, "url", url)
        raw = b""
        try:
            raw = e.read()
            ce = e.headers.get("Content-Encoding")
            raw = _decompress(raw, ce)
        except Exception:
            pass
        body_text = _decode_body(raw)
        if len(body_text) > 500_000:
            body_text = body_text[:500_000]
        return FetchResult(status=status, final_url=final_url,
                           body_text=body_text, error=str(e),
                           elapsed_ms=elapsed_ms)

    except (urllib.error.URLError, OSError, ValueError) as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return FetchResult(status=0, final_url=url, body_text="",
                           error=str(e), elapsed_ms=elapsed_ms)


def fetch_with_retry(url: str, timeout: int = 15,
                    retries: int = 2, backoff: int = 3) -> FetchResult:
    """Fetch *url* with retries on transient network errors.

    Retries only on ``status=0`` (DNS failure, connection refused, timeout).
    HTTP-level errors (4xx, 5xx) and bot walls are not retried — they're
    intentional server responses.
    """
    for attempt in range(retries + 1):
        result = fetch(url, timeout=timeout)
        if result.status != 0:
            return result
        if attempt < retries:
            delay = backoff * (attempt + 1)
            print(f"  retry {attempt+1}/{retries} for {url} "
                  f"(error={result.error}, waiting {delay}s)", flush=True)
            time.sleep(delay)
    return result


def fetch_with_firecrawl(url: str, timeout: int = 15) -> FetchResult:
    """Fetch *url* via Firecrawl, falling back to simple HTTP fetch.

    Firecrawl bypasses bot walls (Cloudflare, Captcha) by rendering pages
    with a headless browser.  If Firecrawl is unavailable or returns an
    error, falls back to the standard HTTP fetch.

    Returns a ``FetchResult`` identical to ``fetch()``.
    """
    start = time.monotonic()
    try:
        result = _firecrawl_app.scrape_url(
            url, formats=["markdown"], timeout=timeout * 1000,
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)

        # Firecrawl returns a Document pydantic model (not a dict).
        md = (result.markdown or "") if hasattr(result, "markdown") else ""
        md = md.strip()
        err = getattr(result.metadata, "error", None) if hasattr(result, "metadata") else None

        if md and len(md) > 200 and not err:
            return FetchResult(
                status=200, final_url=url, body_text=md,
                error=None, elapsed_ms=elapsed_ms,
            )

        if err:
            msg = f"firecrawl error: {err}"
        else:
            msg = f"firecrawl returned {len(md)} chars markdown (below 200 threshold)"

        print(f"  fc fallback ({msg}), trying HTTP...", flush=True)

    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        print(f"  fc exception: {e} ({elapsed_ms}ms), trying HTTP...", flush=True)

    return fetch_with_retry(url, timeout=timeout, retries=1, backoff=2)


_RE_BLOCKED = re.compile(
    r"cf-challenge|captcha|attention\\s+required|cloudflare|"
    r"access\\s+denied|are\\s+you\\s+a\\s+robot",
    re.IGNORECASE,
)

_RE_AMBIGUOUS = re.compile(
    r"verify\s+you\s+are\s+human|just\s+a\s+moment",
    re.IGNORECASE,
)

_BLOCK_STATUSES = frozenset({0, 401, 403, 429, 503})

MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# 3-letter abbreviated month names derived from MONTH_NAMES.
_ABBR_MONTHS = {name[:3]: num for name, num in MONTH_NAMES.items()}
# Combined lookup: "january" -> 1, "jan" -> 1, etc.
_ALL_MONTHS: dict[str, int] = {**MONTH_NAMES, **_ABBR_MONTHS}


def classify_block(result: FetchResult) -> tuple[str, str]:
    """Classify a fetch result as ok, blocked, or ambiguous.

    Returns ``(verdict, reason)`` where *verdict* is one of ``ok``, ``blocked``,
    or ``ambiguous``.  *reason* is a short machine-readable tag explaining the
    verdict (e.g. ``http_403``, ``conn_refused``, ``bot_wall_detected``).

    Blocked verdicts: HTTP 401/403/429/503, connection refused (status 0 with
    'refused' in error), generic network failure (status 0), or a challenge/bot
    wall page under 50 KB (Cloudflare, CAPTCHA, access denied, robot check).

    Ambiguous verdicts: status 200 with a short body (< 2 KB) containing weak
    challenge signals (``verify you are human``, ``just a moment``).

    Everything else is ``ok``.
    """
    body_text = result.body_text
    body_len = len(body_text)

    # Explicit block by status code.
    if result.status in _BLOCK_STATUSES:
        if result.status == 0 and result.error:
            err_lower = result.error.lower()
            if "refused" in err_lower:
                return ("blocked", "conn_refused")
            if "timeout" in err_lower or "timed out" in err_lower:
                return ("blocked", "timeout")
            return ("blocked", "network_error")
        return ("blocked", f"http_{result.status}")

    # Challenge / bot wall served as 200 with a short body.
    if body_len < 50_000 and _RE_BLOCKED.search(body_text):
        return ("blocked", "bot_wall_detected")

    # Ambiguous — 200 but minimal body with weak challenge signals.
    if result.status == 200 and body_len < 2_000 and _RE_AMBIGUOUS.search(body_text):
        return ("ambiguous", "bot_wall_ambiguous")

    return ("ok", "ok")


def _add_commas(n: str) -> str:
    """Add thousands-separator commas to a digit string (same as f"{int(n):,}")."""
    parts: list[str] = []
    for i, ch in enumerate(reversed(n)):
        if i > 0 and i % 3 == 0:
            parts.append(",")
        parts.append(ch)
    return "".join(reversed(parts))


def verify_claims(body_text: str, row: dict) -> dict:
    """Check whether the *body_text* contains the claims listed in *row*.

    Strips HTML, unescapes entities, collapses whitespace, and lowercases the
    text once, then checks for the presence of the row's ``deadline``, ``cost``,
    and ``aid`` (prize/aid) values.

    Empty claim fields return ``True`` (nothing to verify, don't penalize).
    Returns ``{deadline_found, cost_found, prize_found, matched_terms}``.
    Never raises on missing keys.
    """
    text = re.sub(r"<[^>]+>", " ", body_text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).lower()

    matched: list[str] = []

    deadline = (row.get("deadline") or "").strip()
    cost = (row.get("cost") or "").strip()
    aid = (row.get("aid") or "").strip()

    # --- Deadline ---
    deadline_found = True
    if deadline:
        deadline_found = False
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", deadline)
        if m:
            year, month_num_s, day_s = m.group(1), m.group(2), m.group(3)
            month_num = int(month_num_s)
            day_stripped = str(int(day_s))  # "01" -> "1"
            day_padded = day_s  # keep "01" as-is

            # Find full and abbreviated month name for this month number.
            month_name: str | None = None
            month_abbr: str | None = None
            for name, num in MONTH_NAMES.items():
                if num == month_num:
                    month_name = name
                    month_abbr = name[:3]
                    break

            if month_name and month_abbr:
                candidates: list[str] = []
                # YYYY-MM-DD
                candidates.append(deadline)
                # Month DD YYYY
                candidates.append(f"{month_name} {day_padded} {year}")
                candidates.append(f"{month_abbr} {day_padded} {year}")
                candidates.append(f"{month_name} {day_stripped} {year}")
                candidates.append(f"{month_abbr} {day_stripped} {year}")
                # Month DD, YYYY
                candidates.append(f"{month_name} {day_padded}, {year}")
                candidates.append(f"{month_abbr} {day_padded}, {year}")
                candidates.append(f"{month_name} {day_stripped}, {year}")
                candidates.append(f"{month_abbr} {day_stripped}, {year}")
                # DD Month YYYY (with and without zero-padded day)
                candidates.append(f"{day_padded} {month_name} {year}")
                candidates.append(f"{day_padded} {month_abbr} {year}")
                candidates.append(f"{day_stripped} {month_name} {year}")
                candidates.append(f"{day_stripped} {month_abbr} {year}")
                # Month YYYY alone
                candidates.append(f"{month_name} {year}")
                candidates.append(f"{month_abbr} {year}")

                for c in candidates:
                    if c in text:
                        deadline_found = True
                        matched.append(f"deadline:{deadline}")
                        break

    # --- Cost ---
    cost_found = True
    if cost and cost != "0":
        cost_found = False
        raw = cost.replace(",", "").replace("$", "")
        if raw.isdigit():
            patterns = [raw]
            if len(raw) > 3:
                patterns.append(_add_commas(raw))
            for p in patterns:
                # Check with $ prefix, with optional space
                if f"${p}" in text or f"$ {p}" in text:
                    cost_found = True
                    matched.append(f"cost:{cost}")
                    break
                # Check bare number
                if p in text:
                    cost_found = True
                    matched.append(f"cost:{cost}")
                    break

    # Also verify zero-cost claims say "free" / "no cost" / "no fee".
    if cost == "0" and not cost_found:
        if "free" in text or "no cost" in text or "no fee" in text:
            cost_found = True
            matched.append("cost:free")

    # --- Prize ---
    prize_found = True
    if aid:
        prize_found = False
        aid_lower = aid.lower()

        # Dollar amounts from the aid string: "$2000 scholarship" -> "2000"
        dollar_amts = re.findall(r"\$?\s*(\d{3,}(?:,\d{3})*)\b", aid)
        # Keywords from the aid string.
        aid_keywords = [kw for kw in ("stipend", "scholarship", "award", "prize", "financial aid")
                        if kw in aid_lower]

        # Check dollar amounts in text (must be ≥ $500 to be "standout").
        for amt_str in dollar_amts:
            raw_amt = int(amt_str.replace(",", ""))
            if raw_amt >= 500:
                amt_comma = f"{raw_amt:,}"   # e.g. "2,000"
                amt_plain = str(raw_amt)      # e.g. "2000"
                if (f"${amt_comma}" in text or f"${amt_plain}" in text
                        or f"$ {amt_comma}" in text or f"$ {amt_plain}" in text
                        or amt_plain in text):
                    prize_found = True
                    matched.append(f"aid:${amt_str}")
                    break

        # Check keywords in text (only if not already found via dollar).
        if not prize_found:
            for kw in aid_keywords:
                if kw in text:
                    prize_found = True
                    matched.append(f"aid:{kw}")
                    break
            # Also check text for any aid-related keyword, even if not in the
            # aid string (e.g. aid says "financial aid", body says "scholarship").
            if not prize_found:
                for kw in ("stipend", "scholarship", "award", "prize", "financial aid"):
                    if kw in text:
                        prize_found = True
                        matched.append(f"aid_text:{kw}")
                        break

    return {
        "deadline_found": deadline_found,
        "cost_found": cost_found,
        "prize_found": prize_found,
        "matched_terms": matched,
    }


def score_row(row: dict, fetch_result: FetchResult, block: tuple[str, str],
              claims: dict) -> dict:
    """Score a single source-verification row and return the verdict record.

    Returns a dict with keys:
      ``confidence``   — ``HIGH``, ``LOW``, or ``MANUAL_REVIEW``
      ``reason``       — machine-readable tag explaining the verdict
      ``blocked``      — ``True`` if the request was blocked (including ambiguous walls)
      ``http_status``  — the HTTP status code from the fetch (0 on network error)
      ``deadline_found``, ``cost_found``, ``prize_found`` — per-claim booleans
      ``matched_terms` — list of claim terms that matched
      ``final_url``    — the final resolved URL after redirects

    Confidence ladder (hard rules):
      - Blocked verdict &rarr; ``LOW``, reason from ``block[1]``.
      - Ambiguous verdict &rarr; ``MANUAL_REVIEW`` with reason ``bot_wall_ambiguous``.
      - OK verdict (site accessible) &rarr; ``HIGH``, reason ``site_accessible``.
        Claim matching (deadline/cost/prize found) is recorded as metadata only
        and does not affect confidence. The pipeline already extracted the data;
        the verifier's job is to confirm the source URL is real and reachable.
    """
    verdict, reason = block

    blocked = verdict != "ok"
    http_status = fetch_result.status
    final_url = fetch_result.final_url

    deadline_found = claims.get("deadline_found", True)
    cost_found = claims.get("cost_found", True)
    prize_found = claims.get("prize_found", True)
    matched_terms = claims.get("matched_terms", [])

    # Blocked overrides everything.
    if verdict == "blocked":
        return {
            "confidence": "LOW",
            "reason": reason,
            "blocked": True,
            "http_status": http_status,
            "deadline_found": False,
            "cost_found": False,
            "prize_found": False,
            "matched_terms": [],
            "final_url": final_url,
        }

    if verdict == "ambiguous":
        return {
            "confidence": "MANUAL_REVIEW",
            "reason": "bot_wall_ambiguous",
            "blocked": True,
            "http_status": http_status,
            "deadline_found": False,
            "cost_found": False,
            "prize_found": False,
            "matched_terms": [],
            "final_url": final_url,
        }

    # verdict == "ok" — site is accessible.
    # If the site responded, mark HIGH. Claim matching is metadata,
    # not a confidence gate — the pipeline already extracted the data.
    return {
        "confidence": "HIGH",
        "reason": "site_accessible",
        "blocked": False,
        "http_status": http_status,
        "deadline_found": deadline_found,
        "cost_found": cost_found,
        "prize_found": prize_found,
        "matched_terms": matched_terms,
        "final_url": final_url,
    }


def find_latest_csv(output_dir: str) -> str:
    """Return the path to the newest results CSV in *output_dir* by date prefix.

    Matches ``YYYY-MM-DD-ivy_2028-results.csv`` (new format) and the legacy
    ``YYYY-MM-DD-ivy-2028-results.csv``.  Chooses the file with the latest
    date, which keeps results stable across re-runs regardless of mtime.

    Raises ``FileNotFoundError`` when no matching file exists.
    """
    candidates: list[tuple[str, str]] = []
    for name in os.listdir(output_dir):
        m = _RE_CSV.match(name)
        if m:
            candidates.append((m.group(1), os.path.join(output_dir, name)))
    if not candidates:
        raise FileNotFoundError(
            f"No results CSV found in {output_dir!r} "
            f"(expected YYYY-MM-DD-ivy_2028-results.csv)"
        )
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]


def verify_csv(in_path: str, out_path: str, workers: int = 6, timeout: int = 15, limit: int = 0) -> dict:
    """Read a results CSV, verify each source URL, write verdicts to a new CSV.

    Reads the CSV at *in_path* with ``csv.DictReader``, groups rows by host
    for per-host serialized fetches via ``ThreadPoolExecutor``, calls
    ``score_row`` for each, and writes an output CSV at *out_path* mirroring
    the input columns plus verification columns.  Writes atomically via a
    ``.tmp`` intermediate file.

    Returns ``{total, high, low, manual_review, blocked, errors}``.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from datetime import datetime, timezone
    from urllib.parse import urlsplit
    import collections

    # ── Read input CSV ──────────────────────────────────────────────────
    with open(in_path, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    if limit > 0:
        rows = rows[:limit]

    total = len(rows)
    verified_at = datetime.now(timezone.utc).isoformat()

    # ── Group row indices by host ──────────────────────────────────────
    host_groups: dict[str, list[int]] = collections.defaultdict(list)
    no_url_indices: list[int] = []
    for idx, row in enumerate(rows):
        url = (row.get("url") or "").strip()
        if url:
            netloc = urlsplit(url).netloc
            if netloc:
                host_groups[netloc].append(idx)
                continue
        no_url_indices.append(idx)

    results: list[dict | None] = [None] * total

    # Pre-fill empty-URL rows immediately (no fetch needed).
    EMPTY_URL_VERDICT = {
        "confidence": "MANUAL_REVIEW",
        "reason": "no_url",
        "blocked": False,
        "http_status": 0,
        "final_url": "",
        "deadline_found": False,
        "cost_found": False,
        "prize_found": False,
        "matched_terms": [],
    }
    for idx in no_url_indices:
        results[idx] = dict(EMPTY_URL_VERDICT)

    if not host_groups and no_url_indices:
        print(f"warning: {len(no_url_indices)} rows have no URL — all marked MANUAL_REVIEW", flush=True)

    # ── Per-host worker ────────────────────────────────────────────────
    def process_host(indices: list[int]) -> list[tuple[int, dict]]:
        """Sequentially fetch and score all rows sharing one host."""
        out: list[tuple[int, dict]] = []
        for i, idx in enumerate(indices):
            row = rows[idx]
            url = (row.get("url") or "").strip()
            if not url:
                continue  # already handled
            fr = (fetch_with_firecrawl(url, timeout=timeout)
                  if _firecrawl_app else
                  fetch_with_retry(url, timeout=timeout))
            block = classify_block(fr)
            claims = verify_claims(fr.body_text, row) if fr.body_text else {
                "deadline_found": False, "cost_found": False,
                "prize_found": False, "matched_terms": [],
            }
            verdict = score_row(row, fr, block, claims)
            out.append((idx, verdict))
            if i < len(indices) - 1:
                time.sleep(1.0)
        return out

    # ── Execute ────────────────────────────────────────────────────────
    completed = 0
    high = low = manual_review = blocked = errors = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_host, indices): indices
                   for indices in host_groups.values()}

        for future in as_completed(futures):
            for idx, verdict in future.result():
                results[idx] = verdict
                conf = verdict["confidence"]
                if conf == "HIGH":
                    high += 1
                elif conf == "LOW":
                    low += 1
                else:
                    manual_review += 1
                if verdict.get("blocked"):
                    blocked += 1
                if verdict["http_status"] == 0 and verdict["reason"] != "no_url":
                    errors += 1
                completed += 1
                if completed % 10 == 0:
                    print(f"verified {completed}/{total} "
                          f"(HIGH={high} LOW={low} REVIEW={manual_review})",
                          flush=True)

    # Count no-url rows in totals.
    for idx in no_url_indices:
        if results[idx] is not None:
            manual_review += 1

    # ── Write output CSV ───────────────────────────────────────────────
    out_fields: list[str] = list(fieldnames) + [
        "confidence", "reason", "blocked", "http_status", "final_url",
        "deadline_found", "cost_found", "prize_found", "verified_at",
    ]

    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        writer.writeheader()
        for idx, row in enumerate(rows):
            vr = results[idx]
            out_row = dict(row)
            out_row.update({
                "confidence": vr["confidence"],
                "reason": vr["reason"],
                "blocked": str(vr["blocked"]),
                "http_status": str(vr["http_status"]),
                "final_url": vr["final_url"],
                "deadline_found": str(vr["deadline_found"]),
                "cost_found": str(vr["cost_found"]),
                "prize_found": str(vr["prize_found"]),
                "verified_at": verified_at,
            })
            writer.writerow(out_row)

    os.replace(tmp_path, out_path)

    print(f"verified {total}/{total} (HIGH={high} LOW={low} REVIEW={manual_review})",
          flush=True)

    return {
        "total": total,
        "high": high,
        "low": low,
        "manual_review": manual_review,
        "blocked": blocked,
        "errors": errors,
    }


def _selftest() -> int:
    """Unit-test ``classify_block`` with hand-built fixtures (no network).

    Returns 0 on all-pass, 1 if any test fails.
    """
    passed = 0
    failed = 0

    def check(label: str, result: FetchResult, want_verdict: str, want_reason: str) -> None:
        nonlocal passed, failed
        got_verdict, got_reason = classify_block(result)
        if got_verdict == want_verdict and got_reason == want_reason:
            passed += 1
        else:
            print(f"FAIL {label}: "
                  f"expected ({want_verdict}, {want_reason!r}), "
                  f"got ({got_verdict}, {got_reason!r})")
            failed += 1

    # 403 with empty body → blocked, http_403
    check("403 empty", FetchResult(403, "https://e.com", "", None, 100),
          "blocked", "http_403")
    # 403 with some body → blocked, http_403
    check("403 body", FetchResult(403, "https://e.com", "forbidden", None, 100),
          "blocked", "http_403")

    # Status 0, error='Connection refused' → blocked, conn_refused
    check("conn_refused",
          FetchResult(0, "https://e.com", "", "Connection refused", 5000),
          "blocked", "conn_refused")

    # Status 0, error='Name or service not known' → blocked, network_error
    check("dns_fail",
          FetchResult(0, "https://e.com", "", "Name or service not known", 5000),
          "blocked", "network_error")

    # 200, body contains Cloudflare challenge with body < 50 KB → blocked
    cf_body = "<html>Just a moment... checking your browser... Cloudflare</html>"
    check("cloudflare_challenge",
          FetchResult(200, "https://e.com", cf_body, None, 500),
          "blocked", "bot_wall_detected")

    # 200, body with 'just a moment' under 2 KB → ambiguous
    check("just_a_moment",
          FetchResult(200, "https://e.com",
                      "<html>please wait just a moment while we verify you are human</html>",
                      None, 500),
          "ambiguous", "bot_wall_ambiguous")

    # 200, normal HTML no challenge → ok
    check("normal_200",
          FetchResult(200, "https://e.com",
                      "<html><body><h1>Welcome</h1><p>deadline: 2027-01-10</p>"
                      "<p>cost: $6500</p><p>prize: scholarship</p></body></html>",
                      None, 300),
          "ok", "ok")

    # 200, normal HTML > 50 KB that mentions 'captcha' in content → ok (not blocked)
    big_body = ("<p>captcha</p>" * 10_000)[:55_000]
    check("big_body_mentions_captcha",
          FetchResult(200, "https://e.com", big_body, None, 500),
          "ok", "ok")

    # 429 → blocked
    check("http_429",
          FetchResult(429, "https://e.com", "too many requests", None, 200),
          "blocked", "http_429")

    # 401 → blocked
    check("http_401",
          FetchResult(401, "https://e.com", "unauthorized", None, 200),
          "blocked", "http_401")

    # 503 → blocked
    check("http_503",
          FetchResult(503, "https://e.com", "service unavailable", None, 200),
          "blocked", "http_503")

    # Status 0 with timeout error
    check("timeout",
          FetchResult(0, "https://e.com", "", "timed out", 15000),
          "blocked", "timeout")

    # --- score_row tests ---
    def check_score(label: str, row: dict, fr: FetchResult, verdict: str, reason: str,
                    want: dict) -> None:
        nonlocal passed, failed
        got = score_row(row, fr, (verdict, reason),
                        verify_claims(fr.body_text, row) if fr.body_text
                        else {"deadline_found": False, "cost_found": False,
                              "prize_found": False, "matched_terms": []})
        ok = True
        for k, v in want.items():
            if got.get(k) != v:
                ok = False
                break
        # Also verify no unexpected confidence value.
        if got["confidence"] not in ("HIGH", "LOW", "MANUAL_REVIEW"):
            ok = False
        if ok:
            passed += 1
        else:
            print(f"FAIL {label}: want={want!r}, got={got!r}")
            failed += 1

    # Blocked → LOW
    fr = FetchResult(403, "https://blocked.com", "forbidden", None, 100)
    check_score("blocked_403",
                {"url": "https://blocked.com", "deadline": "", "cost": "", "aid": ""},
                fr, "blocked", "http_403",
                {"confidence": "LOW", "reason": "http_403", "blocked": True, "http_status": 403})

    # Ambiguous → MANUAL_REVIEW
    fr = FetchResult(200, "https://ambig.com",
                     "<html>please wait just a moment while we verify you are human</html>",
                     None, 500)
    check_score("ambiguous",
                {"url": "https://ambig.com", "deadline": "", "cost": "", "aid": ""},
                fr, "ambiguous", "bot_wall_ambiguous",
                {"confidence": "MANUAL_REVIEW", "reason": "bot_wall_ambiguous",
                 "blocked": True, "http_status": 200})

    # OK + all 3 claims ⇒ HIGH
    fr = FetchResult(200, "https://match.com",
                     "<html>deadline: 2027-01-10 cost: $6500 prize: scholarship</html>",
                     None, 300)
    check_score("ok_all_claims",
                {"url": "https://match.com", "deadline": "2027-01-10", "cost": "6500",
                 "aid": "scholarship"},
                fr, "ok", "ok",
                {"confidence": "HIGH", "reason": "site_accessible", "blocked": False,
                 "http_status": 200})

    # OK + cost missing ⇒ LOW with claims_missing:cost
    fr = FetchResult(200, "https://missing-cost.com",
                     "<html>deadline: 2027-01-10 prize: scholarship</html>",
                     None, 300)
    check_score("ok_cost_missing",
                {"url": "https://missing-cost.com", "deadline": "2027-01-10", "cost": "6500",
                 "aid": "scholarship"},
                fr, "ok", "ok",
                {"confidence": "HIGH", "reason": "site_accessible",
                 "blocked": False, "http_status": 200})

    # OK + all claims missing ⇒ still HIGH (site is accessible)
    fr = FetchResult(200, "https://all-missing.com",
                     "<html>no data here</html>",
                     None, 300)
    check_score("ok_all_missing",
                {"url": "https://all-missing.com", "deadline": "2027-06-15", "cost": "5000",
                 "aid": "stipend"},
                fr, "ok", "ok",
                {"confidence": "HIGH", "reason": "site_accessible",
                 "blocked": False, "http_status": 200})

    # OK + no claims in row ⇒ MANUAL_REVIEW no_claims_to_verify
    fr = FetchResult(200, "https://no-claims.com",
                     "<html>some page</html>",
                     None, 300)
    check_score("ok_no_claims",
                {"url": "https://no-claims.com", "deadline": "", "cost": "", "aid": ""},
                fr, "ok", "ok",
                {"confidence": "HIGH", "reason": "site_accessible",
                 "blocked": False, "http_status": 200})

    # --- verify_claims tests ---
    def check_claims(label: str, body: str, row: dict, want: dict) -> None:
        nonlocal passed, failed
        got = verify_claims(body, row)
        ok = all(got.get(k) == v for k, v in want.items())
        if ok:
            passed += 1
        else:
            print(f"FAIL {label}: want={want!r}, got={ {k: got.get(k) for k in want}!r}")
            failed += 1

    check_claims("date_jan10_comma",
                 "Apply by Jan 10, 2027 for the program.",
                 {"deadline": "2027-01-10", "cost": "", "aid": ""},
                 {"deadline_found": True})
    check_claims("cost_comma_dollar",
                 "Program fee is $6,500 including housing.",
                 {"deadline": "", "cost": "6500", "aid": ""},
                 {"cost_found": True})
    check_claims("aid_keyword_scholarship",
                 "Need-based scholarship available to all admitted students.",
                 {"deadline": "", "cost": "", "aid": "Need-based financial aid available"},
                 {"prize_found": True})

    summary = f"_selftest: {passed} passed, {failed} failed"
    if failed:
        print(f"FAIL {summary}")
        return 1
    print(f"OK {summary}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify source URLs in Ivy-2028 results CSV.")
    parser.add_argument("--csv", default="",
                        help="Input CSV path (default: auto-pick latest from output/)")
    parser.add_argument("--out", default="",
                        help="Output CSV path (default: derived from input with -verified suffix)")
    parser.add_argument("--workers", type=int, default=6,
                        help="Max concurrent workers (default: 6)")
    parser.add_argument("--timeout", type=int, default=15,
                        help="HTTP request timeout in seconds (default: 15)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Process only first N rows; 0 = all (default: 0)")
    parser.add_argument("--selftest", action="store_true",
                        help="Run unit tests and exit")
    parser.add_argument("--firecrawl", "--fc", nargs="?", const="env",
                        default=None,
                        help="Enable Firecrawl to bypass bot walls.  "
                             "Pass API key inline, or omit to read "
                             "from FIRECRAWL_API_KEY env var.")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()

    # ── Init Firecrawl (optional) ──────────────────────────────────────
    global _firecrawl_app
    if args.firecrawl is not None:
        fc_key = args.firecrawl if args.firecrawl != "env" else None
        fc_key = fc_key or os.environ.get("FIRECRAWL_API_KEY")
        if not fc_key:
            print("error: --firecrawl requires FIRECRAWL_API_KEY or inline key")
            return 1
        try:
            from firecrawl import Firecrawl
            _firecrawl_app = Firecrawl(api_key=fc_key)
            print("Firecrawl enabled", flush=True)
        except ImportError:
            print("error: firecrawl-py not installed. Run: "
                  "pip3.13 install firecrawl-py")
            return 1

    # Resolve input path.
    csv_path = args.csv
    if not csv_path:
        csv_path = find_latest_csv(OUTPUT_DIR)

    csv_path = os.path.abspath(csv_path)

    # Derive output path.
    out_path = args.out
    if not out_path:
        base, ext = os.path.splitext(csv_path)
        out_path = base + "-verified" + ext

    out_path = os.path.abspath(out_path)

    print(f"Input:  {csv_path}", flush=True)
    print(f"Output: {out_path}", flush=True)
    print(flush=True)

    counts = verify_csv(csv_path, out_path,
                        workers=args.workers,
                        timeout=args.timeout,
                        limit=args.limit)

    print(flush=True)
    print(f"Total:         {counts['total']}", flush=True)
    print(f"  HIGH:        {counts['high']}", flush=True)
    print(f"  LOW:         {counts['low']}", flush=True)
    print(f"  MANUAL_REVIEW: {counts['manual_review']}", flush=True)
    print(f"  Blocked:     {counts['blocked']}", flush=True)
    print(f"  Errors:      {counts['errors']}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
