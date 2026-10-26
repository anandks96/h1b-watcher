#!/usr/bin/env python3
"""
H-1B visa slot watcher for US consulates in India.

Two community-powered sources, checked every run:

  1. VisaGrader "Recent slot activity" board: a list of slot sightings people
     report. Any report not seen before triggers a push.
  2. visaslots.info H-1B page: the current state of every Indian VAC
     (biometrics/OFC) and CONSULAR (interview) location. A push fires when a
     location goes from no slots to slots, or when an earlier date appears.

Pushes go to your phone through ntfy. The watcher never touches the visa
scheduling portal or your account. When a push arrives, you log in and book.

Settings (environment variables):
  NTFY_TOPIC    required. The ntfy topic your phone is subscribed to.
  NTFY_SERVER   optional. Defaults to https://ntfy.sh
  NTFY_TOKEN    optional. Access token if you use a protected topic.
  CONSULATES    optional. Comma list, e.g. "Chennai,Hyderabad". Default: all.
  TYPES         optional. Comma list of "Interview", "Biometrics". Default: both.
  LATEST_DATE   optional. Only alert for slots on/before this date (YYYY-MM-DD).
  VISASLOTS     optional. "0" turns the visaslots.info source off.
  STATE_FILE    optional. Defaults to state.json next to this script.
  DRY_RUN       optional. "1" prints notifications instead of sending.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
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
VISASLOTS_URL = "https://visaslots.info/class/h-1b/"
INDIA_CITIES = set(CITY_PAGES)

USER_AGENT = "personal-h1b-slot-alert/1.1 (one user, checks every 5 minutes)"
REQUIRED_COLUMNS = {"consulate", "type", "visa", "availability date"}
VISASLOTS_COLUMNS = {"location", "updated", "earliest", "slots"}
KEEP_SEEN_DAYS = 21           # forget old sightings so the state file stays small
ERROR_ALERT_EVERY_HOURS = {"visagrader": 12, "visaslots": 24}
HEARTBEAT_EVERY_DAYS = 25     # keeps GitHub from pausing the schedule (60-day idle rule)
MAX_SEPARATE_PUSHES = 4       # more alerts than this get bundled into one push
STALE_CHECK_MINUTES = 60      # visaslots data older than this gets a "may be gone" note
SOURCE_NAMES = {"visagrader": "VisaGrader", "visaslots": "visaslots.info"}


class SourceError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def norm(text: str) -> str:
    """' New  Delhi ' -> 'new delhi'."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


def norm_visa(text: str) -> str:
    """'H-1B' -> 'h1b'."""
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


def parse_any_date(text: str) -> date | None:
    """visaslots.info's 'Earliest' format isn't visible while it says N/A, so
    accept the common ones."""
    t = re.sub(r"\s+", " ", (text or "")).strip().replace(",", "")
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y",
                "%m/%d/%Y", "%d/%m/%Y", "%a %d %b %Y", "%a %b %d %Y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fetch(url: str, source: str) -> str:
    name = SOURCE_NAMES[source]
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    except requests.RequestException as exc:
        raise SourceError(f"Could not reach {name}: {exc}") from exc
    if resp.status_code != 200:
        raise SourceError(f"{name} returned HTTP {resp.status_code}")
    return resp.text


# ---------------------------------------------------------------- source 1: VisaGrader

def fetch_html() -> str:
    return fetch(SOURCE_URL, "visagrader")


def parse_rows(html: str) -> list[dict]:
    """Return the rows of VisaGrader's 'Recent slot activity' table as dicts."""
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
        raise SourceError("Couldn't find VisaGrader's 'Recent slot activity' table; the page layout may have changed.")

    trs = table.find_all("tr")
    headers = [norm(c.get_text(" ")) for c in trs[0].find_all(["th", "td"])]
    if not REQUIRED_COLUMNS <= set(headers):
        raise SourceError(f"VisaGrader slot table columns changed: {headers}")

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


def reported_note(row: dict) -> str:
    """VisaGrader's 'Reported' times are sometimes in the future (bad community
    data). Show the time only when it's believable."""
    try:
        ts = datetime.strptime(row["reported"], "%m/%d/%Y, %I:%M:%S %p").replace(tzinfo=timezone.utc)
    except ValueError:
        return "report time unknown"
    if ts > now_utc() + timedelta(hours=1):
        return "report time unknown (site shows a future date)"
    return f"reported {row['reported']} UTC"


def visagrader_alert(r: dict) -> dict:
    return {
        "title": f"H-1B {r['type']} slot: {r['consulate']}",
        "line": f"{r['consulate']} {r['type']}: {r['count'] or '?'} slot(s) on {r['date']} ({reported_note(r)})",
        "body": f"{r['count'] or '?'} slot(s) on {r['date']}\n{reported_note(r).capitalize()}\nSource: VisaGrader",
        "click": CITY_PAGES.get(norm(r["consulate"]), SOURCE_URL),
    }


# ---------------------------------------------------------------- source 2: visaslots.info

def parse_visaslots(html: str) -> list[dict]:
    """Return one dict per Indian location from visaslots.info's H-1B tables.
    'X VAC' rows are biometrics/OFC; 'X CONSULAR' rows are interviews."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        if not trs:
            continue
        headers = [norm(c.get_text(" ")) for c in trs[0].find_all(["th", "td"])]
        if not VISASLOTS_COLUMNS <= set(headers):
            continue
        for tr in trs[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) != len(headers):
                continue
            c = dict(zip(headers, cells))
            loc = re.sub(r"\s+", " ", c["location"].get_text(" ")).strip()
            m = re.match(r"(.+?)\s+(VAC|CONSULAR)$", loc, re.I)
            if not m or norm(m.group(1)) not in INDIA_CITIES:
                continue
            checked = None
            ts = c["updated"].get("data-ts")
            if ts:
                try:
                    checked = datetime.strptime(ts.replace(" UTC", ""), "%d %b %Y %I:%M:%S %p").replace(tzinfo=timezone.utc)
                except ValueError:
                    pass
            earliest = re.sub(r"\s+", " ", c["earliest"].get_text(" ")).strip()
            digits = re.sub(r"[^0-9]", "", c["slots"].get_text())
            link = c["location"].find("a")
            out.append({
                "city": m.group(1).title(),
                "type": "Biometrics" if m.group(2).upper() == "VAC" else "Interview",
                "earliest": earliest,
                "slots": int(digits) if digits else 0,
                "checked": checked,
                "url": link["href"] if link and link.get("href") else VISASLOTS_URL,
            })
    if not out:
        raise SourceError("Couldn't find visaslots.info's India location tables; the page layout may have changed.")
    return out


def has_slots(loc: dict) -> bool:
    return loc["slots"] > 0 or norm(loc["earliest"]) not in ("", "n/a", "na", "-", "--", "none")


def visaslots_alert(loc: dict, why: str) -> dict:
    when = ""
    if loc["checked"]:
        mins = int((now_utc() - loc["checked"]).total_seconds() // 60)
        when = f"checked {mins} min ago" if mins >= 1 else "checked just now"
        if mins > STALE_CHECK_MINUTES:
            when += ", may already be gone"
    count = f"{loc['slots']} slot(s)" if loc["slots"] else "slots"
    return {
        "title": f"H-1B {loc['type']} slot: {loc['city']}",
        "line": f"{loc['city']} {loc['type']}: {count}, earliest {loc['earliest']} ({why}{', ' + when if when else ''})",
        "body": f"{count}, earliest {loc['earliest']}\n{why.capitalize()}{' · ' + when if when else ''}\nSource: visaslots.info",
        "click": loc["url"],
    }


def check_visaslots(locs: list[dict], prev: dict, consulates, types, latest) -> tuple[dict, list[dict]]:
    """Compare against the last known state; alert on newly opened slots or an
    earlier date. Only availability is stored, so state changes only when it does."""
    state, alerts = {}, []
    for loc in locs:
        key = f"{norm(loc['city'])}|{norm(loc['type'])}"
        now_has = has_slots(loc)
        state[key] = {"has": now_has, "earliest": loc["earliest"] if now_has else ""}
        if not now_has:
            continue
        if norm(loc["type"]) not in types or (consulates and norm(loc["city"]) not in consulates):
            continue
        d = parse_any_date(loc["earliest"])
        if latest and d and d > latest:
            continue
        before = prev.get(key, {})
        if not before.get("has"):
            alerts.append(visaslots_alert(loc, "just opened"))
        else:
            pd = parse_any_date(before.get("earliest", ""))
            if d and pd and d < pd:
                alerts.append(visaslots_alert(loc, f"earlier date (was {before['earliest']})"))
    return state, alerts


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


def send_alerts(alerts: list[dict]) -> None:
    if len(alerts) <= MAX_SEPARATE_PUSHES:
        for a in alerts:
            push(a["title"], a["body"] + "\nLog in to the visa portal and book now.",
                 tags=["rotating_light"], click=a["click"])
    else:
        push(f"{len(alerts)} new H-1B slot alerts",
             "\n".join(a["line"] for a in alerts) + "\n\nLog in to the visa portal and book now.",
             tags=["rotating_light"], click=SOURCE_URL)


def source_error(state: dict, source: str, exc: Exception, other_ok: bool) -> None:
    """Warn about a broken source, at most every few hours."""
    print(f"ERROR ({source}): {exc}")
    errors = state.setdefault("errors", {})
    last = errors.get(source)
    now = now_utc()
    if last and now - datetime.fromisoformat(last) <= timedelta(hours=ERROR_ALERT_EVERY_HOURS[source]):
        return
    cover = ("The other source is still being watched." if other_ok
             else "You are NOT covered until this is fixed. Check slots manually for now.")
    push(f"Slot watcher can't read {SOURCE_NAMES[source]}", f"{exc}\n{cover}",
         priority=4, tags=["warning"])
    errors[source] = now.isoformat(timespec="seconds")


# ---------------------------------------------------------------- state

def load_state(path: Path) -> dict:
    if path.exists() and path.read_text().strip():
        return json.loads(path.read_text())
    return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------- main

def run(html: str | None = None, vs_html: str | None = None) -> int:
    state_path = Path(os.environ.get("STATE_FILE", Path(__file__).with_name("state.json")))
    state = load_state(state_path)
    now = now_utc()

    if "last_error_alert" in state:  # older state format
        state.setdefault("errors", {})["visagrader"] = state.pop("last_error_alert")

    consulates = env_list("CONSULATES")
    types = env_list("TYPES") or {"interview", "biometrics"}
    latest = None
    if os.environ.get("LATEST_DATE", "").strip():
        latest = datetime.strptime(os.environ["LATEST_DATE"].strip(), "%Y-%m-%d").date()
    use_visaslots = os.environ.get("VISASLOTS", "1").strip() != "0"

    alerts: list[dict] = []
    failed: dict[str, Exception] = {}

    # --- VisaGrader: report list
    rows = None
    try:
        rows = parse_rows(html if html is not None else fetch_html())
    except SourceError as exc:
        failed["visagrader"] = exc

    first_run = "seen" not in state
    if rows is not None:
        state.get("errors", {}).pop("visagrader", None)
        seen: dict = state.get("seen", {})
        matches = [r for r in rows if wanted(r, consulates, types, latest)]
        new = [r for r in matches if row_key(r) not in seen]
        for r in new:
            seen[row_key(r)] = now.isoformat(timespec="seconds")
        if first_run:
            push("Visa slot watcher is live",
                 f"Watching H-1B {', '.join(sorted(t.title() for t in types))} at "
                 f"{', '.join(sorted(c.title() for c in consulates)) if consulates else 'all Indian consulates'}.\n"
                 f"{len(matches)} matching report(s) already on VisaGrader's board; you'll be pinged for new ones.",
                 priority=3, tags=["white_check_mark"])
        else:
            alerts += [visagrader_alert(r) for r in new]
        # Forget old sightings, but never one still on the board: rows with bogus
        # future 'Reported' dates can linger for months and would come back as
        # false "new slot" alerts.
        cutoff = now - timedelta(days=KEEP_SEEN_DAYS)
        on_board = {row_key(r) for r in matches}
        state["seen"] = {k: v for k, v in seen.items()
                         if k in on_board or datetime.fromisoformat(v) >= cutoff}

    # --- visaslots.info: current state per location
    if use_visaslots:
        try:
            locs = parse_visaslots(vs_html if vs_html is not None else fetch(VISASLOTS_URL, "visaslots"))
            state.get("errors", {}).pop("visaslots", None)
            state["visaslots"], vs_alerts = check_visaslots(locs, state.get("visaslots", {}),
                                                             consulates, types, latest)
            alerts += vs_alerts
        except SourceError as exc:
            failed["visaslots"] = exc

    if alerts:
        send_alerts(alerts)
    active = ["visagrader"] + (["visaslots"] if use_visaslots else [])
    for source, exc in failed.items():
        source_error(state, source, exc, other_ok=any(s not in failed for s in active))
    if not state.get("errors"):
        state.pop("errors", None)

    hb = state.get("heartbeat")
    if not hb or now - datetime.fromisoformat(hb) > timedelta(days=HEARTBEAT_EVERY_DAYS):
        state["heartbeat"] = now.isoformat(timespec="seconds")

    save_state(state_path, state)
    print(f"OK: {len(alerts)} alert(s); failed sources: {', '.join(failed) or 'none'}"
          f"{' (first run, seeded)' if first_run and rows is not None else ''}")
    return 0  # problems are reported by push; a failed job would also email you every 5 min


if __name__ == "__main__":
    sys.exit(run())
