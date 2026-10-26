# H-1B slot watcher (India)

Pings your phone when someone reports a new H-1B **Interview** or **Biometrics (OFC)**
slot at any US consulate in India. It checks VisaGrader's public "Recent slot activity"
board every ~5 minutes, for free, on GitHub.

It never logs into the visa portal or touches your account. When your phone buzzes,
you open the portal and book it yourself.

## Setup (about 15 minutes)

### 1. Phone: install ntfy
1. Install **ntfy** from the App Store or Play Store.
2. Tap **+**, and subscribe to a topic name that nobody could guess, e.g.
   `ak-h1b-7q2xk9`. Anyone who knows the name can read it, so treat it like a password.
3. On iPhone, allow notifications. For urgency, turn on "Time Sensitive" in iOS
   notification settings for ntfy.

### 2. GitHub: create the repo
1. Sign up at github.com.
2. Click **+ → New repository**. Name it `h1b-slot-watcher`, set it to **Public**
   (public repos get unlimited free Actions minutes; private ones would run out).
   Nothing sensitive goes in the repo: your topic name is stored as a secret.
3. Click **uploading an existing file** and drag in `watch.py`, `requirements.txt`,
   `state.json` and `README.md`. Commit.
4. The workflow file sits in a hidden folder, so add it by hand:
   **Add file → Create new file**, type the name `.github/workflows/watch.yml`
   (the slashes create the folders), paste in the contents of `watch.yml`, commit.

### 3. GitHub: add your topic
**Settings → Secrets and variables → Actions → New repository secret**
- Name: `NTFY_TOPIC`
- Value: the topic from step 1 (e.g. `ak-h1b-7q2xk9`)

### 4. Turn it on
1. Open the **Actions** tab. If asked, click **I understand my workflows, go ahead and enable them**.
2. Pick **H-1B slot watcher → Run workflow**.
3. Within a minute your phone should show **"Visa slot watcher is live"**. From then on it runs every 5 minutes by itself.

## Optional filters
**Settings → Secrets and variables → Actions → Variables tab → New repository variable**

| Name | Example | Effect |
| --- | --- | --- |
| `CONSULATES` | `Chennai,Hyderabad` | Only these consulates (default: all five) |
| `TYPES` | `Interview` | Only interview, or only `Biometrics` (default: both) |
| `LATEST_DATE` | `2027-03-31` | Ignore slots later than this date |

## What the pushes mean
- 🚨 **H-1B Interview slot: Chennai** — someone just reported slots. Tapping the
  push opens that consulate's history page. Go to the visa portal and try to book.
- ⚠️ **Watcher can't read its source** — VisaGrader is down or changed its page.
  You're not covered until it's fixed; check manually. Sent at most every 12 hours.

## Limits to know
- Alerts are only as fast as the community reports them, plus up to ~5–10 minutes
  of GitHub scheduling delay. Slots in India often vanish within minutes, so it's
  worth running a second source alongside this (the checkvisaslots extension or an
  H-1B Telegram group).
- GitHub pauses schedules on repos with no activity for 60 days. The watcher
  writes a small heartbeat to `state.json` every ~25 days to prevent that.
- VisaGrader's terms allow personal, non-commercial use with credit. Don't
  republish the data or share the feed.

## Test locally (optional)
```
pip install -r requirements.txt
DRY_RUN=1 python watch.py
```
