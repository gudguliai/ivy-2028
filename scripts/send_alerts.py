#!/usr/bin/env python3
"""Ivy-2028 deadline alerts → astrodastic@gmail.com.

Reads the latest ivy-2028 pipeline CSV, finds urgent (<=7d) and upcoming (<=30d)
deadlines and emails a digest with the app URL.

This is the ivy-2028 successor to the ivy-2028-v2 send_alerts.py. The LIVE copy
runs on this VM (the Gmail token that used to live on Zeus died Oct 3, 2026 —
Google revoked the refresh token, invalid_grant). Sending now goes through the
VM's Gmail connector (gudguliai@gmail.com, send scope granted) instead of a
token file. A copy is kept at ~/.hermes/scripts/ivy-2028-deadline-alerts.py on
Zeus for reference only; nothing on Zeus runs it anymore.

Environment (all optional unless noted):
  IVY_ALERT_SEND_VIA      "token" (default, needs ~/.hermes/google_token.json)
                          or "connector" (uses hatch_gws_cli on this VM)
  IVY_ALERT_GMAIL_ACCOUNT Gmail connector account id — REQUIRED for connector
                          mode (the gudguliai@gmail.com account)
  IVY_ALERT_STATE         state file path (default
                          ~/.hermes/scripts/ivy-2028-alerts-state.json)
  IVY_ALERT_CSV_GLOB      results-CSV glob (default
                          ~/projects/ivy-2028/output/*ivy_2028-results.csv)

DEDUPE: the pipeline only refreshes the CSV weekly, so an unguarded daily job
re-emailed the same list six mornings out of seven. State lives in the
IVY_ALERT_STATE file::

    {"items": {"<name>|<YYYY-MM-DD deadline>": "urgent|upcoming"},
     "last_email": "<iso>|null", "last_subject": "<str>|null",
     "last_run": "<iso>|null"}

An email goes out only when the alert set genuinely changed: an item not seen
before, or an item that crossed from "upcoming" into "urgent". Items that simply
age out of the 30-day window do not trigger mail.

FAILURES: any error (missing/empty CSV, Gmail 401, network) posts a short
Telegram notice and re-raises, so the cron run is recorded as failed instead of
silently doing nothing.

Usage:
  python3.13 send_alerts.py --dry-run            # show the decision, send nothing
  python3.13 send_alerts.py                      # send only if something changed
  python3.13 send_alerts.py --force              # bypass dedupe, send now
  python3.13 send_alerts.py --seed               # record current CSV as already
                                                 # alerted, without sending
  python3.13 send_alerts.py --state /tmp/s.json --to me@example.com   # test run
"""
import argparse
import base64
import csv
import fcntl
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

APP_URL = "https://gudguliai.github.io/ivy-2028/"
TO_EMAIL = "astrodastic@gmail.com"
FROM_EMAIL = "gudguliai@gmail.com"
TOKEN_PATH = os.path.expanduser("~/.hermes/google_token.json")
STATE_PATH = os.path.expanduser(os.environ.get(
    "IVY_ALERT_STATE", "~/.hermes/scripts/ivy-2028-alerts-state.json"))
CSV_GLOB = os.path.expanduser(os.environ.get(
    "IVY_ALERT_CSV_GLOB", "~/projects/ivy-2028/output/*ivy_2028-results.csv"))
SEND_VIA = os.environ.get("IVY_ALERT_SEND_VIA", "token")  # token | connector
GMAIL_ACCOUNT = os.environ.get("IVY_ALERT_GMAIL_ACCOUNT", "")
JOB_NAME = "ivy-2028-deadline-alerts"
NOTIFY_TARGET = "telegram"
STALE_HOURS = 40          # notify if the previous run was this long ago
URGENT_DAYS = 7
UPCOMING_DAYS = 30


# ---------------------------------------------------------------- pipeline data

def latest_csv() -> str:
    files = sorted(glob.glob(CSV_GLOB))
    return files[-1] if files else None


def parse_deadline(raw: str):
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def build_items(rows, max_days):
    today = date.today()
    out = []
    for r in rows:
        dl = parse_deadline(r.get("deadline"))
        if not dl:
            continue
        days = (dl - today).days
        if 0 <= days <= max_days:
            out.append((days, r, dl))
    return sorted(out, key=lambda x: x[0])


def item_key(r, dl) -> str:
    return f"{(r.get('name') or '?').strip()}|{dl.isoformat()}"


def item_html(days, r) -> str:
    dl = r.get("deadline", "")
    cost = r.get("cost", "") or ""
    aid = r.get("aid", "") or ""
    extra = " · ".join(x for x in [cost, aid] if x and x != "N/A")
    notes = (r.get("notes") or "")[:120]
    note_html = f" · {notes}" if notes else ""
    return (f'<li><b>{r.get("name", "?")}</b> — {dl}'
            f' ({days}d){f" · {extra}" if extra else ""}'
            f' — <a href="{r.get("url", APP_URL)}">link</a>{note_html}</li>')


# ------------------------------------------------------------------ dedupe state

def load_state(path: str) -> dict:
    empty = {"items": {}, "last_email": None, "last_subject": None,
             "last_run": None}
    try:
        with open(path) as fh:
            state = json.load(fh)
        if not isinstance(state, dict) or not isinstance(
                state.get("items", {}), dict):
            raise ValueError("malformed state")
        empty.update(state)
        return empty
    except FileNotFoundError:
        return empty
    except Exception as exc:                      # corrupt state: never crash
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            pass
        notify(f"⚠️ {JOB_NAME}: state file was unreadable ({exc}); "
               f"restarted it and re-verified from scratch.")
        return empty


def save_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def decide(current: dict, previous: dict):
    """Return (new_keys, crossed_keys) comparing the current alert set to state."""
    fresh = sorted(k for k in current if k not in previous)
    crossed = sorted(k for k, bucket in current.items()
                     if previous.get(k) not in (None, bucket))
    return fresh, crossed


# --------------------------------------------------------------------- delivery

def _refresh_once(cred):
    """Single refresh attempt; returns the new access token or raises."""
    data = urllib.parse.urlencode({
        "client_id": cred["client_id"],
        "client_secret": cred["client_secret"],
        "refresh_token": cred["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(cred["token_uri"], data=data)
    tok = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
    cred["token"] = tok["access_token"]
    with open(TOKEN_PATH, "w") as fh:
        json.dump(cred, fh, indent=2)
    return cred["token"]


def _refresh_if_needed(cred):
    """Refresh the Google access token via refresh_token; update the file.

    Retries once after a short pause: a single transient network blip at 8 AM
    used to kill the whole run (Sep 26, 2026). Returns None only if both
    attempts fail.
    """
    last = None
    for _attempt in range(2):
        try:
            return _refresh_once(cred)
        except Exception as exc:                  # noqa: BLE001 — retried
            last = exc
            time.sleep(5)
    print(f"token refresh failed twice: {type(last).__name__}: {last}",
          file=sys.stderr)
    return None


def send_via_connector(subject, html_body, to_email=TO_EMAIL):
    """Send through the VM's Gmail connector (hatch_gws_cli).

    Used since Oct 3, 2026, when Google revoked the Zeus token's refresh
    grant (invalid_grant) and the token backend died. Requires
    IVY_ALERT_GMAIL_ACCOUNT to be the gudguliai@gmail.com connector id.
    """
    if not GMAIL_ACCOUNT:
        raise RuntimeError("IVY_ALERT_GMAIL_ACCOUNT is not set — connector "
                           "send needs the Gmail account id")
    exe = shutil.which("hatch_gws_cli") or "/opt/hatch/bin/hatch_gws_cli"
    cmd = [exe, "gmail", "+send", "--account", GMAIL_ACCOUNT,
           "--to", to_email, "--subject", subject,
           "--body", html_body, "--html"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(
            f"connector send failed: {proc.stderr.strip()[:300]}")
    try:
        return json.loads(proc.stdout).get("id", "connector-sent")
    except Exception:                             # noqa: BLE001 — id optional
        return "connector-sent"


def send_email(subject, html_body, to_email=TO_EMAIL):
    if SEND_VIA == "connector":
        return send_via_connector(subject, html_body, to_email)
    cred = json.load(open(TOKEN_PATH))
    access = cred.get("token")

    def _do(access):
        msg = MIMEMultipart("alternative")
        msg["To"] = to_email
        msg["From"] = FROM_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html"))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        req = urllib.request.Request(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            data=json.dumps({"raw": raw}).encode(),
            headers={"Authorization": f"Bearer {access}",
                     "Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=60).read().decode())

    try:
        return _do(access).get("id")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            new_access = _refresh_if_needed(cred)
            if new_access:
                return _do(new_access).get("id")
        raise


def notify(text: str) -> None:
    """Best-effort Telegram ping via the Hermes CLI. Never raises."""
    exe = shutil.which("hermes")
    if not exe:
        # No Hermes CLI on this machine (e.g. the VM) — log and move on.
        print(f"notify skipped (no hermes CLI): {text}", file=sys.stderr)
        return
    try:
        subprocess.run([exe, "send", "--to", NOTIFY_TARGET, text],
                       capture_output=True, timeout=90, check=False)
    except Exception as exc:                      # never mask the real error
        print(f"notify failed: {exc}", file=sys.stderr)


# ------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print the decision; send nothing, write no state")
    ap.add_argument("--force", action="store_true",
                    help="ignore dedupe state and send now")
    ap.add_argument("--seed", action="store_true",
                    help="record the current CSV as already alerted, no email")
    ap.add_argument("--send-via", choices=["token", "connector"],
                    default=None,
                    help="override IVY_ALERT_SEND_VIA for this run")
    ap.add_argument("--state", default=STATE_PATH)
    ap.add_argument("--to", default=TO_EMAIL)
    args = ap.parse_args()

    global SEND_VIA
    if args.send_via:
        SEND_VIA = args.send_via

    lock = open(args.state + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("SKIP — another run holds the lock")
        return

    csv_path = latest_csv()
    if not csv_path:
        raise RuntimeError(f"no results CSV matching {CSV_GLOB}")
    with open(csv_path) as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise RuntimeError(f"empty results CSV: {os.path.basename(csv_path)}")

    urgent = build_items(rows, URGENT_DAYS)
    upcoming = [x for x in build_items(rows, UPCOMING_DAYS) if x[0] > URGENT_DAYS]
    current = {item_key(r, dl): "urgent" for _, r, dl in urgent}
    current.update({item_key(r, dl): "upcoming" for _, r, dl in upcoming})

    state = load_state(args.state)
    now = datetime.now()
    fresh, crossed = decide(current, state.get("items", {}))

    subject = f"Ivy Alert: {len(urgent)} urgent · {len(upcoming)} upcoming (30d)"
    u_html = "".join(item_html(d, r) for d, r, _ in urgent) or "<li>None</li>"
    n_html = "".join(item_html(d, r) for d, r, _ in upcoming) or "<li>None</li>"
    html = f"""<html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#0f172a">
<h2 style="margin-bottom:0">Ivy — Opportunity Alerts</h2>
<p style="color:#64748b">Data from {os.path.basename(csv_path)} · generated {date.today()}</p>
<h3>🔥 Urgent (≤ 7 days)</h3><ul>{u_html}</ul>
<h3>📅 Upcoming (≤ 30 days)</h3><ul>{n_html}</ul>
<hr><p style="color:#64748b">Full digest: <a href="{APP_URL}">{APP_URL}</a></p>
</body></html>"""

    # Was the previous run missed (machine asleep, job paused, hard crash)?
    missed = None
    try:
        gap = now - datetime.fromisoformat(state["last_run"])
        if gap > timedelta(hours=STALE_HOURS):
            missed = gap
    except (TypeError, ValueError):
        pass

    if args.dry_run:
        reason = (f"new={len(fresh)} crossed_to_urgent={len(crossed)} "
                  f"force={args.force}")
        print(f"DRY RUN — would {'SEND' if (fresh or crossed or args.force) else 'SKIP'}"
              f": {subject} | {reason}")
        if fresh:
            print("  new:", ", ".join(fresh[:5]))
        if crossed:
            print("  crossed:", ", ".join(crossed[:5]))
        return

    if args.seed:
        new_state = dict(state)
        new_state.update(items=current, last_run=now.isoformat(timespec="seconds"),
                         seeded_from=os.path.basename(csv_path))
        save_state(args.state, new_state)
        print(f"SEEDED {len(current)} items from {os.path.basename(csv_path)} "
              f"(no email sent)")
        return

    do_send = bool(fresh or crossed) or args.force
    if not (urgent or upcoming):
        do_send = args.force
        subject = f"Ivy Alert: 0 urgent · 0 upcoming (30d)"

    new_state = dict(state)
    new_state.update(items=current, last_run=now.isoformat(timespec="seconds"))

    if not do_send:
        save_state(args.state, new_state)
        print(f"NO_CHANGE ({len(urgent)} urgent, {len(upcoming)} upcoming) — "
              f"already alerted, no email sent")
        return

    msg_id = send_email(subject, html, args.to)
    new_state.update(last_email=now.isoformat(timespec="seconds"),
                     last_subject=subject)
    save_state(args.state, new_state)
    print(f"SENT {msg_id} | {subject}")

    if missed:
        hours = int(missed.total_seconds() // 3600)
        notify(f"ℹ️ {JOB_NAME}: previous run was {hours}h ago — this alert "
               f"covers the gap. ({subject})")


def run():
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        notify(f"⚠️ {JOB_NAME} failed: {type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    run()
