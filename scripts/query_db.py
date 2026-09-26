#!/usr/bin/env python3
"""Query the ivy-2028 opportunity archive ("treasure trove").

The SQLite DB at data/ivy_2028.db holds every opportunity ever found
(1,002+ across 22 weekly runs since May 2026), with per-run snapshots
(deadline, cost, snippet, notes), week-over-week status, and a log of
programs that disappeared.

Usage:
  query_db.py search <keyword> [--category CAT] [--limit N]
  query_db.py category <cat> [--limit N]
  query_db.py deadlines [--days N]            # upcoming deadlines from latest run
  query_db.py new [--since YYYY-MM-DD]        # opportunities first seen since date
  query_db.py gone [--limit N]                # programs that disappeared
  query_db.py stats                            # counts by category, runs, date range
  query_db.py show <url-substring>            # full history of one opportunity
"""
import sqlite3
import sys
import os
from datetime import date, timedelta

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "ivy_2028.db")


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def latest_run(c):
    return c.execute("SELECT MAX(run_date) FROM runs").fetchone()[0]


def cmd_stats(c):
    print("runs:", c.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
    print("date range:", c.execute("SELECT MIN(run_date), MAX(run_date) FROM runs").fetchone()[0:2])
    print("opportunities (all time):", c.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0])
    print("gone (disappeared):", c.execute("SELECT COUNT(*) FROM run_gone").fetchone()[0])
    print("\nby category:")
    for r in c.execute("SELECT category, COUNT(*) n FROM opportunities GROUP BY category ORDER BY n DESC"):
        print(f"  {r['category']:22s} {r['n']}")


def cmd_search(c, kw, category=None, limit=20):
    lr = latest_run(c)
    q = """SELECT o.name, o.category, r.deadline, r.cost, o.url
           FROM opportunities o JOIN run_opportunities r ON r.url=o.url AND r.run_date=?
           WHERE (LOWER(o.name) LIKE ? OR LOWER(r.snippet) LIKE ? OR LOWER(r.notes) LIKE ?)"""
    params = [lr, f"%{kw.lower()}%", f"%{kw.lower()}%", f"%{kw.lower()}%"]
    if category:
        q += " AND o.category=?"
        params.append(category)
    q += " ORDER BY r.deadline LIMIT ?"
    params.append(limit)
    for r in c.execute(q, params):
        print(f"- {r['name']} [{r['category']}] dl={r['deadline'] or 'TBD'}")
        print(f"  {r['url']}")


def cmd_category(c, cat, limit=30):
    lr = latest_run(c)
    for r in c.execute(
        """SELECT o.name, r.deadline, o.url FROM opportunities o
           JOIN run_opportunities r ON r.url=o.url AND r.run_date=?
           WHERE o.category=? ORDER BY r.deadline LIMIT ?""", (lr, cat, limit)):
        print(f"- {r['name']} dl={r['deadline'] or 'TBD'}")
        print(f"  {r['url']}")


def cmd_deadlines(c, days=60):
    lr = latest_run(c)
    today = date.today().isoformat()
    horizon = (date.today() + timedelta(days=days)).isoformat()
    print(f"upcoming deadlines (next {days}d, as of run {lr}):")
    for r in c.execute(
        """SELECT o.name, o.category, r.deadline, o.url FROM opportunities o
           JOIN run_opportunities r ON r.url=o.url AND r.run_date=?
           WHERE r.deadline >= ? AND r.deadline <= ? AND r.stale=0
           ORDER BY r.deadline""", (lr, today, horizon)):
        print(f"{r['deadline']}  [{r['category']}] {r['name']}")
        print(f"    {r['url']}")


def cmd_new(c, since):
    for r in c.execute(
        "SELECT name, category, url, first_seen_run_date FROM opportunities WHERE first_seen_run_date >= ? ORDER BY first_seen_run_date DESC, name",
        (since,)):
        print(f"{r['first_seen_run_date']}  [{r['category']}] {r['name']}")
        print(f"    {r['url']}")


def cmd_gone(c, limit=30):
    for r in c.execute(
        "SELECT name, category, url, run_date, last_seen_run_date FROM run_gone ORDER BY run_date DESC LIMIT ?",
        (limit,)):
        print(f"gone {r['run_date']} (last seen {r['last_seen_run_date']}) [{r['category']}] {r['name']}")


def cmd_show(c, frag):
    rows = c.execute("SELECT * FROM opportunities WHERE url LIKE ?", (f"%{frag}%",)).fetchall()
    if not rows:
        print("no match")
        return
    o = rows[0]
    print(f"{o['name']} [{o['category']}] first seen {o['first_seen_run_date']}")
    print(o['url'], "\ntags:", o['ethnic_tags'])
    for r in c.execute(
        "SELECT run_date, deadline, cost, wow_status, stale, snippet, notes FROM run_opportunities WHERE url=? ORDER BY run_date",
        (o['url'],)):
        print(f"\n--- {r['run_date']} [{r['wow_status']}] deadline={r['deadline']}")
        if r['snippet']:
            print("   ", r['snippet'][:160])
        if r['notes']:
            print("   notes:", r['notes'][:160])


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    c = conn()
    cmd = sys.argv[1]
    try:
        if cmd == "stats":
            cmd_stats(c)
        elif cmd == "search":
            cat = None
            if "--category" in sys.argv:
                cat = sys.argv[sys.argv.index("--category") + 1]
            lim = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 20
            cmd_search(c, sys.argv[2], cat, lim)
        elif cmd == "category":
            lim = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 30
            cmd_category(c, sys.argv[2], lim)
        elif cmd == "deadlines":
            days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 60
            cmd_deadlines(c, days)
        elif cmd == "new":
            since = sys.argv[sys.argv.index("--since") + 1] if "--since" in sys.argv else "2026-09-01"
            cmd_new(c, since)
        elif cmd == "gone":
            lim = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 30
            cmd_gone(c, lim)
        elif cmd == "show":
            cmd_show(c, sys.argv[2])
        else:
            print(__doc__)
            return 2
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
