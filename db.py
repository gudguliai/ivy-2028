#!/usr/bin/env python3
"""
Ivy-2028 SQLite database layer.
Tracks runs, opportunities, WoW status, and disappeared programs.
"""

import re
import sqlite3
import os
import unicodedata
from datetime import date
from typing import Optional

DEFAULT_DB_DIR = os.path.dirname(os.path.abspath(__file__))


def _db_path(db_path: str | None) -> str:
    if db_path:
        return db_path
    return os.path.join(DEFAULT_DB_DIR, "data", "ivy_2028.db")


def _norm_url(url: str) -> str:
    return (url or "").rstrip("/")


def get_conn(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | None = None):
    """Create tables if they don't exist."""
    path = _db_path(db_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with get_conn(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_date TEXT PRIMARY KEY,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS opportunities (
                url TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                first_seen_run_date TEXT NOT NULL,
                ethnic_tags TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS run_opportunities (
                run_date TEXT NOT NULL,
                url TEXT NOT NULL,
                deadline TEXT,
                cost TEXT,
                snippet TEXT,
                stale INTEGER DEFAULT 0,
                wow_status TEXT DEFAULT 'new',
                notes TEXT,
                PRIMARY KEY (run_date, url),
                FOREIGN KEY (run_date) REFERENCES runs(run_date),
                FOREIGN KEY (url) REFERENCES opportunities(url)
            );

            CREATE TABLE IF NOT EXISTS run_gone (
                run_date TEXT NOT NULL,
                url TEXT NOT NULL,
                last_seen_run_date TEXT NOT NULL,
                name TEXT,
                category TEXT,
                PRIMARY KEY (run_date, url)
            );
        """)


def insert_run(run_date: str, db_path: str | None = None):
    with get_conn(db_path) as conn:
        conn.execute("INSERT OR IGNORE INTO runs (run_date) VALUES (?)", (run_date,))


def get_previous_run_dates(current_run_date: str, limit: int = 1, db_path: str | None = None) -> list[str]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT run_date FROM runs WHERE run_date < ? ORDER BY run_date DESC LIMIT ?",
            (current_run_date, limit),
        ).fetchall()
    return [r["run_date"] for r in rows]


def get_run_opportunities(run_date: str, db_path: str | None = None) -> dict[str, dict]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT url, deadline, stale, cost, snippet, notes FROM run_opportunities WHERE run_date = ?",
            (run_date,),
        ).fetchall()
    result = {}
    for r in rows:
        result[r["url"]] = dict(r)
    return result


def save_opportunity(url: str, name: str, category: str, run_date: str, ethnic_tags: str = "", db_path: str | None = None):
    with get_conn(db_path) as conn:
        existing = conn.execute(
            "SELECT ethnic_tags FROM opportunities WHERE url = ?", (url,)
        ).fetchone()
        if existing:
            existing_tags = set(t.strip() for t in existing["ethnic_tags"].split(",") if t.strip())
            new_tags = set(t.strip() for t in ethnic_tags.split(",") if t.strip())
            merged = ", ".join(sorted(existing_tags | new_tags))
            conn.execute(
                "UPDATE opportunities SET name=?, category=?, ethnic_tags=? WHERE url=?",
                (name, category, merged, url),
            )
        else:
            conn.execute(
                "INSERT INTO opportunities (url, name, category, first_seen_run_date, ethnic_tags) VALUES (?, ?, ?, ?, ?)",
                (url, name, category, run_date, ethnic_tags),
            )


def rename_opportunity_url(old_url: str, new_url: str, name: str = "", category: str = "unknown", db_path: str | None = None):
    """Point an existing archive row (and its run history) at a new URL.

    Used when a re-found program's fresh URL differs from the archive
    canonical: keeps one row per program with the live link, preserving
    first_seen_run_date and run history. If the new URL is already tracked,
    history folds into it and the old row is dropped.
    """
    old_url = _norm_url(old_url)
    new_url = _norm_url(new_url)
    if not old_url or not new_url or old_url == new_url:
        return False
    with get_conn(db_path) as conn:
        exists = conn.execute("SELECT 1 FROM opportunities WHERE url=?", (old_url,)).fetchone()
        if not exists:
            return False
        # FK is immediate (not deferrable): disable briefly while we move
        # both parent and child rows in one transaction.
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            # Move history row-by-row: where the new URL already has a row for
            # the same run (the program was tracked under both URLs), drop the
            # old duplicate instead of colliding on the PK.
            for (rd,) in conn.execute(
                "SELECT run_date FROM run_opportunities WHERE url=?", (old_url,)
            ).fetchall():
                clash = conn.execute(
                    "SELECT 1 FROM run_opportunities WHERE run_date=? AND url=?",
                    (rd, new_url),
                ).fetchone()
                if clash:
                    conn.execute(
                        "DELETE FROM run_opportunities WHERE run_date=? AND url=?",
                        (rd, old_url),
                    )
                else:
                    conn.execute(
                        "UPDATE run_opportunities SET url=? WHERE run_date=? AND url=?",
                        (new_url, rd, old_url),
                    )
            for (rd,) in conn.execute(
                "SELECT run_date FROM run_gone WHERE url=?", (old_url,)
            ).fetchall():
                clash = conn.execute(
                    "SELECT 1 FROM run_gone WHERE run_date=? AND url=?",
                    (rd, new_url),
                ).fetchone()
                if clash:
                    conn.execute(
                        "DELETE FROM run_gone WHERE run_date=? AND url=?",
                        (rd, old_url),
                    )
                else:
                    conn.execute(
                        "UPDATE run_gone SET url=? WHERE run_date=? AND url=?",
                        (new_url, rd, old_url),
                    )
            clash = conn.execute("SELECT 1 FROM opportunities WHERE url=?", (new_url,)).fetchone()
            if clash:
                conn.execute("DELETE FROM opportunities WHERE url=?", (old_url,))
            elif name:
                conn.execute("UPDATE opportunities SET url=?, name=?, category=? WHERE url=?",
                             (new_url, name, category, old_url))
            else:
                conn.execute("UPDATE opportunities SET url=? WHERE url=?", (new_url, old_url))
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
    return True


def save_run_opportunities(run_date: str, opps: list[dict], wow_statuses: dict[str, str], db_path: str | None = None):
    with get_conn(db_path) as conn:
        rows = []
        for opp in opps:
            url = _norm_url(opp.get("url", ""))
            if not url:
                continue
            rows.append((
                run_date,
                url,
                opp.get("deadline"),
                opp.get("cost"),
                (opp.get("snippet") or "")[:500],
                1 if opp.get("stale") else 0,
                wow_statuses.get(url, "new"),
                opp.get("notes"),
            ))
        conn.executemany(
            "INSERT OR REPLACE INTO run_opportunities (run_date, url, deadline, cost, snippet, stale, wow_status, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def detect_gone(run_date: str, current_urls: set[str], prev_run_date: str, db_path: str | None = None):
    prev = get_run_opportunities(prev_run_date, db_path=db_path)
    gone = set(prev.keys()) - current_urls
    if not gone:
        return
    with get_conn(db_path) as conn:
        for url in gone:
            info = conn.execute(
                "SELECT name, category FROM opportunities WHERE url = ?", (url,)
            ).fetchone()
            conn.execute(
                "INSERT OR REPLACE INTO run_gone (run_date, url, last_seen_run_date, name, category) VALUES (?, ?, ?, ?, ?)",
                (run_date, url, prev_run_date, info["name"] if info else "Unknown", info["category"] if info else "unknown"),
            )


def get_gone_opportunities(run_date: str, db_path: str | None = None) -> list[dict]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT url, last_seen_run_date, name, category FROM run_gone WHERE run_date = ? ORDER BY category, name",
            (run_date,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_opportunities(db_path: str | None = None) -> list[dict]:
    """Every opportunity ever found, deduped by URL, oldest first."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT url, name, category, first_seen_run_date FROM opportunities ORDER BY category, name"
        ).fetchall()
    return [dict(r) for r in rows]


def compute_wow_statuses(opps: list[dict], prev_opps: dict[str, dict]) -> dict[str, str]:
    """
    Compare current opportunities vs previous run.
    Returns {url: status} where status is 'new', 'updated', 'stale', or 'unchanged'.
    """
    statuses = {}
    for opp in opps:
        url = _norm_url(opp.get("url", ""))
        if not url:
            continue

        if url not in prev_opps:
            statuses[url] = "new"
        else:
            prev = prev_opps[url]
            prev_deadline = prev.get("deadline")
            prev_stale = bool(prev.get("stale", 0))
            cur_deadline = opp.get("deadline")
            cur_stale = bool(opp.get("stale"))

            if cur_stale and prev_stale and cur_deadline == prev_deadline:
                statuses[url] = "stale"
            elif cur_deadline != prev_deadline or cur_stale != prev_stale:
                statuses[url] = "updated"
            else:
                statuses[url] = "unchanged"

    return statuses


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s.lower().strip()


ETHNIC_KEYWORDS = [
    (r"\bindian\b.*\bamerican\b", "indian_american"),
    (r"\basian\b.*\bamerican\b", "asian_american"),
    (r"\bsouth\s+asian\b", "south_asian"),
    (r"\bindian\s+origin\b", "indian_origin"),
    (r"\bchildren\s+of\s+immigrant", "kids_of_immigrants"),
    (r"\bimmigrant\b", "kids_of_immigrants"),
    (r"\bfirst[-\s]generation\b", "kids_of_immigrants"),
    (r"\bminority\b", "minority"),
    (r"\bunderrepresented\b", "minority"),
    (r"\bheritage\b.*\bscholarship\b", "heritage"),
]


def detect_ethnic_tags(opp: dict) -> str:
    """Scan opportunity for ethnic eligibility keywords. Returns comma-separated tags."""
    # Start with subagent-provided tags
    tags = set()
    agent_tags = opp.get("ethnic_tags", "")
    if agent_tags:
        if isinstance(agent_tags, str):
            for t in agent_tags.split(","):
                tags.add(t.strip().lower().replace(" ", "_"))
        elif isinstance(agent_tags, list):
            for t in agent_tags:
                tags.add(str(t).strip().lower().replace(" ", "_"))

    # Keyword scan as fallback
    combined = (
        _norm(opp.get("snippet") or "")
        + " " + _norm(opp.get("name") or "")
        + " " + _norm(opp.get("eligibility_note") or "")
        + " " + _norm(opp.get("notes") or "")
    )
    for pattern, tag in ETHNIC_KEYWORDS:
        if re.search(pattern, combined):
            tags.add(tag)

    return ", ".join(sorted(tags))
