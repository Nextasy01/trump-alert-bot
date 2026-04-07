#!/usr/bin/env python3
"""
Trump Truth Social → Telegram Alert Bot (v3)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Uses curl_cffi to impersonate Chrome's TLS fingerprint for Truth Social,
and cloudscraper to bypass Cloudflare's bot detection on trumpstruth.org.

Requirements:
    pip install curl-cffi beautifulsoup4 python-dotenv cloudscraper

Setup:
    1. cp .env.example .env  → fill in your Telegram credentials
    2. python trump_alert_bot.py
"""

import os, re, json, time, hashlib, logging
from pathlib import Path
from bs4 import BeautifulSoup
from seleniumbase import SB
from dotenv import load_dotenv

# curl_cffi replaces `requests` and spoofs Chrome's TLS fingerprint
from curl_cffi import requests
# cloudscraper bypasses Cloudflare JS challenges
import cloudscraper
# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────
load_dotenv()

BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")
POLL_EVERY = int(os.getenv("POLL_SECONDS", "90"))
SEEN_FILE  = Path("seen_posts.json")

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
    SEEN_FILE.write_text(json.dumps(list(seen)))

def make_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:20]

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def find_keywords(text: str) -> list:
    low = text.lower()
    return [kw for kw in KEYWORDS if kw in low]

def strip_html(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()

# ──────────────────────────────────────────────
# Sessions
# ──────────────────────────────────────────────
# Session 1: curl_cffi for primary Truth Social API
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
# Source 1 — Truth Social internal API (primary)
# ──────────────────────────────────────────────
def fetch_truthsocial_api() -> list[dict]:
    url = f"https://truthsocial.com/api/v1/accounts/{TRUMP_ACCOUNT_ID}/statuses?exclude_replies=true&limit=20"
    
    # SB(uc=True) spins up a stealthy undetected Chrome session
    with SB(uc=True, headless=True) as sb:
        # Step 1: Open the main domain so Cloudflare evaluates your browser & sets the cf_clearance cookie
        sb.uc_open_with_reconnect("https://truthsocial.com/", 3)
        sb.sleep(3) # Give Turnstile 3 seconds to resolve the transparent math captcha
        
        # Step 2: Open the actual API endpoint
        sb.uc_open_with_reconnect(url, 3)
        
        # Step 3: Extract the raw loaded JSON from the browser body
        page_source = sb.get_text("body")
        try:
            items = json.loads(page_source)
        except json.JSONDecodeError:
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
# Source 2 — trumpstruth.org (fallback)
# ──────────────────────────────────────────────
# ──────────────────────────────────────────────
# Source 2 — trumpstruth.org (fallback)
# ──────────────────────────────────────────────
def fetch_trumpstruth() -> list[dict]:
    # Use cloudscraper for trumpstruth.org as it's proven to bypass their specific protections
    scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True})
    r = scraper.get("https://www.trumpstruth.org/", timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    posts = []
    seen_ids: set = set()

    for anchor in soup.find_all("a", string=re.compile(r"Original Post", re.I)):
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
    ("Truth Social API (curl_cffi)", fetch_truthsocial_api),
    ("trumpstruth.org (cloudscraper)", fetch_trumpstruth),
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

    seen = load_seen()

    try:
        posts     = fetch_posts()
        new_alerts = 0

        for post in posts:
            pid = post["id"]
            if pid in seen:
                continue

            hits = find_keywords(post["text"])
            if hits:
                log.info(f"🎯 MATCH → {hits}")
                log.info(f"   {post['text'][:100]}…")
                send_telegram(format_alert(post, hits))
                new_alerts += 1

            seen.add(pid)

        save_seen(seen)

        if new_alerts == 0:
            log.info(f"No new matches this run.")

    except Exception as e:
        log.error(f"Unexpected error: {e}")


if __name__ == "__main__":
    run()