# ivy-2028

Weekly opportunity-research pipeline for a Class of 2028 student: competitions, scholarships, summer programs, and exams — each scored 0–100 for **spike fit** against the admissions strategy (world-building / computational narrative / interactive storytelling).

## How it works

1. **Research** (Monday 6:00 AM ET): 10 parallel research agents, one per category in `agents.md`, extract opportunities as JSON (name, url, deadline, eligibility, cost, …). Deadlines inside 30 days are re-verified against the official source; grade eligibility is stated explicitly.
2. **Coordinate** (`coordinator.py --agent ivy_2028 --date YYYY-MM-DD --validate-urls`): dedupes (within-run + cross-run against the archive), filters ineligible programs, verifies URLs (Keenable → Firecrawl → HTTP), scores spike fit, tracks week-over-week status in SQLite, and renders the HTML digest + CSV.
3. **Publish**: digest committed to `output/`, `index.html` refreshed, pushed to main → GitHub Pages.

## The archive ("treasure trove")

`data/ivy_2028.db` holds every opportunity ever found — 1,002 across 22 weekly runs since May 2026, with per-run snapshots, WoW status, and 1,462 "gone" records. Query it:

```
python3 scripts/query_db.py stats
python3 scripts/query_db.py search "game design"
python3 scripts/query_db.py category worldbuilding
python3 scripts/query_db.py deadlines --days 30
python3 scripts/query_db.py new --since 2026-09-01
python3 scripts/query_db.py show youngarts
```

## Secrets

URL verification needs API keys. Order of lookup: `KEENABLE_API_KEY` / `FIRECRAWL_API_KEY` env vars → `~/.env.secrets` → `.secrets` in this directory. Never commit keys.

## Categories

worldbuilding · science_math · scholarships · writing_humanities · summer_programs · general_competitions · ct_local · fencing · tech_ai · exams
