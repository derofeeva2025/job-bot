#!/usr/bin/env python3
"""Watch DOU RSS feeds and send new vacancies to Telegram.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Flags: --dry  print instead of sending, do not save state
"""
import datetime, html, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

FEEDS = {  # tag -> feed url
    "QA": "https://jobs.dou.ua/vacancies/feeds/?remote&category=QA",
    "PM": "https://jobs.dou.ua/vacancies/feeds/?remote&category=Project%20Manager",
}
LEGACY_TAGS = ["QA"]  # feeds that were already running before feeds.json existed
REMOTE_ONLY = True  # send only fully remote vacancies (set False to also get hybrid / office ones)
EXCLUDE_TITLE = re.compile(r"\b(junior|trainee|intern|internship|entry[- ]level|стаж[её]р)\b", re.I)
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
    t = html.unescape(html.unescape(t)).replace("\xa0", " ")  # some feeds are double-escaped
    t = re.sub(r"^\s*(Відгукнутись на вакансію|Відгукнутися на вакансію|Apply)\s*$", "", t, flags=re.M | re.I)
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


ROLE_RX = re.compile(r"\b(qa|sdet|quality|test(er|ing)?|projektleit\w*|project manager|project coordinator|program manager|delivery manager|scrum master|it[- ]projektmanager)\b", re.I)

def fetch_json(url):
    return json.loads(fetch(url))

def swissdev_items():
    """swissdevjobs.ch open JSON: Tester/Manager categories + role keywords in title."""
    for j in fetch_json("https://swissdevjobs.ch/api/jobsLight"):
        title = j.get("name") or ""
        if j.get("isPaused"):
            continue
        if j.get("techCategory") != "Tester" and not ROLE_RX.search(title):
            continue
        if REMOTE_ONLY and str(j.get("workplace", "")).lower() != "remote":
            continue
        sal = ""
        if j.get("annualSalaryFrom"):
            sal = f"CHF {j['annualSalaryFrom']:,}".replace(",", " ")
            if j.get("annualSalaryTo"):
                sal += f" – {j['annualSalaryTo']:,}".replace(",", " ")
        yield {
            "tag": "CH", "kind": "swissdev", "title": title,
            "link": "https://swissdevjobs.ch/jobs/" + j["jobUrl"],
            "date": j.get("activeFrom", ""), "text": "",
            "company": j.get("company", ""), "city": j.get("actualCity") or j.get("cityCategory", ""),
            "workplace": j.get("workplace", ""), "salary": sal, "language": j.get("language", ""),
            "level": j.get("expLevel", ""), "tech": j.get("technologies") or [],
            "site": j.get("companyWebsiteLink") or None,
        }

def wwr_items():
    """We Work Remotely: one RSS, keep only QA / PM roles."""
    root = ET.fromstring(fetch("https://weworkremotely.com/remote-jobs.rss"))
    for it in root.iter("item"):
        raw = (it.findtext("title") or "").strip()
        company, _, role = raw.partition(": ")
        if not role:
            company, role = "", raw
        if not ROLE_RX.search(role):
            continue
        region = (it.findtext("region") or "").strip()
        yield {
            "tag": "WWR", "title": f"{role.strip()} at {company.strip()}, {region}, remote".replace(", ,", ","),
            "link": (it.findtext("link") or "").strip(),
            "date": (it.findtext("pubDate") or "").strip(),
            "text": to_text(it.findtext("description")),
        }


DJINNI_CATS = {"QA Manual", "QA Automation"}  # PM roles are matched by title (their PM category is too broad)
REMOTE_RX = re.compile(r"\b(remote|віддален\w*|дистанційн\w*|удален\w*)\b", re.I)

def djinni_items():
    """Djinni RSS: latest ~100 vacancies of all kinds (filters are ignored), so we filter here."""
    root = ET.fromstring(fetch("https://djinni.co/jobs/rss/"))
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        cats = {c.text for c in it.findall("category") if c.text}
        if not (cats & DJINNI_CATS or ROLE_RX.search(title)):
            continue
        text = to_text(it.findtext("description"))
        if REMOTE_ONLY and not REMOTE_RX.search(text):
            continue
        yield {
            "tag": "DJ", "title": title, "link": (it.findtext("link") or "").strip(),
            "date": (it.findtext("pubDate") or "").strip(), "text": text,
            "remote": bool(REMOTE_RX.search(text)),
        }


CAREERS_FILE = os.path.join(BASE, "careers.json")  # domain -> careers url (cache)
CAREER_WORDS = re.compile(r"career|karriere|jobs?\b|join[- ]us|work[- ]with[- ]us|hiring|vacanc|вакан|кар.?єр|работа у нас|stellen|offene", re.I)
ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com", "teamtailor.com",
             "recruitee.com", "bamboohr.com", "personio.", "breezy.hr", "jobs.", "careers.", "apply.", "join.com", "pinpointhq.com")
COMMON_PATHS = ["/careers", "/careers/", "/career", "/jobs", "/company/careers", "/about/careers", "/join-us", "/karriere", "/vacancies"]

def _get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (job-bot)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.geturl(), r.read(400_000).decode("utf-8", errors="replace")

def load_careers():
    try:
        with open(CAREERS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def dou_company_site(vacancy_link):
    m = re.search(r"jobs\.dou\.ua/companies/([^/]+)/", vacancy_link)
    if not m:
        return None
    _, page = _get(f"https://jobs.dou.ua/companies/{m.group(1)}/")
    m = re.search(r'class="site"[^>]*>\s*<a[^>]+href="(https?://[^"]+)"', page)
    return m.group(1) if m else None

def find_careers(site):
    """Return the company's careers/jobs page, or the site itself if none was found."""
    if not re.match(r"https?://", site):
        site = "https://" + site.lstrip("/")
    host = urllib.parse.urlparse(site).netloc.lower().removeprefix("www.")
    if not host or host.endswith("swissdevjobs.ch"):
        return None  # missing or just the job board itself
    cache = load_careers()
    deadline = time.monotonic() + 15  # never spend more than ~15 s on one company
    if host in cache:
        return cache[host]
    found = None
    try:
        final, page = _get(site)
        base = final
        cands = []
        for m in re.finditer(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', page, re.S | re.I):
            href, txt = m.group(1).strip(), re.sub(r"<[^>]+>", " ", m.group(2)).strip()
            if href.startswith(("mailto:", "tel:", "javascript:")):
                continue
            url = urllib.parse.urljoin(base, html.unescape(href))
            u = urllib.parse.urlparse(url)
            score = 0
            if CAREER_WORDS.search(txt) and len(txt) < 40:
                score += 2
            if re.search(r"/blog|/news|/press|/article|/post/", u.path, re.I):
                continue  # articles about careers are not the careers page
            if re.search(r"/(careers?|karriere|jobs?|vacanc\w*|stellen\w*|offene-stellen|join-us|work-with-us)(/|$)", u.path, re.I):
                score += 2
            if any(h in u.netloc for h in ATS_HOSTS):
                score += 1
            if score >= 2:
                cands.append((score, url))
        if cands:
            found = max(cands, key=lambda c: c[0])[1]
    except Exception:
        pass
    if not found:
        root = f"{urllib.parse.urlparse(site).scheme}://{urllib.parse.urlparse(site).netloc}"
        for p in COMMON_PATHS:
            if time.monotonic() > deadline:
                break
            try:
                final, _ = _get(root + p, timeout=4)
                if urllib.parse.urlparse(final).path.strip("/"):  # did not bounce to the homepage
                    found = final
                    break
            except Exception:
                continue
    found = found or site
    cache = load_careers()
    cache[host] = found
    try:
        with open(CAREERS_FILE, "w") as f:
            json.dump(cache, f, indent=0, ensure_ascii=False)
    except OSError:
        pass
    return found

def enrich(v):
    """Attach the company's careers page to a vacancy (best effort)."""
    try:
        site = v.get("site")
        if not site and v["tag"] in ("QA", "PM"):
            site = dou_company_site(v["link"])
        if site:
            v["careers"] = find_careers(site) or None
    except Exception as e:
        print(f"careers lookup failed for {v['title'][:40]}: {e}")
    return v


STRICT_RX = re.compile(r"\b(qa|sdet|aqa|quality assurance|software test\w*|test automation|test(er| engineer| manager| lead| analyst)|projektleit\w*|project (manager|coordinator|lead)|program manager|delivery manager|scrum master|it[- ]projektmanager)\b", re.I)
NOISE_RX = re.compile(r"\b(rater|evaluator|trainer|labell?er|annotator|dispense|hardware|electronics|production test)\b|hochspannung|anlagen|elektro|maschinen", re.I)
LOC_BAD = re.compile(r"\b(us|usa|u\.s\.a?\.?|united states|canada|north america|latam|latin america|india|philippines|brazil|mexico|australia|new zealand)\b", re.I)
LOC_OK = re.compile(r"\b(worldwide|anywhere|global|europe|emea|eu|european|switzerland|swiss|dach)\b", re.I)
SWISS_RX = re.compile(r"\b(ch|schweiz|switzerland|z[uü]rich|bern|basel|gen[fe]\w*|luzern|lausanne|winterthur|st\. gallen|zug)\b", re.I)

def wanted(title, loc=""):
    """QA/PM role, not noise, and open to someone living in Switzerland
    (empty location, or worldwide/Europe/Switzerland; a named single country excludes it)."""
    if not STRICT_RX.search(title) or NOISE_RX.search(title):
        return False
    if LOC_OK.search(loc):
        return True
    if LOC_BAD.search(loc):
        return False
    return not re.sub(r"remote|\W", "", loc, flags=re.I)  # only "remote"/empty -> open; any other named place -> no

def money(lo, hi, cur="$"):
    try:
        lo, hi = int(float(lo or 0)), int(float(hi or 0))
    except (TypeError, ValueError):
        return ""
    if not lo:
        return ""
    return f"Salary: {cur}{lo:,} - {cur}{hi:,}" if hi else f"Salary: {cur}{lo:,}"

def arbeitnow_items():
    for j in fetch_json("https://www.arbeitnow.com/api/job-board-api").get("data", []):
        loc = j.get("location") or ""
        if not wanted(j["title"]) or not (j.get("remote") or (not REMOTE_ONLY and SWISS_RX.search(loc + " " + j["title"]))):
            continue
        yield {"tag": "AN", "title": f"{j['title']} at {j['company_name']}, {loc}" + (", remote" if j.get("remote") else ""),
               "link": j["url"], "date": datetime.datetime.fromtimestamp(j["created_at"], TZ).strftime("%a, %d %b %Y %H:%M"),
               "text": to_text(j.get("description"))}

def himalayas_items():
    seen = set()
    for q in ("qa engineer", "quality assurance", "test automation", "project manager"):
        d = fetch_json("https://himalayas.app/jobs/api/search?sort=recent&q=" + urllib.parse.quote_plus(q))
        for j in d.get("jobs", []):
            loc = ", ".join(j.get("locationRestrictions") or [])
            if j["guid"] in seen or not wanted(j["title"], loc):
                continue
            if any(x in str(j.get("seniority")) for x in ("Entry", "Intern")):
                continue
            seen.add(j["guid"])
            pay = money(j.get("minSalary"), j.get("maxSalary"), "$" if (j.get("currency") in (None, "None", "USD")) else str(j.get("currency")) + " ")
            yield {"tag": "HM", "title": f"{j['title']} at {j['companyName']}, {loc or 'Worldwide'}, remote",
                   "link": j.get("applicationLink") or j["guid"],
                   "date": datetime.datetime.fromtimestamp(int(j["pubDate"]), TZ).strftime("%a, %d %b %Y %H:%M"),
                   "text": (pay + "\n" if pay else "") + to_text(j.get("description") or j.get("excerpt"))}

def remoteok_items():
    for j in fetch_json("https://remoteok.com/api")[1:]:
        loc = j.get("location") or ""
        if not wanted(j.get("position", ""), loc):
            continue
        pay = money(j.get("salary_min"), j.get("salary_max"))
        yield {"tag": "ROK", "title": f"{j['position']} at {j.get('company', '')}, {loc or 'Worldwide'}, remote",
               "link": j["url"], "date": j.get("date", ""),
               "text": (pay + "\n" if pay else "") + to_text(j.get("description"))}

def workingnomads_items():
    for j in fetch_json("https://www.workingnomads.com/api/exposed_jobs/"):
        loc = j.get("location") or ""
        if not wanted(j["title"], loc):
            continue
        yield {"tag": "WN", "title": f"{j['title']} at {j.get('company_name', '')}, {loc or 'Worldwide'}, remote",
               "link": j["url"], "date": j.get("pub_date", ""), "text": to_text(j.get("description"))}

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
    "req": re.compile(r"^(основні вимоги|вимоги|що потрібно|нам потрібен|ми очікуємо|очікування|job requirements?|requirements?|must[- ]have|required|what we.?re looking for|who we.?re looking for|qualifications|about you|your skills|we need your|what you bring|what we expect|what kind of professional|кого ми шукаємо|хто нам потрібен)", re.I),
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

BULLET = re.compile(r"^(•|—|–|-|\*|·)\s+")

def sections(text):
    out, cur, intro = {}, None, []
    for line in [l.strip() for l in text.split("\n") if l.strip()]:
        is_bullet = bool(BULLET.match(line))
        bare = BULLET.sub("", line) if is_bullet else re.sub(r"^[^\w]+", "", line).strip()
        if not is_bullet and len(line) <= 90 and line.rstrip().endswith((":", "?")):
            key = next((k for k, rx in SECTIONS.items() if rx.match(bare)), None)
            if key and out.get(key):
                key = None  # second (e.g. English) copy of an already captured section
            cur = key
            if key:
                out[key] = []
            continue
        if cur:
            out[cur].append(bare)
        elif not out:
            intro.append(bare)
    res = {k: [i for i in v if i] for k, v in out.items()}
    res["intro"] = intro
    return res

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

def message_swissdev(v):
    link = html.escape(v["link"], quote=True)
    lines = [f"#{v['tag']}", f'<b><a href="{link}">{html.escape(v["title"])}</a></b>']
    if v["company"]:
        lines.append(f"@ {html.escape(v['company'])}")
    if v.get("careers"):
        lines.append(f'💼 <a href="{html.escape(v["careers"], quote=True)}">Вакансии на сайте компании</a>')
    meta = []
    if v["city"]:
        meta.append("📍 " + html.escape(v["city"]))
    if v["workplace"]:
        meta.append("🏢 " + html.escape(str(v["workplace"])))
    lines += ["", "  ".join(meta)] if meta else []
    if v["salary"]:
        lines.append("💰 " + html.escape(v["salary"]))
    if v["level"]:
        lines.append("📊 " + html.escape(str(v["level"])))
    if v["language"]:
        lines.append("🗣 Язык вакансии: " + html.escape(str(v["language"])))
    if v["tech"]:
        lines.append("🔧 " + html.escape(", ".join(map(str, v["tech"][:12]))))
    return "\n".join(lines)[:4000]

def message(v):
    if v.get("kind") == "swissdev":
        return message_swissdev(v)
    role, company, places, remote = split_title(v["title"])
    remote = remote or v.get("remote", False)
    text = v["text"]
    sec = sections(text)
    link = html.escape(v["link"], quote=True)
    lines = []
    if v.get("tag"):
        lines.append(f"#{html.escape(v['tag'])}")
    lines.append(f'<b><a href="{link}">{html.escape(role)}</a></b>')
    if company:
        lines.append(f"@ {html.escape(company)}")
    if v.get("careers"):
        lines.append(f'💼 <a href="{html.escape(v["careers"], quote=True)}">Вакансии на сайте компании</a>')
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

    intro = " ".join(sec.get("intro", []))
    if intro and (sec.get("req") or sec.get("resp")):
        intro = intro[:260].rsplit(" ", 1)[0] + "…" if len(intro) > 260 else intro
        lines += ["", html.escape(intro)]
    if sec.get("req"):
        lines += ["", "<b>Требования:</b>"] + bullets(sec["req"], 8)
    if sec.get("nice"):
        lines += ["", "<b>Будет плюсом:</b>"] + bullets(sec["nice"], 5)
    if sec.get("resp"):
        lines += ["", "<b>Задачи:</b>"] + bullets(sec["resp"], 5)
    if not sec.get("req") and not sec.get("resp"):
        pts = [BULLET.sub("", l) for l in text.split("\n") if BULLET.match(l)]
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
            {"text": "Открыть и податься", "url": v["link"]}] + (
            [{"text": "Сайт компании", "url": v["careers"]}] if v.get("careers") else [])]},
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
    sources = {tag: (lambda u=url, t=tag: list(items(u, t))) for tag, url in FEEDS.items()}
    sources["CH"] = lambda: list(swissdev_items())
    sources["WWR"] = lambda: list(wwr_items())
    sources["DJ"] = lambda: list(djinni_items())
    sources["AN"] = lambda: list(arbeitnow_items())
    sources["HM"] = lambda: list(himalayas_items())
    sources["ROK"] = lambda: list(remoteok_items())
    sources["WN"] = lambda: list(workingnomads_items())
    for tag, fn in sources.items():
        try:
            by_tag[tag] = fn()
        except Exception as e:  # one broken source must not stop the others
            print(f"source {tag} failed: {e}")

    seen = load_seen()
    first_run = seen is None
    seen = seen or []
    ready = set(load_feeds() or (list(by_tag) if first_run else LEGACY_TAGS))
    baseline = [t for t in by_tag if t not in ready]  # feeds added later: remember backlog silently

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
            print("\n--- sample message ---\n" + message(enrich(new[-1])))
        return

    if first_run:
        # do not flood the chat with the current backlog
        save_seen([v["link"] for v in vacancies])
        save_feeds(set(by_tag) | ready)
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
        enrich(v)
        send(v, token, chat_id)
        sent += 1
    save_seen(seen)
    save_feeds(set(by_tag) | ready)
    print(f"sent {sent}, new {len(new)}, baselined feeds: {baseline}")

if __name__ == "__main__":
    main()
