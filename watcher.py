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
import urllib.request
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

# RSS/news feeds we scan for keyword hits.
FEEDS = {
    "Google News": (
        "https://news.google.com/rss/search?q="
        + urllib.parse.quote('Sepang F1 tickets OR "Bahrain Grand Prix in Malaysia" tiket')
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


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"hashes": {}, "seen": []}


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

        hot = SUBJECT.search(text) and ACTION.search(text)
        flag = "🚨 <b>LIKELY TICKET NEWS</b>" if hot else "ℹ️ Page changed"
        alerts.append(f"{flag}\n{name}\n{url}")


def check_feeds(state, alerts):
    seen = set(state["seen"])
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

            title = unescape(strip_html(t.group(1)))
            link = unescape(l.group(1).strip()) if l else ""
            key = hashlib.sha1((title + link).encode()).hexdigest()[:16]

            if key in seen:
                continue
            if not (SUBJECT.search(title) and ACTION.search(title)):
                continue

            seen.add(key)
            state["seen"].append(key)
            alerts.append(f"🚨 <b>{name}</b>\n{title}\n{link}")


def main():
    state = load_state()
    alerts = []

    check_pages(state, alerts)
    check_feeds(state, alerts)

    for a in alerts[:10]:
        try:
            notify(a)
            print("[sent]", a.splitlines()[0])
        except Exception as e:
            print(f"[warn] telegram send failed: {e}", file=sys.stderr)

    save_state(state)
    print(f"done, {len(alerts)} alert(s)")


if __name__ == "__main__":
    main()
