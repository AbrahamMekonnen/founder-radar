#!/usr/bin/env python3
"""
founder-radar — daily scan of Cerebral Valley events for high-caliber
founder / VC / hiring events, filtered by free AI (Gemini), pushed to your
phone via ntfy (and optional email digest). Only *new* events notify.

Runs headless in GitHub Actions. State lives in seen.json (committed back).
"""
import os
import re
import sys
import json
import time
import hashlib
import smtplib
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path

# ---------------- config (tune these) ----------------------------------------
SOURCE_URL   = "https://cerebralvalley.ai/events"
SEEN_FILE    = Path(__file__).parent / "seen.json"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
MAX_BLURB    = 600

# What we care about. Edit this to retune what counts as relevant.
FILTER_INSTRUCTIONS = """
You are filtering AI/tech events for someone who wants TWO things:
  (A) meet FUNDED FOUNDERS and VCs to learn from / exchange ideas, and
  (B) find STARTUP JOB opportunities (they are an AI engineer).

KEEP an event if it is clearly one of:
  - founder <> VC dinners/mixers, pitch nights, demo days, founder showcases
  - events hosted by VCs or where funded founders/investors clearly gather
  - hiring / recruiting / talent events, or company-hosted nights (host is hiring)
  - major-caliber summits/conferences relevant to founders/VCs/hiring
DROP: pure hackathons, coding workshops, trainings, product/webinar sessions,
  remote/online-only events, and anything not about founders/VCs/hiring.

For each event return: keep (true/false), category (one of:
"founder","vc","hiring","conference","other"), caliber ("major" or "minor"),
and reason (max 12 words).
""".strip()

BADGES = {"LIVE", "Today", "Tomorrow", "This Week", "Next Week", "Featured",
          "Previous slide", "Next slide", "see more", "see less"}
DATE_RE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+[A-Za-z]+\s+\d{1,2}\s+·")


# ---------------- scrape (headless browser) ----------------------------------
def fetch_events():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent="Mozilla/5.0")
        page.goto(SOURCE_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(4000)
        # scroll to trigger any lazy-loaded events
        for _ in range(8):
            page.mouse.wheel(0, 5000)
            page.wait_for_timeout(600)
        try:
            text = page.inner_text("main")
        except Exception:
            text = page.inner_text("body")
        browser.close()
    return parse_events(text)  # zero-width chars stripped inside parse_events #​", ""))


def parse_events(text):
    # (no zero-width cleaning needed; parser is UTF-8 safe)  ###"​", "﻿"))  ###​﻿]", "", text)  # zero-width/BOM strip  ###​﻿]", "", text)  # strip zero-width / BOM chars
    lines = [l.strip() for l in text.split("\n")]
    lines = [l for l in lines if l]  # drop blank lines
    events, n = [], len(lines)
    for i, line in enumerate(lines):
        if not DATE_RE.match(line):
            continue
        # title = nearest preceding real line (skip month tokens / day numbers / badges)
        title = None
        for j in range(i - 1, max(i - 4, -1), -1):
            c = lines[j]
            if c in BADGES or len(c) <= 2 or re.fullmatch(r"\d{1,2}", c) \
               or re.fullmatch(r"[A-Za-z]{3}", c):
                continue
            title = c
            break
        if not title:
            continue
        # location = next non-badge line after the date
        location, k = "", i + 1
        while k < n and (lines[k] in BADGES):
            k += 1
        if k < n and not DATE_RE.match(lines[k]):
            location = lines[k]
            k += 1
        # blurb = following lines until next date or a "see more/less" marker
        blurb_parts = []
        while k < n and not DATE_RE.match(lines[k]):
            seg = lines[k]
            if seg in BADGES or re.fullmatch(r"[A-Za-z]{3}", seg) or re.fullmatch(r"\d{1,2}", seg):
                break
            blurb_parts.append(seg)
            if len(" ".join(blurb_parts)) > MAX_BLURB:
                break
            k += 1
        blurb = re.sub(r"\s*see (more|less)\s*$", "", " ".join(blurb_parts)).strip()
        eid = hashlib.md5(f"{title}|{line}".encode("utf-8")).hexdigest()[:12]
        events.append({"id": eid, "title": title, "date": line,
                       "location": location, "blurb": blurb[:MAX_BLURB]})
    # de-dup within a single scrape
    uniq = {}
    for e in events:
        uniq[e["id"]] = e
    return list(uniq.values())


# ---------------- classify (free AI: Gemini) ---------------------------------
def classify(events):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        # no key -> keyword fallback so it still works with zero AI setup
        return keyword_fallback(events)
    numbered = "\n".join(
        f'{i}. TITLE: {e["title"]} | WHEN: {e["date"]} | WHERE: {e["location"]} | ABOUT: {e["blurb"]}'
        for i, e in enumerate(events)
    )
    prompt = (
        FILTER_INSTRUCTIONS
        + "\n\nReturn ONLY a JSON array, one object per event, in the same order, "
        + 'each: {"index":int,"keep":bool,"category":str,"caliber":str,"reason":str}.\n\nEVENTS:\n'
        + numbered
    )
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={key}")
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
    }).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read())
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        verdicts = json.loads(raw)
    except Exception as ex:
        print(f"[warn] Gemini failed ({ex}); using keyword fallback", file=sys.stderr)
        return keyword_fallback(events)
    by_index = {v.get("index", i): v for i, v in enumerate(verdicts)}
    out = []
    for i, e in enumerate(events):
        v = by_index.get(i, {})
        if v.get("keep"):
            out.append({**e, "category": v.get("category", "?"),
                        "caliber": v.get("caliber", "?"), "reason": v.get("reason", "")})
    return out


KW_KEEP = ["founder", "vc", "venture", "investor", "pitch", "demo day", "demo night",
           "hiring", "recruit", "talent", "career", "join our team", "dinner", "mixer",
           "angel", "seed", "raise", "operators", "summit"]
KW_DROP = ["hackathon", "workshop", "training", "webinar", "remote", "online", "bootcamp"]


def keyword_fallback(events):
    out = []
    for e in events:
        hay = f'{e["title"]} {e["blurb"]}'.lower()
        if any(k in hay for k in KW_DROP):
            continue
        if any(k in hay for k in KW_KEEP):
            out.append({**e, "category": "keyword-match", "caliber": "?",
                        "reason": "matched keyword filter"})
    return out


# ---------------- notify ------------------------------------------------------
def notify_ntfy(kept):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("[info] NTFY_TOPIC not set; skipping phone push", file=sys.stderr)
        return
    lines = [f'• {e["title"]} — {e["date"]} [{e.get("caliber","?")}/{e.get("category","?")}]'
             for e in kept]
    body = "\n".join(lines)[:3800]
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
        headers={"Title": f"{len(kept)} new founder/VC/hiring events",
                 "Priority": "default", "Tags": "rocket",
                 "Click": SOURCE_URL},
    )
    try:
        urllib.request.urlopen(req, timeout=30)
        print(f"[ok] pushed {len(kept)} events to ntfy topic '{topic}'")
    except Exception as ex:
        print(f"[warn] ntfy push failed: {ex}", file=sys.stderr)


def notify_email(kept):
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        return  # email optional
    user = os.environ["SMTP_USER"]
    pw   = os.environ["SMTP_PASS"]
    to   = [a.strip() for a in os.environ.get("EMAIL_TO", user).split(",") if a.strip()]
    rows = "\n\n".join(
        f'{e["title"]}\n{e["date"]} · {e["location"]}\n'
        f'[{e.get("caliber","?")} / {e.get("category","?")}] {e.get("reason","")}\n{e["blurb"]}'
        for e in kept
    )
    msg = MIMEText(f"{len(kept)} new founder/VC/hiring events:\n\n{rows}\n\n{SOURCE_URL}")
    msg["Subject"] = f"[founder-radar] {len(kept)} new events"
    msg["From"] = user
    msg["To"] = ", ".join(to)
    try:
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587"))) as s:
            s.starttls(); s.login(user, pw); s.sendmail(user, to, msg.as_string())
        print(f"[ok] emailed {len(kept)} events to {to}")
    except Exception as ex:
        print(f"[warn] email failed: {ex}", file=sys.stderr)


# ---------------- main --------------------------------------------------------
def main():
    seen = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()
    first_run = not seen

    events = fetch_events()
    print(f"[info] scraped {len(events)} events")
    if not events:
        print("[error] no events parsed — page structure may have changed", file=sys.stderr)
        sys.exit(1)

    new = [e for e in events if e["id"] not in seen]
    print(f"[info] {len(new)} new since last run")

    if first_run:
        # seed baseline silently so we don't blast every current event on day one
        SEEN_FILE.write_text(json.dumps([e["id"] for e in events], indent=0))
        print("[info] first run — seeded baseline, no notifications sent")
        return

    if new:
        kept = classify(new)
        print(f"[info] {len(kept)} passed the founder/VC/hiring filter")
        if kept:
            notify_ntfy(kept)
            notify_email(kept)

    # remember everything we've now seen
    seen.update(e["id"] for e in events)
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=0))


if __name__ == "__main__":
    main()
