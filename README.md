# founder-radar

Daily **multi-source** scan for **high-caliber founder / VC / hiring events**
across SF & the Bay, over the next ~2 weeks. A free AI (Google Gemini) reads
each new event and keeps only the relevant ones, then pushes them to your
**phone via ntfy** (and, optionally, an email digest). Only *new* events ever
notify — no repeats, de-duplicated across sources.

**Sources (via a few generic adapters):**
- Cerebral Valley (headless render)
- Luma — discovery pages (`/sf`, `/ai-sf`) + calendars by ID (accelerators/communities)
- Any schema.org JSON-LD site — Hidden Events, Startup Valley (add more in `JSONLD_SITES`)
- Any iCal feed — Meetup groups, etc. (add more in `ICAL_FEEDS`)
- Devpost hackathons

Add a source by dropping a URL/ID into the relevant list at the top of `radar.py`.

Runs on **GitHub Actions** (free), so it works even when your computer is off.
Fork it and anyone can run their own.

## How it works
1. Headless browser renders the events page and extracts each event.
2. New events (vs `seen.json`) are sent to Gemini, which filters for
   founders / VCs / hiring of real caliber (edit the filter in `radar.py`).
3. Survivors are pushed via ntfy + optional email.
4. `seen.json` is committed back so nothing repeats.

## Setup (~10 min, all free)

### 1. Get this into your own GitHub repo
Create a new repo on github.com, then from this folder:
```bash
git init && git add . && git commit -m "founder-radar"
git branch -M main
git remote add origin https://github.com/<you>/founder-radar.git
git push -u origin main
```

### 2. Phone push (ntfy) — free, no account
- Install the **ntfy** app (iOS/Android).
- Pick a hard-to-guess topic name, e.g. `founder-radar-ab-7f3q9`.
- In the app: **Subscribe to topic** → enter that exact name.

### 3. Free Gemini key
- Go to https://aistudio.google.com/apikey → **Create API key** (no credit card).

### 4. Add repo secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**:
- `GEMINI_API_KEY` = your AI Studio key
- `NTFY_TOPIC` = your topic name from step 2
- *(optional email digest)* `SMTP_HOST` `SMTP_USER` `SMTP_PASS` `EMAIL_TO`
  (for Gmail: host `smtp.gmail.com`, user = your address, pass = a Gmail
  [App Password](https://myaccount.google.com/apppasswords), EMAIL_TO = comma-
  separated recipients — this is how you share it with others.)
- *(optional)* set a repo **Variable** `GEMINI_MODEL` to override the model.

### 5. First run
Repo → **Actions → founder-radar → Run workflow**.
The first run **seeds a baseline silently** (so you don't get blasted with every
current event). From the next run on, you'll get pushes only for *new* events.
After that it runs automatically every morning (~8am Pacific).

## Sharing with others
Two ways: (a) they **subscribe to the same ntfy topic** and get the same
alerts, or (b) add their emails to `EMAIL_TO`. To let someone run their own
copy, they just fork and repeat setup with their own key + topic.

## Tuning
- **What counts as relevant:** edit `FILTER_INSTRUCTIONS` in `radar.py`.
- **No-AI mode:** leave `GEMINI_API_KEY` unset — it falls back to keyword rules
  (`KW_KEEP` / `KW_DROP` in `radar.py`).
- **Schedule:** edit the `cron` line in `.github/workflows/radar.yml`.
