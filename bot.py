#!/usr/bin/env python3
"""
Trump Truth Social → Telegram Alert Bot (v3)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Primary source is the trumpstruth.org RSS feed (plain XML, fetched with
curl_cffi's Chrome TLS fingerprint). SeleniumBase browser automation against
truthsocial.com and trumpstruth.org remains as a fallback.

Requirements:
    pip install curl-cffi beautifulsoup4 python-dotenv seleniumbase

Setup:
    1. cp .env.example .env  → fill in your Telegram credentials
    2. python trump_alert_bot.py
"""

import os, re, json, time, hashlib, logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from bs4 import BeautifulSoup
from seleniumbase import SB
from dotenv import load_dotenv

# curl_cffi replaces `requests` and spoofs Chrome's TLS fingerprint
from curl_cffi import requests
# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────
load_dotenv()

BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")
POLL_EVERY = int(os.getenv("POLL_SECONDS", "90"))
SEEN_FILE  = Path("seen_posts.json")
STATE_FILE = Path("bot_state.json")

# Don't alert on posts older than this — prevents a flood of stale alerts
# when a source comes back after an outage (they're still marked as seen).
MAX_ALERT_AGE = timedelta(hours=24)

# Dead-man switch: warn via Telegram after this many consecutive
# all-sources-failed runs, then repeat the warning periodically.
FAIL_ALERT_THRESHOLD = 15
FAIL_ALERT_REPEAT    = 500

# Trump's internal Truth Social account ID (permanent)
TRUMP_ACCOUNT_ID = "107780257626128497"

# ── Keywords ───────────────────────────────────
KEYWORDS = [
    # Iran & nuclear
    "iran", "iranian", "tehran", "khamenei", "ayatollah", "mullah",
    "persian gulf", "irgc", "nuclear deal", "jcpoa", "enrichment",
    # Strait & shipping
    "strait of hormuz", "hormuz", "tanker", "oil tanker", "shipping lane",
    # War / conflict
    "war", "strike", "airstrike", "attacked", "military action",
    "missile", "bomb", "invasion", "retaliation", "troops",
    # Oil & energy
    "oil price", "crude oil", "opec", "petroleum", "gas price",
    "energy market", "barrel", "brent", "wti", "oil production",
    # Sanctions & geopolitics
    "sanction", "embargo", "maximum pressure",
]

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("TrumpAlertBot")

# ──────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────
def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()

def save_seen(seen: set):
    # Sorted so the file only changes when the set actually changes —
    # otherwise set iteration order produces a spurious git commit every run.
    SEEN_FILE.write_text(json.dumps(sorted(seen)))

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"consecutive_failures": 0}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, sort_keys=True))

def make_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:20]

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def find_keywords(text: str) -> list:
    low = text.lower()
    hits = []
    for kw in KEYWORDS:
        # Use regex word boundaries (\b) so 'war' doesn't match 'warrior'
        if re.search(rf"\b{re.escape(kw)}\b", low):
            hits.append(kw)
    return hits

def strip_html(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()

def parse_post_date(raw: str):
    """Parse an RFC-822 (RSS) or ISO-8601 (Truth Social API) date. None if unknown."""
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None

def is_fresh(post: dict) -> bool:
    """True if the post is recent enough to alert on (unknown date = fresh)."""
    dt = parse_post_date(post.get("date", ""))
    if dt is None:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - dt <= MAX_ALERT_AGE

# ──────────────────────────────────────────────
# Sessions
# ──────────────────────────────────────────────
# curl_cffi session with Chrome TLS fingerprint, used for the RSS feed
session = requests.Session(impersonate="chrome")

BROWSER_HEADERS = {
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://truthsocial.com/",
    "Origin":          "https://truthsocial.com",
    "DNT":             "1",
    "Connection":      "keep-alive",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-origin",
}


# ──────────────────────────────────────────────
# Source 1 — trumpstruth.org RSS feed (primary)
# ──────────────────────────────────────────────
def fetch_trumpstruth_rss() -> list[dict]:
    """Plain-XML feed with the full text of every post. No JS challenge,
    no browser needed — curl_cffi's Chrome TLS fingerprint is enough."""
    url = "https://www.trumpstruth.org/feed"
    resp = session.get(url, headers=BROWSER_HEADERS, timeout=30)
    if resp.status_code != 200:
        log.warning(f"RSS feed returned HTTP {resp.status_code}: {resp.text[:200]!r}")
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        log.warning(f"RSS feed is not valid XML ({e}); body starts: {resp.text[:200]!r}")
        return []

    posts = []
    for item in root.iter("item"):
        link  = (item.findtext("link") or "").strip()
        desc  = item.findtext("description") or ""
        title = item.findtext("title") or ""
        text  = strip_html(desc) or strip_html(title)
        if not text:
            continue

        # Retruths are prefixed "RT: <truthsocial url>" — strip it from the
        # text and use that URL as the link to the original post.
        rt = re.match(r"RT:\s*(https?://truthsocial\.com/\S+)\s*", text)
        if rt:
            link = rt.group(1)
            text = text[rt.end():].strip()
        if not text:
            continue

        m = re.search(r"/statuses/(\d+)", link)
        pid = f"trumpstruth-{m.group(1)}" if m else make_id(text[:150])

        posts.append({
            "id":   pid,
            "text": text[:2000],
            "url":  link or "https://truthsocial.com/@realDonaldTrump",
            "date": (item.findtext("pubDate") or "").strip(),
        })
    return posts


# ──────────────────────────────────────────────
# Source 2 — Truth Social internal API (fallback)
# ──────────────────────────────────────────────
def fetch_truthsocial_api() -> list[dict]:
    url = f"https://truthsocial.com/api/v1/accounts/{TRUMP_ACCOUNT_ID}/statuses?exclude_replies=true&limit=20"
    
    # We remove headless=True so Chrome runs 'headed'. 
    # Because you are using xvfb-run on Github Actions, it renders in a fake virtual display. 
    # This completely circumvents Turnstile's headless detection!
    with SB(uc=True) as sb:
        # Step 1: Open the main domain so Cloudflare evaluates your browser & sets the cf_clearance cookie
        try:
            sb.uc_open_with_reconnect("https://truthsocial.com/", 3)
            sb.uc_gui_click_captcha() # Explicitly solve Datacenter IP captchas!
        except Exception:
            pass
        sb.sleep(3) # Give Turnstile 3 seconds to resolve
        
        
        # Step 2: Open the actual API endpoint
        sb.uc_open_with_reconnect(url, 3)
        
        # Step 3: Extract the raw loaded JSON from the browser body
        page_source = sb.get_text("body")
        try:
            items = json.loads(page_source)
        except json.JSONDecodeError:
            log.warning(f"Truth Social API did not return JSON (likely a Cloudflare "
                        f"challenge page); body starts: {page_source[:200]!r}")
            items = []
    posts = []
    for item in items:
        # Loop over items identical to your previous implementation
        text = strip_html(item.get("content", ""))
        if not text:
            continue
            
        posts.append({
            "id":   str(item.get("id", make_id(text))),
            "text": text,
            "url":  item.get("url", f"https://truthsocial.com/@realDonaldTrump"),
            "date": item.get("created_at", ""),
        })
    return posts


# ──────────────────────────────────────────────
# Source 3 — trumpstruth.org HTML scrape (fallback)
# ──────────────────────────────────────────────
def fetch_trumpstruth() -> list[dict]:
    # Use SeleniumBase for the fallback as well, since Cloudscraper gets blocked on GitHub runner Microsoft IPs
    with SB(uc=True) as sb:
        try:
            sb.uc_open_with_reconnect("https://www.trumpstruth.org/", 3)
            sb.uc_gui_click_captcha()
        except Exception:
            pass
        sb.sleep(3)
        html = sb.get_page_source()
        
    soup = BeautifulSoup(html, "html.parser")

    posts = []
    seen_ids: set = set()

    anchors = soup.find_all("a", string=re.compile(r"Original Post", re.I))
    if not anchors:
        log.warning(f"trumpstruth.org page has no 'Original Post' anchors (likely a "
                    f"Cloudflare challenge page); body starts: {soup.get_text(' ', strip=True)[:200]!r}")

    for anchor in anchors:
        try:
            block = anchor.find_parent(["div", "article", "section", "li"])
            if not block:
                continue

            raw = block.get_text(" ", strip=True)
            raw = re.sub(
                r"\b(Original Post|Prev\.?\s*Page|Next\.?\s*Page|Trump.s Truth)\b",
                "", raw, flags=re.I
            ).strip()
            raw = re.sub(r"\s{2,}", " ", raw)

            if len(raw) < 30:
                continue

            link = anchor.get("href", "")
            if link.startswith("/"):
                link = "https://truthsocial.com" + link
            elif not link.startswith("http"):
                link = "https://truthsocial.com/@realDonaldTrump"

            pid = make_id(raw[:150])
            if pid in seen_ids:
                continue
            seen_ids.add(pid)

            posts.append({"id": pid, "text": raw[:2000], "url": link, "date": ""})
        except Exception:
            continue

    return posts

# ──────────────────────────────────────────────
# Dispatcher
# ──────────────────────────────────────────────
SOURCES = [
    ("trumpstruth.org RSS feed", fetch_trumpstruth_rss),
    ("Truth Social API (SeleniumBase)", fetch_truthsocial_api),
    ("trumpstruth.org HTML scrape (SeleniumBase)", fetch_trumpstruth),
]

def fetch_posts() -> list[dict]:
    for name, fn in SOURCES:
        try:
            posts = fn()
            if posts:
                log.info(f"✓ {len(posts)} posts via [{name}]")
                return posts
        except Exception as e:
            log.warning(f"[{name}] failed: {e}")
    log.error("All sources failed this cycle.")
    return []


# ──────────────────────────────────────────────
# Telegram
# ──────────────────────────────────────────────
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        # Telegram API doesn't need Chrome impersonation, use plain requests
        import urllib.request, urllib.parse
        payload = json.dumps({
            "chat_id":                  CHAT_ID,
            "text":                     message,
            "parse_mode":               "HTML",
            "disable_web_page_preview": False,
        }).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                log.info("✅ Telegram alert sent.")
            else:
                log.error(f"❌ Telegram returned {resp.status}")
    except Exception as e:
        log.error(f"❌ Telegram send failed: {e}")


def format_alert(post: dict, hits: list) -> str:
    kw_str  = " · ".join(f"<b>{k.upper()}</b>" for k in hits[:6])
    preview = post["text"][:900] + ("…" if len(post["text"]) > 900 else "")
    date_ln = f"🕐 {post['date']}\n" if post.get("date") else ""

    return (
        f"🚨 <b>TRUMP MARKET ALERT</b>\n\n"
        f"🔑 {kw_str}\n\n"
        f"📢 <i>{preview}</i>\n\n"
        f"{date_ln}"
        f"🔗 <a href='{post['url']}'>View original post</a>\n\n"
        f"⚠️ <i>Automated alert — verify before trading.</i>"
    )


# ──────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────
def run():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("❌ Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your .env file or GitHub Secrets")

    log.info("🤖 Trump Alert Bot v3 started (GitHub Actions Mode)")
    log.info(f"   Keywords: {len(KEYWORDS)}")

    seen  = load_seen()
    state = load_state()

    try:
        posts     = fetch_posts()
        new_alerts = 0

        if posts:
            state["consecutive_failures"] = 0
        else:
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            fails = state["consecutive_failures"]
            if fails == FAIL_ALERT_THRESHOLD or (fails > 0 and fails % FAIL_ALERT_REPEAT == 0):
                send_telegram(
                    f"⚠️ <b>TRUMP ALERT BOT — DEAD MAN SWITCH</b>\n\n"
                    f"All sources have failed {fails} runs in a row. "
                    f"The bot is NOT monitoring posts. Check the GitHub Actions logs."
                )

        for post in posts:
            pid = post["id"]
            if pid in seen:
                continue

            hits = find_keywords(post["text"])
            if hits:
                if is_fresh(post):
                    log.info(f"🎯 MATCH → {hits}")
                    log.info(f"   {post['text'][:100]}…")
                    send_telegram(format_alert(post, hits))
                    new_alerts += 1
                else:
                    log.info(f"⏭ Skipping stale match ({post.get('date', 'no date')}): {hits}")

            seen.add(pid)

        save_seen(seen)
        save_state(state)

        if new_alerts == 0:
            log.info(f"No new matches this run.")

    except Exception as e:
        log.error(f"Unexpected error: {e}")


if __name__ == "__main__":
    run()