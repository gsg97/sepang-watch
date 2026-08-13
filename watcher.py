#!/usr/bin/env python3
"""
Sepang F1 2026 ticket watcher.

Polls a handful of sources and pings a Telegram chat the moment anything
looks like a ticket on-sale announcement. Designed to run on a GitHub
Actions cron. State is kept in state.json and committed back to the repo.
"""

import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE = "state.json"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# Pages we hash and diff. Any change at all is worth knowing about.
PAGES = {
    "SIC ticketing page": "https://www.sepangcircuit.com/ticketing",
    "SIC homepage": "https://www.sepangcircuit.com/",
}

# Live ticketing endpoints. These are watched by HTTP STATUS CODE, not content.
# The AudienceView seatSelect endpoint currently 500s because the performance
# record exists but seat inventory isn't published. The moment that stops being
# a 500, inventory is live. This is the earliest machine-detectable signal.
#
# Poll rate is governed by the workflow cron (~2 min). Do not lower it. A
# faster rate is abuse, adds nothing, and risks getting the IP thrown into a
# WAF blocklist on exactly the day it matters.
ENDPOINTS = {
    "Bahrain GP seat map (AudienceView)": (
        "https://avapi.bahraingp.com/Online/seatSelect.asp"
        "?createBO::WSmap=1&BOparam::WSmap::loadBestAvailable::performance_ids"
        "=45C89B1D-FC79-4F87-AC66-A9DB18A17A39"
    ),
    "Bahrain GP ticket home": "https://avapi.bahraingp.com/Online/default.asp",
    "Ticket2U search": "https://www.ticket2u.com.my/search?keyword=f1",
}

# A status in this set means "not on sale yet". Anything else is a state change
# worth waking up for.
NOT_READY = {500, 502, 503, 504, 404, 403}

# Absolute floor. The race was confirmed 26 July 2026, so anything older is
# archive noise no matter what.
FLOOR = datetime(2026, 7, 26, tzinfo=timezone.utc)

# Rolling window. Only alert on items published this recently. Without this,
# a fixed floor means week-old headlines keep qualifying forever.
MAX_AGE_HOURS = 30


def cutoff_now():
    rolling = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    return max(FLOOR, rolling)


# RSS/news feeds we scan for keyword hits.
FEEDS = {
    "Google News": (
        "https://news.google.com/rss/search?q="
        + urllib.parse.quote(
            'Sepang F1 tickets OR "Bahrain Grand Prix in Malaysia" tiket when:2d'
        )
        + "&hl=en-MY&gl=MY&ceid=MY:en"
    ),
    "Paul Tan": "https://paultan.org/feed/",
    "SoyaCincau": "https://soyacincau.com/feed/",
}

# An item must hit at least one term from each group to count.
SUBJECT = re.compile(r"\b(sepang|formula\s?1|f1|grand prix)\b", re.I)
ACTION = re.compile(
    r"\b(ticket|tiket|on sale|go(es)? on sale|presale|pre-sale|"
    r"ballot|registration|sales open|book(ing)? now|harga)\b",
    re.I,
)


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def fetch_status(url, timeout=25):
    """Return (status_code, body_snippet). Never raises on HTTP errors, since
    the error code IS the signal we care about here."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(2000).decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(2000).decode("utf-8", errors="ignore")
        except Exception:
            pass
        return e.code, body
    except Exception as e:
        print(f"[warn] {url}: {e}", file=sys.stderr)
        return None, ""


def check_endpoints(state, alerts):
    """Watch ticketing endpoints by status code. Alert on any transition out
    of a not-ready state."""
    codes = state.setdefault("codes", {})

    for name, url in ENDPOINTS.items():
        status, body = fetch_status(url)
        if status is None:
            continue

        previous = codes.get(name)
        codes[name] = status

        if previous is None:
            print(f"[init] {name}: baseline status {status}")
            continue
        if previous == status:
            continue

        # ONLY alert on a genuine transition into a working state. Movement
        # between not-ready codes (500 -> 503 -> 502) is a server being worked
        # on, and alerting on it is pure noise. Log it and move on.
        if not (previous in NOT_READY and status not in NOT_READY):
            print(f"[info] {name}: {previous} -> {status} (still not live)")
            continue

        alerts.append(
            f"🔥🔥 <b>TICKETING ENDPOINT IS LIVE</b> 🔥🔥\n"
            f"{esc(name)}\n"
            f"status {previous} → <b>{status}</b>\n"
            f'<a href="{esc(url)}">OPEN NOW</a>'
        )


def notify(text):
    payload = json.dumps(
        {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=20).read()


def strip_html(s):
    s = re.sub(r"<script.*?</script>|<style.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", unescape(s)).strip()


def canon_link(link):
    """Strip query strings so tracking params don't create phantom new items."""
    return link.split("?")[0].split("#")[0].strip().rstrip("/")


def clean_title(title):
    """Google News appends ' - Source' and varies the source name between
    polls, which is what caused duplicate alerts. Drop it."""
    return re.sub(r"\s+[-–|]\s+[^-–|]{1,60}$", "", title).strip()


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def item_date(item):
    """Pull a publish date out of an RSS item. None if unparseable."""
    m = re.search(r"<(pubDate|dc:date|published|updated)>(.*?)</\1>", item, re.S | re.I)
    if not m:
        return None
    raw = m.group(2).strip()
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"hashes": {}, "seen": [], "codes": {}}


def save_state(state):
    state["seen"] = state["seen"][-400:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def check_pages(state, alerts):
    for name, url in PAGES.items():
        try:
            text = strip_html(fetch(url))
        except Exception as e:
            print(f"[warn] {name}: {e}", file=sys.stderr)
            continue

        digest = hashlib.sha256(text.encode()).hexdigest()
        previous = state["hashes"].get(name)
        state["hashes"][name] = digest

        if previous is None:
            print(f"[init] baseline stored for {name}")
            continue
        if previous == digest:
            continue

        # Only alert when the changed page actually mentions tickets going on
        # sale. A homepage rotating its banner images is not news.
        if not (SUBJECT.search(text) and ACTION.search(text)):
            print(f"[info] {name}: content changed, no ticket keywords")
            continue

        alerts.append(f"🚨 <b>LIKELY TICKET NEWS</b>\n{esc(name)}\n{esc(url)}")


def check_feeds(state, alerts):
    seen = set(state["seen"])
    cutoff = cutoff_now()
    print(f"[info] feed cutoff: {cutoff.isoformat()}")

    for name, url in FEEDS.items():
        try:
            raw = fetch(url)
        except Exception as e:
            print(f"[warn] {name}: {e}", file=sys.stderr)
            continue

        items = re.findall(r"<item>(.*?)</item>", raw, flags=re.S | re.I)
        for item in items:
            t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S | re.I)
            l = re.search(r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", item, re.S | re.I)
            if not t:
                continue

            title = clean_title(unescape(strip_html(t.group(1))))
            link = unescape(l.group(1).strip()) if l else ""

            # Key on the canonical link ONLY. Including the title caused the
            # repeat alerts: Google News rewrites the source suffix between
            # polls, which changed the hash for an article already seen.
            key = hashlib.sha1(canon_link(link).encode()).hexdigest()[:16]

            if key in seen:
                continue

            # Hard recency gate. Undated items are skipped rather than
            # trusted, since every feed here does publish dates.
            published = item_date(item)
            if published is None or published < cutoff:
                seen.add(key)
                state["seen"].append(key)
                continue

            if not (SUBJECT.search(title) and ACTION.search(title)):
                seen.add(key)
                state["seen"].append(key)
                continue

            seen.add(key)
            state["seen"].append(key)
            stamp = published.astimezone().strftime("%d %b %H:%M")
            alerts.append(
                f"🚨 <b>{esc(name)}</b> · {stamp}\n"
                f'<a href="{esc(link)}">{esc(title)}</a>'
            )


def main():
    state = load_state()

    # Endpoint alerts are the ones that actually matter, so they run first and
    # get sent first even if the news feeds are noisy that cycle.
    urgent = []
    check_endpoints(state, urgent)

    alerts = []
    check_pages(state, alerts)
    check_feeds(state, alerts)

    for a in urgent + alerts[:10]:
        try:
            notify(a)
            print("[sent]", a.splitlines()[0])
        except Exception as e:
            print(f"[warn] telegram send failed: {e}", file=sys.stderr)

    save_state(state)
    print(f"done, {len(urgent)} urgent, {len(alerts)} news alert(s)")


if __name__ == "__main__":
    main()
