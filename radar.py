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
LEAD_DAYS     = int(os.environ.get("LEAD_DAYS") or "5")    # skip events sooner than this (need travel/planning lead time)
HORIZON_DAYS  = int(os.environ.get("HORIZON_DAYS") or "18")  # far edge of the window
DRY_RUN       = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
UA            = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                 "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36")
MAX_BLURB     = 400
BOARD_URL     = os.environ.get("BOARD_URL") or "https://abrahammekonnen.github.io/founder-radar/"

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
try:
    from zoneinfo import ZoneInfo
    _PT = ZoneInfo("America/Los_Angeles")
except Exception:
    _PT = None


def parse_iso(s):
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if d.tzinfo and _PT:          # convert UTC/offset times to Pacific so the
            d = d.astimezone(_PT)     # date matches local sources (fixes cross-source dedup)
        return d.date()
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
        return False  # need a date to place it in the window
    today = dt.date.today()
    return (today + dt.timedelta(days=LEAD_DAYS)) <= e["start"] <= (today + dt.timedelta(days=HORIZON_DAYS))


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
                 "Tags": "rocket", "Click": BOARD_URL,
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


# ---------------- board (collapsible, filterable HTML page) ------------------
CAT = {"founder": "founder", "vc": "VC", "hiring": "hiring",
       "conference": "conf", "other": "other", "keyword": "match"}

FONT_LINKS = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
              'family=Bricolage+Grotesque:wght@600;700&family=Hanken+Grotesk:wght@400;500;600&'
              'family=JetBrains+Mono&display=swap">')

BOARD_CSS = """<style>
:root{--bg:#f6f7f9;--surface:#fff;--ink:#151a21;--muted:#616b7a;--border:#e4e7ec;
--accent:#2f6bff;--soft:#2f6bff14;--founder:#7c5cff;--vc:#12a150;--hiring:#dd8409;
--conference:#2f6bff;--other:#8a94a6;--shadow:0 1px 2px rgba(20,24,33,.05),0 6px 20px rgba(20,24,33,.05);}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0d1015;--surface:#151a21;
--ink:#e7ebf1;--muted:#98a2b2;--border:#232b36;--accent:#6b93ff;--soft:#6b93ff1f;--founder:#9d86ff;
--vc:#39c07a;--hiring:#f0a63a;--conference:#6b93ff;--other:#98a2b2;--shadow:none;}}
:root[data-theme="dark"]{--bg:#0d1015;--surface:#151a21;--ink:#e7ebf1;--muted:#98a2b2;--border:#232b36;
--accent:#6b93ff;--soft:#6b93ff1f;--founder:#9d86ff;--vc:#39c07a;--hiring:#f0a63a;--conference:#6b93ff;
--other:#98a2b2;--shadow:none;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font-family:"Hanken Grotesk",system-ui,-apple-system,sans-serif;line-height:1.45;
-webkit-font-smoothing:antialiased;}
.wrap{max-width:720px;margin:0 auto;padding:0 18px 64px;}
header{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 88%,transparent);
backdrop-filter:blur(10px);border-bottom:1px solid var(--border);padding:18px 0 12px;margin-bottom:8px;}
.title{font-family:"Bricolage Grotesque","Hanken Grotesk",sans-serif;font-weight:700;
font-size:1.5rem;letter-spacing:-.02em;margin:0;display:flex;align-items:center;gap:.5rem;}
.dot{width:9px;height:9px;border-radius:50%;background:var(--accent);
box-shadow:0 0 0 4px var(--soft);}
.sub{color:var(--muted);font-size:.82rem;margin:.25rem 0 .9rem;}
.filters{display:flex;flex-wrap:wrap;gap:6px;align-items:center;}
.chip{font:inherit;font-size:.78rem;font-weight:600;padding:5px 11px;border-radius:999px;
border:1px solid var(--border);background:var(--surface);color:var(--muted);cursor:pointer;
transition:.15s;}
.chip:hover{border-color:var(--accent);color:var(--ink);}
.chip[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:#fff;}
.chip.major[aria-pressed="true"]{background:var(--hiring);border-color:var(--hiring);}
.spacer{flex:1}
.themebtn{margin-left:auto;background:none;border:1px solid var(--border);border-radius:8px;
color:var(--muted);cursor:pointer;padding:5px 9px;font-size:.9rem;}
details{background:var(--surface);border:1px solid var(--border);border-radius:14px;
margin:10px 0;box-shadow:var(--shadow);overflow:hidden;}
summary{list-style:none;cursor:pointer;padding:13px 16px;display:flex;align-items:baseline;
gap:.6rem;font-weight:600;}
summary::-webkit-details-marker{display:none}
summary::after{content:"›";margin-left:auto;color:var(--muted);font-size:1.2rem;
transform:rotate(90deg);transition:transform .2s;}
details[open] summary::after{transform:rotate(-90deg);}
.daycount{color:var(--muted);font-weight:500;font-size:.8rem;}
.ev{display:flex;gap:.8rem;padding:11px 16px;border-top:1px solid var(--border);align-items:baseline;}
.ev:first-of-type{border-top:1px solid var(--border);}
.time{font-family:"JetBrains Mono",ui-monospace,monospace;font-size:.74rem;color:var(--muted);
white-space:nowrap;min-width:64px;font-variant-numeric:tabular-nums;padding-top:1px;}
.body{flex:1;min-width:0;}
.evt{color:var(--ink);text-decoration:none;font-weight:600;font-size:.94rem;}
.evt:hover{color:var(--accent);text-decoration:underline;}
.meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:5px;align-items:center;}
.tag{font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.03em;
padding:2px 7px;border-radius:5px;color:#fff;}
.src{font-size:.72rem;color:var(--muted);}
.new{font-size:.66rem;font-weight:800;color:var(--accent);border:1px solid var(--accent);
padding:1px 5px;border-radius:5px;text-transform:uppercase;}
.star{color:var(--hiring);}
.empty{color:var(--muted);text-align:center;padding:40px 0;}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>"""

BOARD_JS = """<script>
(function(){
 var root=document.documentElement;
 var saved=localStorage.getItem('fr-theme'); if(saved)root.setAttribute('data-theme',saved);
 document.getElementById('theme').onclick=function(){
   var d=(root.getAttribute('data-theme')==='dark')?'light':'dark';
   root.setAttribute('data-theme',d);localStorage.setItem('fr-theme',d);};
 var cat='all',major=false;
 function apply(){
   document.querySelectorAll('.ev').forEach(function(e){
     var ok=(cat==='all'||e.dataset.cat===cat)&&(!major||e.dataset.caliber==='major');
     e.style.display=ok?'':'none';});
   document.querySelectorAll('details').forEach(function(d){
     var vis=d.querySelectorAll('.ev:not([style*="none"])').length;
     d.style.display=vis?'':'none';
     var c=d.querySelector('.daycount'); if(c)c.textContent=vis+(vis===1?' event':' events');});
 }
 document.querySelectorAll('.chip[data-cat]').forEach(function(b){
   b.onclick=function(){cat=b.dataset.cat;
     document.querySelectorAll('.chip[data-cat]').forEach(function(x){x.setAttribute('aria-pressed',x===b);});
     apply();};});
 var mj=document.getElementById('majorToggle');
 mj.onclick=function(){major=!major;mj.setAttribute('aria-pressed',major);apply();};
})();
</script>"""


def _esc(s):
    return ((s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_board_inner(events, generated, new_ids=None):
    new_ids = new_ids or set()
    evs = sorted(events, key=lambda e: (e["start"] or dt.date.max, e.get("when", "")))
    groups = {}
    for e in evs:
        groups.setdefault(e["start"], []).append(e)
    sections = []
    for i, (d, items) in enumerate(groups.items()):
        label = (d.strftime("%a, %b ") + str(d.day)) if d else "Undated"
        rows = []
        for e in items:
            cat = e.get("category", "other")
            cat = cat if cat in CAT else "other"
            tm = re.search(r"(\d{1,2}:\d{2}\s*[APap][Mm])", e.get("when", ""))
            time = tm.group(1).upper().replace(" ", "") if tm else "—"
            star = ' <span class="star" title="major">★</span>' if e.get("caliber") == "major" else ""
            newb = ' <span class="new">new</span>' if event_id(e) in new_ids else ""
            url = _esc(e.get("url") or "#")
            rows.append(
                f'<div class="ev" data-cat="{cat}" data-caliber="{_esc(e.get("caliber","minor"))}">'
                f'<span class="time">{_esc(time)}</span><div class="body">'
                f'<a class="evt" href="{url}" target="_blank" rel="noopener">{_esc(e["title"])}</a>{newb}'
                f'<div class="meta"><span class="tag" style="background:var(--{cat})">{CAT[cat]}</span>'
                f'{star}<span class="src">{_esc(e.get("location") or e["source"])}</span></div></div></div>')
        openattr = " open" if i < 2 else ""
        sections.append(
            f'<details{openattr}><summary>{_esc(label)}'
            f'<span class="daycount">{len(items)} events</span></summary>{"".join(rows)}</details>')
    body = "".join(sections) or '<p class="empty">No founder/VC/hiring events in the window right now.</p>'
    return (FONT_LINKS + BOARD_CSS +
            '<div class="wrap"><header>'
            '<h1 class="title"><span class="dot"></span>Founder Radar'
            '<button id="theme" class="themebtn" title="Toggle theme">◐</button></h1>'
            f'<p class="sub">SF founder · VC · hiring events, {LEAD_DAYS}–{HORIZON_DAYS} days out · '
            f'updated {generated}</p>'
            '<div class="filters">'
            '<button class="chip" data-cat="all" aria-pressed="true">All</button>'
            '<button class="chip" data-cat="founder">Founders</button>'
            '<button class="chip" data-cat="vc">VCs</button>'
            '<button class="chip" data-cat="hiring">Hiring</button>'
            '<button class="chip" data-cat="conference">Conferences</button>'
            '<button class="chip major" id="majorToggle" aria-pressed="false">★ Major only</button>'
            '</div></header>'
            f'<main>{body}</main></div>' + BOARD_JS)


def write_board(events, new_ids=None):
    gen = dt.datetime.now().strftime("%b %d, %I:%M %p") if False else dt.date.today().isoformat()
    inner = build_board_inner(events, gen, new_ids)
    doc = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
           '<meta name="viewport" content="width=device-width,initial-scale=1">'
           '<title>Founder Radar</title>'
           '<link rel="preconnect" href="https://fonts.googleapis.com">'
           '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
           '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
           'family=Bricolage+Grotesque:wght@600;700&family=Hanken+Grotesk:wght@400;500;600&'
           'family=JetBrains+Mono&display=swap">'
           '</head><body>' + inner + '</body></html>')
    out = Path(__file__).parent / "docs"
    out.mkdir(exist_ok=True)
    (out / "index.html").write_text(doc, encoding="utf-8")
    (out / "board_inner.html").write_text(inner, encoding="utf-8")  # for Artifact preview
    print(f"[ok] wrote board ({len(events)} events)")


# ---------------- main --------------------------------------------------------
def main():
    seen = set(json.loads(SEEN_FILE.read_text(encoding="utf-8"))) if SEEN_FILE.exists() else set()
    first_run = not seen

    raw = gather_all()
    by_id = {}
    for e in raw:
        if within_horizon(e):
            by_id[event_id(e)] = e
    print(f"[info] {len(raw)} scraped -> {len(by_id)} in window "
          f"[+{LEAD_DAYS}d .. +{HORIZON_DAYS}d]")

    new_ids = {eid for eid in by_id if eid not in seen}
    print(f"[info] {len(new_ids)} new since last run")

    # classify ALL in-window events (for the board); keep the relevant ones
    kept = classify(list(by_id.values()))
    print(f"[info] {len(kept)} relevant (founder/VC/hiring)")
    write_board(kept, new_ids)                       # always refresh the board

    new_kept = [e for e in kept if event_id(e) in new_ids]

    if DRY_RUN:
        print(f"[dry-run] board written; {len(new_kept)} would push")
        return

    if first_run:
        SEEN_FILE.write_text(json.dumps(sorted(by_id.keys()), indent=0), encoding="utf-8")
        print("[info] first run — seeded baseline + board, no push")
        return

    if new_kept:
        notify_ntfy(new_kept)
        notify_email(new_kept)

    seen.update(by_id.keys())
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=0), encoding="utf-8")


if __name__ == "__main__":
    main()
