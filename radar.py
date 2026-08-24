#!/usr/bin/env python3
"""
founder-radar v2 — daily multi-source scan for high-caliber founder / VC /
hiring events across SF & the Bay. Pulls many sources through a few generic
adapters, keeps the next ~2 weeks, de-dupes across sources, filters with free
AI (Gemini), and pushes new hits to your phone via ntfy (+ optional email).

Runs headless in GitHub Actions. State lives in seen.json (committed back).
"""
import os
import re
import sys
import json
import time
import hashlib
import smtplib
import datetime as dt
import urllib.request
import urllib.parse
from email.mime.text import MIMEText
from pathlib import Path

# ---------------- config ------------------------------------------------------
SEEN_FILE     = Path(__file__).parent / "seen.json"
GEMINI_MODEL  = os.environ.get("GEMINI_MODEL") or "gemini-3.6-flash"
HORIZON_DAYS  = int(os.environ.get("HORIZON_DAYS", "16"))
DRY_RUN       = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
UA            = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                 "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36")
MAX_BLURB     = 400

# Sources (add/remove freely — each maps to a generic adapter below)
LUMA_DISCOVERY = ["https://luma.com/ai-sf", "https://luma.com/sf"]
LUMA_CALENDARS = ["cal-QzSyFbecenFng8a"]  # Berkeley SkyDeck; add more cal-* IDs
JSONLD_SITES   = ["https://hiddenevents.online/sf",
                  "https://startupvalley.club/cities/san-francisco"]
ICAL_FEEDS     = ["https://www.meetup.com/startups-and-tech-events-in-san-francisco/events/ical/"]
DEVPOST_QUERY  = "san francisco"
CV_URL         = "https://cerebralvalley.ai/events"

FILTER_INSTRUCTIONS = """
You are filtering AI/tech events for someone who wants TWO things:
  (A) meet FUNDED FOUNDERS and VCs to learn from / exchange ideas, and
  (B) find STARTUP JOB opportunities (they are an AI engineer).
KEEP an event only if it is clearly one of:
  - founder <> VC dinners/mixers, pitch nights, demo days, founder showcases
  - events hosted by VCs or where funded founders/investors clearly gather
  - hiring / recruiting / talent events, or company-hosted nights (host is hiring)
  - major-caliber summits/conferences relevant to founders/VCs/hiring
DROP: pure hackathons unless investor-facing, generic coding workshops, trainings,
  product webinars, wellness/social-only meetups, and anything not about
  founders/VCs/hiring.
For each event return: keep (true/false), category (one of
"founder","vc","hiring","conference","other"), caliber ("major"|"minor"),
reason (max 12 words).
""".strip()

MONTHS = {m: i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1)}
MONTHS.update({m: i for i, m in enumerate(
    ["January","February","March","April","May","June","July","August",
     "September","October","November","December"], 1)})


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


# ---------------- date parsing ------------------------------------------------
def parse_iso(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except Exception:
        return None


def parse_ical_dt(val):
    m = re.search(r"(\d{8})", val)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), "%Y%m%d").date()
    except Exception:
        return None


def parse_monthday(text):
    """Parse 'Aug 26' / 'August 26, 2026' style; infer year if absent."""
    m = re.search(r"([A-Z][a-z]{2,8})\s+(\d{1,2})(?:.*?(\d{4}))?", text)
    if not m:
        return None
    mon = MONTHS.get(m.group(1))
    if not mon:
        return None
    day = int(m.group(2))
    today = dt.date.today()
    year = int(m.group(3)) if m.group(3) else today.year
    try:
        d = dt.date(year, mon, day)
    except ValueError:
        return None
    if not m.group(3) and d < today - dt.timedelta(days=3):
        d = dt.date(year + 1, mon, day)
    return d


# ---------------- adapters (each returns list of normalized dicts) ------------
def _norm_event(source, title, start, location="", url="", blurb="", when=""):
    return {"source": source, "title": (title or "").strip(),
            "start": start, "when": when or (start.isoformat() if start else ""),
            "location": (location or "").strip(), "url": url or "",
            "blurb": (blurb or "").strip()[:MAX_BLURB]}


def fetch_luma_discovery(url):
    html = http_get(url)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    data = json.loads(m.group(1))
    found, seen_ids = [], set()

    def walk(o):
        if isinstance(o, dict):
            ev = o.get("event") if isinstance(o.get("event"), dict) else (
                o if ("name" in o and "start_at" in o) else None)
            if ev and ev.get("name") and ev.get("start_at"):
                aid = ev.get("api_id") or ev.get("name")
                if aid not in seen_ids:
                    seen_ids.add(aid)
                    slug = ev.get("url") or ""
                    found.append(_norm_event(
                        "luma", ev.get("name"), parse_iso(ev.get("start_at")),
                        ev.get("geo_address_info", {}).get("city_state", "") if isinstance(ev.get("geo_address_info"), dict) else "",
                        f"https://luma.com/{slug}" if slug and not slug.startswith("http") else slug))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return found


def fetch_luma_calendar(cal_id):
    url = (f"https://api.lu.ma/calendar/get-items?calendar_api_id={cal_id}"
           f"&period=future&pagination_limit=50")
    data = json.loads(http_get(url))
    out = []
    for entry in data.get("entries", []):
        ev = entry.get("event", {})
        if not ev.get("name"):
            continue
        slug = ev.get("url") or ""
        out.append(_norm_event(
            "luma-cal", ev.get("name"), parse_iso(ev.get("start_at")),
            ev.get("timezone", ""),
            f"https://luma.com/{slug}" if slug and not slug.startswith("http") else slug))
    return out


def fetch_jsonld(url):
    html = http_get(url)
    blocks = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)
    events, out = [], []

    def walk(o):
        if isinstance(o, dict):
            if o.get("@type") == "Event":
                events.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    for b in blocks:
        try:
            walk(json.loads(b))
        except Exception:
            pass
    host = urllib.parse.urlparse(url).netloc.replace("www.", "")
    for e in events:
        loc = e.get("location")
        loc = loc.get("name") if isinstance(loc, dict) else (loc or "")
        out.append(_norm_event(host, e.get("name"), parse_iso(e.get("startDate")),
                               loc, e.get("url", ""), e.get("description", "")))
    return out


def fetch_ical(url):
    ics = http_get(url)
    out = []
    for v in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", ics, re.S):
        s = re.search(r"\nSUMMARY[^:]*:(.*)", v)
        d = re.search(r"\nDTSTART[^:]*:(.*)", v)
        u = re.search(r"\nURL[^:]*:(.*)", v)
        loc = re.search(r"\nLOCATION[^:]*:(.*)", v)
        host = urllib.parse.urlparse(url).netloc.replace("www.", "")
        out.append(_norm_event(
            host, (s.group(1).strip() if s else ""),
            parse_ical_dt(d.group(1)) if d else None,
            (loc.group(1).strip() if loc else "").replace("\\,", ","),
            (u.group(1).strip() if u else "")))
    return out


def fetch_devpost(query):
    url = f"https://devpost.com/api/hackathons?search={urllib.parse.quote(query)}"
    data = json.loads(http_get(url))
    out = []
    for h in data.get("hackathons", []):
        out.append(_norm_event(
            "devpost", h.get("title"), parse_monthday(h.get("submission_period_dates", "")),
            h.get("displayed_location", {}).get("location", "") if isinstance(h.get("displayed_location"), dict) else "",
            h.get("url", ""),
            ", ".join(t.get("name", "") if isinstance(t, dict) else str(t)
                      for t in (h.get("themes") or [])),
            h.get("submission_period_dates", "")))
    return out


def fetch_cerebralvalley():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA)
        page.goto(CV_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(4000)
        for _ in range(8):
            page.mouse.wheel(0, 5000)
            page.wait_for_timeout(600)
        try:
            text = page.inner_text("main")
        except Exception:
            text = page.inner_text("body")
        browser.close()
    return parse_cv_text(text)


CV_BADGES = {"LIVE", "Today", "Tomorrow", "This Week", "Next Week", "Featured",
             "Previous slide", "Next slide", "see more", "see less"}
CV_DATE_RE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+[A-Za-z]+\s+\d{1,2}\s+·")


def parse_cv_text(text):
    lines = [l.strip() for l in text.split("\n")]
    lines = [l for l in lines if l]
    out, n = [], len(lines)
    for i, line in enumerate(lines):
        if not CV_DATE_RE.match(line):
            continue
        title = None
        for j in range(i - 1, max(i - 4, -1), -1):
            c = lines[j]
            if c in CV_BADGES or len(c) <= 2 or re.fullmatch(r"\d{1,2}", c) \
               or re.fullmatch(r"[A-Za-z]{3}", c):
                continue
            title = c
            break
        if not title:
            continue
        k = i + 1
        while k < n and lines[k] in CV_BADGES:
            k += 1
        location = ""
        if k < n and not CV_DATE_RE.match(lines[k]):
            location = lines[k]
            k += 1
        blurb = []
        while k < n and not CV_DATE_RE.match(lines[k]):
            seg = lines[k]
            if seg in CV_BADGES or re.fullmatch(r"[A-Za-z]{3}", seg) or re.fullmatch(r"\d{1,2}", seg):
                break
            blurb.append(seg)
            if len(" ".join(blurb)) > MAX_BLURB:
                break
            k += 1
        b = re.sub(r"\s*see (more|less)\s*$", "", " ".join(blurb)).strip()
        out.append(_norm_event("cerebralvalley", title, parse_monthday(line),
                               location, CV_URL, b, when=line))
    return out


# ---------------- orchestration ----------------------------------------------
def gather_all():
    tasks = [("cerebralvalley", fetch_cerebralvalley, ())]
    tasks += [(f"luma:{u}", fetch_luma_discovery, (u,)) for u in LUMA_DISCOVERY]
    tasks += [(f"luma-cal:{c}", fetch_luma_calendar, (c,)) for c in LUMA_CALENDARS]
    tasks += [(f"jsonld:{u}", fetch_jsonld, (u,)) for u in JSONLD_SITES]
    tasks += [(f"ical:{u}", fetch_ical, (u,)) for u in ICAL_FEEDS]
    tasks += [("devpost", fetch_devpost, (DEVPOST_QUERY,))]
    events = []
    for name, fn, args in tasks:
        try:
            got = fn(*args)
            print(f"[src] {name}: {len(got)}")
            events.extend(got)
        except Exception as ex:
            print(f"[warn] source {name} failed: {ex}", file=sys.stderr)
    return events


def event_id(e):
    norm = re.sub(r"[^a-z0-9]", "", (e["title"] or "").lower())
    key = f"{norm}|{e['start'].isoformat() if e['start'] else 'x'}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def within_horizon(e):
    if not e["start"]:
        return False  # need a date to place it in the next ~2 weeks
    today = dt.date.today()
    return today - dt.timedelta(days=1) <= e["start"] <= today + dt.timedelta(days=HORIZON_DAYS)


# ---------------- classify (free AI: Gemini) ---------------------------------
def classify(events):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return keyword_fallback(events)
    numbered = "\n".join(
        f'{i}. TITLE: {e["title"]} | WHEN: {e["when"]} | WHERE: {e["location"]} '
        f'| SRC: {e["source"]} | ABOUT: {e["blurb"]}'
        for i, e in enumerate(events))
    prompt = (FILTER_INSTRUCTIONS +
              "\n\nReturn ONLY a JSON array, one object per event, same order, each: "
              '{"index":int,"keep":bool,"category":str,"caliber":str,"reason":str}.'
              "\n\nEVENTS:\n" + numbered)
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={key}")
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"responseMimeType": "application/json",
                                            "temperature": 0}}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        verdicts = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
    except Exception as ex:
        print(f"[warn] Gemini failed ({ex}); keyword fallback", file=sys.stderr)
        return keyword_fallback(events)
    by_i = {v.get("index", i): v for i, v in enumerate(verdicts)}
    out = []
    for i, e in enumerate(events):
        v = by_i.get(i, {})
        if v.get("keep"):
            out.append({**e, "category": v.get("category", "?"),
                        "caliber": v.get("caliber", "?"), "reason": v.get("reason", "")})
    return out


KW_KEEP = ["founder", "vc", "venture", "investor", "pitch", "demo day", "demo night",
           "hiring", "recruit", "talent", "career", "dinner", "mixer", "angel",
           "seed", "raise", "operators", "summit"]
KW_DROP = ["workshop", "training", "webinar", "bootcamp", "cold plunge", "yoga"]


def keyword_fallback(events):
    out = []
    for e in events:
        hay = f'{e["title"]} {e["blurb"]}'.lower()
        if any(k in hay for k in KW_DROP):
            continue
        if any(k in hay for k in KW_KEEP):
            out.append({**e, "category": "keyword", "caliber": "?", "reason": "keyword match"})
    return out


# ---------------- notify ------------------------------------------------------
def notify_ntfy(kept):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("[info] NTFY_TOPIC unset; skipping push", file=sys.stderr)
        return
    def ascii_safe(s):
        for k, v in {"•": "-", "—": "-", "–": "-", "’": "'", "‘": "'",
                     "“": '"', "”": '"', "→": "->", "·": "-"}.items():
            s = s.replace(k, v)
        return s.encode("ascii", "ignore").decode()
    def line(e):
        head = (f'- {e["title"]} | {e["when"]} '
                f'[{e.get("caliber","?")}/{e.get("category","?")}]')
        url = e.get("url") or ""
        return ascii_safe(head + ("\\n  " + url if url else ""))
    lines = [line(e) for e in kept]
    # multi-line goes in the Message HEADER (\n-escaped) — a multi-line body
    # makes ntfy attach it as a file instead of showing a message.
    msg = "\\n".join(lines)[:3800]
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=b"", method="POST",
        headers={"Title": f"{len(kept)} new founder/VC/hiring events",
                 "Tags": "rocket", "Click": "https://cerebralvalley.ai/events",
                 "Message": msg})
    try:
        urllib.request.urlopen(req, timeout=30)
        print(f"[ok] pushed {len(kept)} to ntfy")
    except Exception as ex:
        print(f"[warn] ntfy failed: {ex}", file=sys.stderr)


def notify_email(kept):
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        return
    user, pw = os.environ["SMTP_USER"], os.environ["SMTP_PASS"]
    to = [a.strip() for a in os.environ.get("EMAIL_TO", user).split(",") if a.strip()]
    rows = "\n\n".join(
        f'{e["title"]}\n{e["when"]} · {e["location"]} · {e["source"]}\n'
        f'[{e.get("caliber","?")}/{e.get("category","?")}] {e.get("reason","")}\n'
        f'{e["url"]}\n{e["blurb"]}' for e in kept)
    msg = MIMEText(f"{len(kept)} new founder/VC/hiring events:\n\n{rows}")
    msg["Subject"] = f"[founder-radar] {len(kept)} new events"
    msg["From"], msg["To"] = user, ", ".join(to)
    try:
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587"))) as s:
            s.starttls(); s.login(user, pw); s.sendmail(user, to, msg.as_string())
        print(f"[ok] emailed {len(kept)} to {to}")
    except Exception as ex:
        print(f"[warn] email failed: {ex}", file=sys.stderr)


# ---------------- main --------------------------------------------------------
def main():
    seen = set(json.loads(SEEN_FILE.read_text(encoding="utf-8"))) if SEEN_FILE.exists() else set()
    first_run = not seen

    raw = gather_all()
    # horizon + dedup by cross-source id
    horizon, by_id = [], {}
    for e in raw:
        if within_horizon(e):
            by_id[event_id(e)] = e
    print(f"[info] {len(raw)} scraped -> {len(by_id)} unique within {HORIZON_DAYS}d")

    new = {eid: e for eid, e in by_id.items() if eid not in seen}
    print(f"[info] {len(new)} new since last run")

    if DRY_RUN:  # always classify + print, never write state
        sample = list(new.values()) or list(by_id.values())
        kept = classify(sample[:80])
        print(f"[dry-run] {len(kept)} would notify:")
        for e in kept:
            print(f"  KEEP [{e.get('caliber')}/{e.get('category')}] {e['title']} — {e['when']} ({e['source']})")
        return

    if first_run:
        SEEN_FILE.write_text(json.dumps(sorted(by_id.keys()), indent=0), encoding="utf-8")
        print("[info] first run — seeded baseline, no notifications")
        return

    if new:
        kept = classify(list(new.values()))
        print(f"[info] {len(kept)} passed the founder/VC/hiring filter")
        if kept:
            notify_ntfy(kept)
            notify_email(kept)

    seen.update(by_id.keys())
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=0), encoding="utf-8")


if __name__ == "__main__":
    main()
