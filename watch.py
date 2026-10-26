#!/usr/bin/env python3
"""
H-1B visa slot watcher for US consulates in India.

Reads the public "Recent slot activity" table on VisaGrader (community-reported
slot sightings for all five Indian consulates), keeps the H-1B Interview and
Biometrics rows you care about, and pushes any row it hasn't seen before to
your phone through ntfy.

It never touches the visa scheduling portal or your account. When a push
arrives, you log in and book yourself.

Settings (environment variables):
  NTFY_TOPIC    required. The ntfy topic your phone is subscribed to.
  NTFY_SERVER   optional. Defaults to https://ntfy.sh
  NTFY_TOKEN    optional. Access token if you use a protected topic.
  CONSULATES    optional. Comma list, e.g. "Chennai,Hyderabad". Default: all.
  TYPES         optional. Comma list of "Interview", "Biometrics". Default: both.
  LATEST_DATE   optional. Only alert for slots on/before this date (YYYY-MM-DD).
  STATE_FILE    optional. Defaults to state.json next to this script.
  DRY_RUN       optional. "1" prints notifications instead of sending.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SOURCE_URL = "https://visagrader.com/us-visa-time-slots-availability/india-ind"
CITY_PAGES = {
    "new delhi": "https://visagrader.com/us-visa-time-slots-availability/india-ind/new-delhi-P147/h1b-visa-H1B",
    "chennai": "https://visagrader.com/us-visa-time-slots-availability/india-ind/chennai-P48/h1b-visa-H1B",
    "hyderabad": "https://visagrader.com/us-visa-time-slots-availability/india-ind/hyderabad-P85/h1b-visa-H1B",
    "kolkata": "https://visagrader.com/us-visa-time-slots-availability/india-ind/kolkata-P100/h1b-visa-H1B",
    "mumbai": "https://visagrader.com/us-visa-time-slots-availability/india-ind/mumbai-P139/h1b-visa-H1B",
}
USER_AGENT = "personal-h1b-slot-alert/1.0 (one user, checks every 5 minutes)"
REQUIRED_COLUMNS = {"consulate", "type", "visa", "availability date"}
KEEP_SEEN_DAYS = 21          # forget old sightings so the state file stays small
ERROR_ALERT_EVERY_HOURS = 12  # don't spam if the source is down for a while
HEARTBEAT_EVERY_DAYS = 25     # keeps GitHub from pausing the schedule (60-day idle rule)
MAX_SEPARATE_PUSHES = 4       # more new rows than this get bundled into one push


class SourceError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def norm(text: str) -> str:
    """'H-1B' -> 'h1b', ' New  Delhi ' -> 'new delhi'."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


def norm_visa(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def env_list(name: str) -> set[str]:
    raw = os.environ.get(name, "")
    return {norm(x) for x in raw.split(",") if x.strip()}


def parse_us_date(text: str):
    """VisaGrader shows dates as M/D/YYYY."""
    try:
        return datetime.strptime(text.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- source

def fetch_html() -> str:
    try:
        resp = requests.get(SOURCE_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    except requests.RequestException as exc:
        raise SourceError(f"Could not reach VisaGrader: {exc}") from exc
    if resp.status_code != 200:
        raise SourceError(f"VisaGrader returned HTTP {resp.status_code}")
    return resp.text


def parse_rows(html: str) -> list[dict]:
    """Return the rows of the 'Recent slot activity' table as dicts."""
    soup = BeautifulSoup(html, "html.parser")

    table = None
    for heading in soup.find_all(["h1", "h2", "h3", "h4"]):
        if "recent slot activity" in norm(heading.get_text(" ")):
            table = heading.find_next("table")
            break
    if table is None:  # fall back to any table with the right columns
        for candidate in soup.find_all("table"):
            first = candidate.find("tr")
            cols = {norm(c.get_text(" ")) for c in first.find_all(["th", "td"])} if first else set()
            if REQUIRED_COLUMNS <= cols:
                table = candidate
                break
    if table is None:
        raise SourceError("Couldn't find the 'Recent slot activity' table; the page layout may have changed.")

    trs = table.find_all("tr")
    headers = [norm(c.get_text(" ")) for c in trs[0].find_all(["th", "td"])]
    if not REQUIRED_COLUMNS <= set(headers):
        raise SourceError(f"Slot table columns changed: {headers}")

    rows = []
    for tr in trs[1:]:
        cells = [re.sub(r"\s+", " ", c.get_text(" ")).strip() for c in tr.find_all(["td", "th"])]
        if len(cells) != len(headers):
            continue
        r = dict(zip(headers, cells))
        rows.append({
            "consulate": r.get("consulate", ""),
            "type": r.get("type", ""),
            "visa": r.get("visa", ""),
            "date": r.get("availability date", ""),
            "count": r.get("count", r.get("slots", "")),
            "reported": r.get("reported", ""),
        })
    return rows


def wanted(row: dict, consulates: set[str], types: set[str], latest) -> bool:
    if norm_visa(row["visa"]) != "h1b":
        return False
    if norm(row["type"]) not in types:
        return False
    if consulates and norm(row["consulate"]) not in consulates:
        return False
    if latest:
        d = parse_us_date(row["date"])
        if d and d > latest:
            return False
    return True


def row_key(row: dict) -> str:
    return "|".join(norm(row[k]) for k in ("consulate", "type", "visa", "date", "count", "reported"))


# ---------------------------------------------------------------- notify

def push(title: str, message: str, *, priority: int = 5, tags=None, click: str | None = None) -> None:
    if os.environ.get("DRY_RUN") == "1":
        print(f"[DRY RUN] {title}\n{message}\n")
        return
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        sys.exit("NTFY_TOPIC is not set. Add it as a repository secret (see README).")
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    payload = {"topic": topic, "title": title, "message": message,
               "priority": priority, "tags": tags or []}
    if click:
        payload["click"] = click
    headers = {}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
    resp = requests.post(server, json=payload, headers=headers, timeout=20)
    resp.raise_for_status()


def describe(row: dict) -> str:
    count = row["count"] or "?"
    return f"{row['consulate']} {row['type']}: {count} slot(s) on {row['date']} (reported {row['reported']} UTC)"


def notify_new(rows: list[dict]) -> None:
    if len(rows) <= MAX_SEPARATE_PUSHES:
        for r in rows:
            push(
                f"H-1B {r['type']} slot: {r['consulate']}",
                f"{r['count'] or '?'} slot(s) on {r['date']}\n"
                f"Reported {r['reported']} UTC\nLog in to the visa portal and book now.",
                tags=["rotating_light"],
                click=CITY_PAGES.get(norm(r["consulate"]), SOURCE_URL),
            )
    else:
        push(
            f"{len(rows)} new H-1B slot reports",
            "\n".join(describe(r) for r in rows) + "\n\nLog in to the visa portal and book now.",
            tags=["rotating_light"],
            click=SOURCE_URL,
        )


# ---------------------------------------------------------------- state

def load_state(path: Path) -> dict:
    if path.exists() and path.read_text().strip():
        return json.loads(path.read_text())
    return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------- main

def run(html: str | None = None) -> int:
    state_path = Path(os.environ.get("STATE_FILE", Path(__file__).with_name("state.json")))
    state = load_state(state_path)
    now = now_utc()

    consulates = env_list("CONSULATES")
    types = env_list("TYPES") or {"interview", "biometrics"}
    latest = None
    if os.environ.get("LATEST_DATE", "").strip():
        latest = datetime.strptime(os.environ["LATEST_DATE"].strip(), "%Y-%m-%d").date()

    try:
        rows = parse_rows(html if html is not None else fetch_html())
    except SourceError as exc:
        print(f"ERROR: {exc}")
        last = state.get("last_error_alert")
        if not last or now - datetime.fromisoformat(last) > timedelta(hours=ERROR_ALERT_EVERY_HOURS):
            push("Visa slot watcher can't read its source",
                 f"{exc}\nYou are NOT covered until this is fixed. Check slots manually for now.",
                 priority=4, tags=["warning"])
            state["last_error_alert"] = now.isoformat(timespec="seconds")
            save_state(state_path, state)
        return 0  # the push is the alert; a failed job would also email you every 5 min

    state.pop("last_error_alert", None)
    seen: dict = state.get("seen", {})
    first_run = "seen" not in state

    matches = [r for r in rows if wanted(r, consulates, types, latest)]
    new = [r for r in matches if row_key(r) not in seen]
    for r in new:
        seen[row_key(r)] = now.isoformat(timespec="seconds")

    if first_run:
        push("Visa slot watcher is live",
             f"Watching H-1B {', '.join(sorted(t.title() for t in types))} at "
             f"{', '.join(sorted(c.title() for c in consulates)) if consulates else 'all Indian consulates'}.\n"
             f"{len(matches)} matching report(s) already on the board; you'll be pinged for new ones.",
             priority=3, tags=["white_check_mark"])
    elif new:
        notify_new(new)

    cutoff = now - timedelta(days=KEEP_SEEN_DAYS)
    state["seen"] = {k: v for k, v in seen.items() if datetime.fromisoformat(v) >= cutoff}

    hb = state.get("heartbeat")
    if not hb or now - datetime.fromisoformat(hb) > timedelta(days=HEARTBEAT_EVERY_DAYS):
        state["heartbeat"] = now.isoformat(timespec="seconds")

    save_state(state_path, state)
    print(f"OK: {len(rows)} rows on page, {len(matches)} match, {len(new)} new"
          f"{' (first run, seeded)' if first_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
