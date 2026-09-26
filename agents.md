# Research Agents — ivy-2028 (v3)

## Agent: ivy_2028
- Schedule: Monday 6:00 AM ET (cron: ivy-2028-weekly-pipeline, runs on Odie's VM)
- URL verification (our own tools only — no Keenable, no Firecrawl, per Aditya Sep 25, 2026): every researcher opens each opportunity URL with their own browser tools during research, confirms the page loads and matches the opportunity, and sets `url_verified: true`, `url_status: "verified"`, `url_source: "browser"` on the artifact. Coordinator runs a plain-HTTP HEAD/GET backstop for anything not pre-verified.
- Deadline verification: for any opportunity with a deadline inside 30 days, re-check the date against the official source page during the run. Set `deadline_confidence: "high"` only if you confirmed it on the official site this run; otherwise `"low"`.
- Post-step: none (verification is inside coordinator now)
- Profile: Indian origin, parents immigrated to US, US citizen, CT-based, **11th grade, Class of 2028**, high income family, 4.0+ GPA.
  Interests (ranked): **world-building / computational narrative / interactive storytelling** (the admissions spike — Unreal Engine, simulations, published fantasy novelist), writing & humanities, biology, psychology, fencing. Willing to travel/overnight.
  Deprioritized: law/civics (old interest, keep only standout national items), pure-cyber competitions.
- Exclusions: low-income-only, need-based, Pell Grant, college-only, senior-only (Class of 2027), programs that have permanently closed.
- Ethnic tagging: yes (indian_american, asian_american, south_asian, kids_of_immigrants, minority, heritage)
- Coordinator: `cd ~/workspace/ivy-2028 && python3 coordinator.py --agent ivy_2028 --date $(date +%Y-%m-%d) --validate-urls`
- History: full archive lives in `data/ivy_2028.db` (SQLite, 1,002 opportunities / 22 runs since May 2026). Query it before reporting "new" — see scripts/query_db.py.

### Categories

**1. worldbuilding** (the spike — highest priority)
Search terms: interactive fiction competitions, worldbuilding contests for teens, computational narrative, procedural generation student showcase, Unreal Engine education challenge, teen game design competition, Twine / ChoiceScript contests, narrative game jam high school, MIT Media Lab high school programs, AI storytelling competition teens
Notes: This category feeds the admissions spike directly. Weight national/prestigious items highest. Anything that could become a portfolio artifact (a playable build, a published piece) outranks a cash prize.

**2. science_math**
Search terms: biology olympiad, USABO, AMC, Science Olympiad, ISEF, science fair, research competitions, STEM contests, CT Science Fair

**3. scholarships**
Search terms: national merit, merit scholarship, Indian American scholarship, CT scholarship, Asian/South Asian heritage scholarships, scholarships for children of immigrants
Notes: Filter out low-income-only, need-based, Pell Grant. Keep merit, academic, leadership, Indian American heritage, Asian heritage. Always record grade eligibility — flag senior-only explicitly.

**4. writing_humanities**
Search terms: writing competitions, essay contests, Scholastic Art Writing, National History Day, journalism, poetry
Notes: Second spike pillar (published novelist, CT Writer's Competition gold). Prioritize national-level.

**5. summer_programs**
Search terms: computational narrative / game design summer 2027, biology summer programs 2027, research internships, pre-college, RSI, SSP, Yale summer, NIH
Notes: Summer 2027 is the target window (between junior and senior year). Note application open dates, not just deadlines.

**6. general_competitions**
Search terms: Regeneron STS, National Merit, Coca Cola, Davidson Fellows, presidential scholars
Notes: Filter out low-income-only. **Verify grade eligibility — STS and several others are seniors-only; mark them ineligible, do not list as actionable.**

**7. ct_local**
Search terms: CT scholarships, Yale CT programs, UConn research, New England competitions, CT science fair

**8. fencing**
Search terms: USA Fencing tournaments, CT fencing competitions, New England circuit, Junior Olympics, NAC series, fencing camps, CT high school fencing

**9. tech_ai**
Search terms: USACO, Congressional App Challenge, TechRise, hackathons for teens, FIRST/FTC robotics, AI competitions for high school
Notes: Straight-tech items live here. Creative/narrative tech belongs in worldbuilding.

**10. exams**
Search terms: PSAT, SAT, ACT, AP, CLT, PSAT/NMSQT dates and registration
Notes: Record the school's actual test date where findable, not just the national window.

### Extraction Schema
Each subagent must extract these fields per opportunity:
- name, category, url, url_verified (true only if you opened the URL with your own browser tools this run and it loaded + matched), url_status ("verified"/"dead"), url_source ("browser"), snippet, deadline (YYYY-MM-DD), deadline_confidence ("high" only if confirmed on the official site this run, else "low"), cost, aid, eligibility, eligibility_note (state grade eligibility explicitly, e.g. "grades 9-12", "seniors only"), notes, source
- ethnic_tags: comma-separated — set if the program targets specific ethnic/cultural groups. Values: indian_american, asian_american, south_asian, kids_of_immigrants, minority, heritage

Write JSON array to `/tmp/ivy-2028/<agent>/<date>/<category>.json`.

### Execution Flow

1. **Create artifacts directory**: `mkdir -p /tmp/ivy-2028/ivy_2028/$(date +%Y-%m-%d)`
2. **Spawn 10 subagents in parallel** — one per category above. Each agent:
   - Uses WebSearch + WebFetch to find real opportunities (reads actual page content)
   - **Verifies every URL with your own browser tools**: open it, confirm it loads and is the right program; set url_verified/url_status/url_source on the artifact. Never mark a URL verified you didn't open.
   - Filters out low-income-only / need-based programs per category notes
   - **Verifies grade eligibility** and states it in eligibility_note; senior-only items are marked ineligible, never listed as actionable
   - **Re-verifies deadlines inside 30 days** against the official source; sets deadline_confidence accordingly
   - Checks `scripts/query_db.py` history so genuinely new finds are flagged
   - Extracts all fields from the schema above
   - Writes JSON array to the artifact path for its category
3. **Run coordinator**: execute the Coordinator command from the agent header
4. **Commit + push**: digest HTML/CSV go to the `gudguliai/ivy-2028` repo (`output/`), index refreshed, push to main so GitHub Pages updates
5. **Report summary**: total opportunities, top-10 spike-fit items, urgent deadlines (with confidence), standout finds, notable changes vs last week

### Spike-fit scoring (coordinator.py)
Each opportunity gets a 0–100 spike-fit score against the admissions strategy (world-builder narrative). The digest leads with the top 10. Weights are documented in coordinator.py — transparent heuristic, not a black box.
