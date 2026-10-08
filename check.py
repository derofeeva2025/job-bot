#!/usr/bin/env python3
"""Watch DOU RSS feeds and send new vacancies to Telegram.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Flags: --dry  print instead of sending, do not save state
"""
import html, json, os, re, sys, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

FEEDS = [
    "https://jobs.dou.ua/vacancies/feeds/?remote&category=QA",
    # "https://jobs.dou.ua/vacancies/feeds/?remote&category=Project%20Manager",
]
EXCLUDE_TITLE = re.compile(r"\b(junior|trainee|intern|internship|стаж[её]р)\b", re.I)
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")
MAX_SEEN = 1000
DESC_LIMIT = 2500

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 job-bot"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()

def to_text(raw):
    t = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", raw or "")
    t = re.sub(r"(?i)<li[^>]*>", "• ", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()

def items(feed_url):
    root = ET.fromstring(fetch(feed_url))
    for it in root.iter("item"):
        yield {
            "title": (it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "date": (it.findtext("pubDate") or "").strip(),
            "text": to_text(it.findtext("description")),
        }

def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return None

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(seen[-MAX_SEEN:], f, indent=0)

def message(v):
    text = v["text"]
    if len(text) > DESC_LIMIT:
        text = text[:DESC_LIMIT].rsplit(" ", 1)[0] + "…"
    body = f"<b>{html.escape(v['title'])}</b>\n{html.escape(v['date'])}\n\n{html.escape(text)}"
    return body[:4000]

def send(v, token, chat_id):
    payload = {
        "chat_id": chat_id,
        "text": message(v),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": [[
            {"text": "Открыть и податься", "url": v["link"] + "#apply"}]]},
    }
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30).read()

def main():
    dry = "--dry" in sys.argv
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not dry and not (token and chat_id):
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")

    vacancies = []
    for feed in FEEDS:
        vacancies += list(items(feed))

    seen = load_seen()
    first_run = seen is None
    seen = seen or []
    new = [v for v in vacancies if v["link"] not in seen]
    # oldest first so the chat reads in order
    new.reverse()

    if dry:
        print(f"{len(vacancies)} in feed, {len(new)} not seen, first_run={first_run}")
        for v in new[-3:]:
            skipped = bool(EXCLUDE_TITLE.search(v["title"]))
            print("-", "SKIP" if skipped else "SEND", v["title"], v["link"])
        if new:
            print("\n--- sample message ---\n" + message(new[-1]))
        return

    if first_run:
        # do not flood the chat with the current backlog
        save_seen([v["link"] for v in vacancies])
        print(f"first run: remembered {len(vacancies)} vacancies, nothing sent")
        return

    sent = 0
    for v in new:
        seen.append(v["link"])
        if EXCLUDE_TITLE.search(v["title"]):
            continue
        send(v, token, chat_id)
        sent += 1
    save_seen(seen)
    print(f"sent {sent}, new {len(new)}")

if __name__ == "__main__":
    main()
