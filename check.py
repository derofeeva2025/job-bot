#!/usr/bin/env python3
"""Watch DOU RSS feeds and send new vacancies to Telegram.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Flags: --dry  print instead of sending, do not save state
"""
import datetime, html, json, os, re, sys, urllib.error, urllib.parse, urllib.request
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

FEEDS = {  # tag -> feed url
    "QA": "https://jobs.dou.ua/vacancies/feeds/?remote&category=QA",
    "PM": "https://jobs.dou.ua/vacancies/feeds/?remote&category=Project%20Manager",
}
LEGACY_TAGS = ["QA"]  # feeds that were already running before feeds.json existed
EXCLUDE_TITLE = re.compile(r"\b(junior|trainee|intern|internship|стаж[её]р)\b", re.I)
BASE = os.path.dirname(os.path.abspath(__file__))
SEEN_FILE = os.path.join(BASE, "seen.json")
FEEDS_FILE = os.path.join(BASE, "feeds.json")  # feeds whose backlog is already remembered
MAX_SEEN = 1000
TZ = ZoneInfo("Europe/Zurich")
ACTIVE_FROM, ACTIVE_TO = datetime.time(9, 0), datetime.time(21, 30)  # scheduled runs only inside this window
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

def items(feed_url, tag=""):
    root = ET.fromstring(fetch(feed_url))
    for it in root.iter("item"):
        yield {
            "tag": tag,
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

def load_feeds():
    try:
        with open(FEEDS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return None

def save_feeds(tags):
    with open(FEEDS_FILE, "w") as f:
        json.dump(sorted(tags), f)

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(seen[-MAX_SEEN:], f, indent=0)

SECTIONS = {
    "req": re.compile(r"^(основні вимоги|вимоги|що потрібно|нам потрібен|ми очікуємо|очікування|job requirements?|requirements?|must[- ]have|required|what we.?re looking for|who we.?re looking for|qualifications|about you|your skills|we need your|what you bring|what we expect)", re.I),
    "nice": re.compile(r"^(буде плюсом|буде перевагою|nice[- ]to[- ]have|would be a plus|will be a plus|bonus)", re.I),
    "resp": re.compile(r"^(основні обов.?язки|обов.?язки|завдання|з чим ти|job responsibilities|key responsibilities|responsibilities|what you.?ll do|your role)", re.I),
}
TECH = ["Playwright", "Selenium", "Cypress", "Appium", "Postman", "SQL", "REST", "API", "GraphQL", "TypeScript", "JavaScript",
        "Python", "Java", "C#", "Kotlin", "Swift", "Jira", "TestRail", "Qase", "CI/CD", "Jenkins", "GitHub Actions", "GitLab",
        "Docker", "Kubernetes", "AWS", "Linux", "Git", "Charles", "Fiddler", "k6", "JMeter", "Pytest", "Cucumber", "Allure"]
SALARY = re.compile(r"(?:\$|€|USD|EUR)\s?\d[\d\s,.]*\d(?:\s?[-–—]\s?\d[\d\s,.]*\d)?(?:\s?(?:\$|€|USD|EUR))?"
                    r"|\d[\d\s,.]*\d(?:\s?[-–—]\s?\d[\d\s,.]*\d)?\s?(?:\$|€|USD|EUR)")
ENGLISH = re.compile(r"(?:англійськ\w*|english)[^\n.;]{0,60}?\b(A1|A2|B1|B2|C1|C2|upper[- ]intermediate|intermediate|advanced|fluent)\b"
                     r"|\b(A1|A2|B1|B2|C1|C2|upper[- ]intermediate|intermediate|advanced|fluent)\b[^\n.;]{0,30}(?:англійськ|english)", re.I)

def split_title(title):
    """'Senior QA в Company, Київ, віддалено' -> role, company, places, remote"""
    m = re.split(r"\s+(?:в|at|@)\s+", title, maxsplit=1)
    role = m[0].strip()
    company, places, remote = "", [], False
    if len(m) > 1:
        parts = [p.strip() for p in m[1].split(",")]
        company = parts[0]
        while len(parts) > 1 and re.fullmatch(r"(inc|llc|ltd|gmbh|corp|co|ag|sa|s\.r\.o|ооо)\.?", parts[1], re.I):
            company += ", " + parts.pop(1)
        for p in parts[1:]:
            low = p.lower()
            if "віддален" in low or "remote" in low:
                remote = True
            elif "за кордон" in low:
                places.append("за границей")
            elif "гібрид" in low or "hybrid" in low:
                places.append("гибрид")
            elif "офіс" in low or "office" in low:
                places.append("офис")
            elif p:
                places.append(p)
    return role, company, places, remote

def sections(text):
    out, cur = {}, None
    for line in [l.strip() for l in text.split("\n") if l.strip()]:
        is_bullet = line.startswith("•")
        bare = re.sub(r"^[^\w]+", "", line).strip() if not is_bullet else line.lstrip("• ").strip()
        if not is_bullet and len(line) <= 90 and line.rstrip().endswith((":", "?")):
            key = next((k for k, rx in SECTIONS.items() if rx.match(bare)), None)
            if key and out.get(key):
                key = None  # second (e.g. English) copy of an already captured section
            cur = key
            if key:
                out[key] = []
            continue
        if cur and is_bullet:
            out[cur].append(bare.lstrip("—–- ").strip())
        elif cur and not out[cur] and len(bare) < 200:
            out[cur].append(bare)
    return {k: [i for i in v if i] for k, v in out.items()}

def bullets(items, n, width=150):
    res = []
    for it in items[:n]:
        it = it.rstrip(";.,").strip()
        if len(it) > width:
            it = it[:width].rsplit(" ", 1)[0] + "…"
        res.append("• " + html.escape(it))
    if len(items) > n:
        res.append(f"<i>…и ещё {len(items) - n}</i>")
    return res

def message(v):
    role, company, places, remote = split_title(v["title"])
    text = v["text"]
    sec = sections(text)
    link = html.escape(v["link"], quote=True)
    lines = []
    if v.get("tag"):
        lines.append(f"#{html.escape(v['tag'])}")
    lines.append(f'<b><a href="{link}">{html.escape(role)}</a></b>')
    if company:
        lines.append(f"@ {html.escape(company)}")
    meta = []
    if remote:
        meta.append("🌐 Remote")
    if places:
        meta.append("📍 " + html.escape(", ".join(places)))
    if meta:
        lines += ["", "  ".join(meta)]
    sal = SALARY.search(text)
    if sal:
        lines.append("💰 " + html.escape(sal.group(0).strip()))
    eng = ENGLISH.search(text)
    if eng:
        lines.append("🇬🇧 Английский: " + html.escape(next(g for g in eng.groups() if g)))
    tech = [t for t in TECH if re.search(r"(?<![\w])" + re.escape(t) + r"(?![\w])", text, re.I)]
    if tech:
        lines.append("🔧 " + html.escape(", ".join(tech[:12])))

    if sec.get("req"):
        lines += ["", "<b>Требования:</b>"] + bullets(sec["req"], 7)
    if sec.get("nice"):
        lines += ["", "<b>Будет плюсом:</b>"] + bullets(sec["nice"], 4)
    if sec.get("resp") and not sec.get("req"):
        lines += ["", "<b>Задачи:</b>"] + bullets(sec["resp"], 5)
    if not sec.get("req") and not sec.get("resp"):
        pts = [l.lstrip("• ").strip() for l in text.split("\n") if l.startswith("•")]
        if pts:
            lines += [""] + bullets(pts, 6)
        else:
            short = text[:500].rsplit(" ", 1)[0] + ("…" if len(text) > 500 else "")
            lines += ["", html.escape(short)]
    return "\n".join(lines)[:4000]

def post(token, payload):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram {e.code}: {e.read().decode(errors='replace')}") from None

def send(v, token, chat_id):
    payload = {
        "chat_id": chat_id,
        "text": message(v),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": [[
            {"text": "Открыть и податься", "url": v["link"]}]]},
    }
    try:
        post(token, payload)
    except RuntimeError as e:
        if "parse entities" not in str(e):
            raise
        # HTML formatting rejected: resend as plain text
        payload.pop("parse_mode")
        payload["text"] = f"{v['title']}\n\n{v['text'][:DESC_LIMIT]}"[:4000]
        post(token, payload)

def in_active_hours():
    return ACTIVE_FROM <= datetime.datetime.now(TZ).time() < ACTIVE_TO

def main():
    dry = "--dry" in sys.argv
    # manual runs (workflow_dispatch / PyCharm) always go through; scheduled ones only in the window
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule" and not in_active_hours():
        print("outside 09:00-21:30 Europe/Zurich, skipping")
        return
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not dry and not (token and chat_id):
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")

    by_tag = {}
    for tag, url in FEEDS.items():
        by_tag[tag] = list(items(url, tag))

    seen = load_seen()
    first_run = seen is None
    seen = seen or []
    ready = set(load_feeds() or (list(FEEDS) if first_run else LEGACY_TAGS))
    baseline = [t for t in FEEDS if t not in ready]  # feeds added later: remember backlog silently

    vacancies, dup = [], set()
    for tag, lst in by_tag.items():
        for v in lst:
            if v["link"] not in dup:
                dup.add(v["link"])
                vacancies.append(v)
    new = [v for v in vacancies if v["link"] not in seen and v["tag"] not in baseline]
    new.reverse()  # oldest first so the chat reads in order

    if dry:
        print(f"{len(vacancies)} in feeds, {len(new)} to send, first_run={first_run}, baseline={baseline}")
        for v in new[-3:]:
            print("-", "SKIP" if EXCLUDE_TITLE.search(v["title"]) else "SEND", v["tag"], v["title"])
        if new:
            print("\n--- sample message ---\n" + message(new[-1]))
        return

    if first_run:
        # do not flood the chat with the current backlog
        save_seen([v["link"] for v in vacancies])
        save_feeds(FEEDS)
        print(f"first run: remembered {len(vacancies)} vacancies, nothing sent")
        return

    for v in vacancies:
        if v["tag"] in baseline and v["link"] not in seen:
            seen.append(v["link"])
    sent = 0
    for v in new:
        seen.append(v["link"])
        if EXCLUDE_TITLE.search(v["title"]):
            continue
        send(v, token, chat_id)
        sent += 1
    save_seen(seen)
    save_feeds(FEEDS)
    print(f"sent {sent}, new {len(new)}, baselined feeds: {baseline}")

if __name__ == "__main__":
    main()
