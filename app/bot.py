"""
Truth Social → Telegram Stock/Finance Alert Bot
Polls Trump's Truth Social, uses Claude AI to classify posts,
and forwards finance-relevant ones to a Telegram chat.
"""

import os
import re
import json
import time
import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config from environment ──────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN     = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
TELEGRAM_ADMIN_ID  = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
HEALTHCHECK_URL    = os.getenv("HEALTHCHECK_URL", "")
POLL_INTERVAL      = int(os.getenv("POLL_INTERVAL_SECONDS", "900"))

STATE_FILE = Path("/data/state.json")

# ── Persistence ──────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}

def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))

# ── Truth Social via trumpstruth.org RSS ─────────────────────────────────────

TRUTH_RSS_URL = "https://www.trumpstruth.org/feed"
TRUTH_NS = "https://truthsocial.com/ns"

def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()

def fetch_truth_posts(since_id: str | None) -> list[dict]:
    try:
        resp = requests.get(TRUTH_RSS_URL, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except (requests.RequestException, ET.ParseError) as e:
        log.error(f"Truth Social RSS fetch failed: {e}")
        return []

    posts = []
    for item in root.findall(".//item"):
        original_id  = item.findtext(f"{{{TRUTH_NS}}}originalId", "")
        original_url = item.findtext(f"{{{TRUTH_NS}}}originalUrl", "")
        text         = strip_html(item.findtext("description", ""))
        pub_date     = item.findtext("pubDate", "")

        if not original_id or not text:
            continue
        if since_id and int(original_id) <= int(since_id):
            continue

        posts.append({"id": original_id, "text": text, "url": original_url, "created_at": pub_date})

    # RSS is newest-first; process oldest-first
    return list(reversed(posts))

# ── Claude AI classification ─────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a financial content classifier.
Your job is to determine whether a post is likely to move financial markets or affect investor sentiment. Flag it if it relates to:
- Stock markets (any exchange, any country)
- Individual company stocks or share prices
- Earnings, revenue, profit/loss announcements
- IPOs, mergers, acquisitions, or corporate actions
- Economic indicators (interest rates, inflation, GDP, unemployment)
- Specific named companies (even without explicit stock mention)
- Cryptocurrency markets or tokens
- Tariffs, trade deals, sanctions, or export controls
- Wars, military conflicts, or geopolitical tensions that could disrupt markets or supply chains
- Energy markets (oil, gas, commodities) or supply chain disruptions
- Central bank policy or government spending decisions

Use your judgement — if a reasonable investor would consider this market-moving news, flag it.

Respond ONLY with a JSON object in this exact format:
{"relevant": true, "reason": "brief reason"}
or
{"relevant": false, "reason": "brief reason"}

Do not include any other text."""

def is_finance_relevant(text: str) -> tuple[bool, str]:
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 150,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": text}],
            },
            timeout=20,
        )
        resp.raise_for_status()
        content = resp.json()["content"][0]["text"].strip()

        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        content = content.strip()

        result = json.loads(content)
        return result.get("relevant", False), result.get("reason", "")

    except Exception as e:
        log.warning(f"Claude classification failed: {e} — defaulting to keyword check")
        return keyword_fallback(text)

def keyword_fallback(text: str) -> tuple[bool, str]:
    keywords = [
        "$", "stock", "share", "market", "ipo", "nasdaq", "nyse",
        "earnings", "revenue", "profit", "loss", "acquisition", "merger",
        "bull", "bear", "rally", "crash", "fed", "interest rate",
        "inflation", "gdp", "sec", "trading", "invest", "dividend",
        "ticker", "etf", "s&p", "dow jones", "dax", "ftse",
        "tariff", "sanction", "oil", "war", "conflict",
    ]
    text_lower = text.lower()
    for kw in keywords:
        if kw in text_lower:
            return True, f"keyword match: '{kw}'"
    return False, "no finance keywords found"

# ── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(post_id: str, text: str, created_at: str, url: str, reason: str):
    ts = ""
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            ts = dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            ts = created_at

    message = (
        f"📈 *Finance mention detected*\n"
        f"🟥 Truth Social  ·  @realDonaldTrump"
        + (f"  ·  {ts}" if ts else "") + "\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{text}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 _{reason}_\n"
        f"🔗 [View post]({url})"
    )

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown",
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
        resp.raise_for_status()
        log.info(f"✅ Sent to Telegram: {post_id}")
    except requests.RequestException as e:
        log.error(f"Telegram send failed: {e}")

# ── Monitoring ───────────────────────────────────────────────────────────────

def ping_healthcheck():
    if not HEALTHCHECK_URL:
        return
    try:
        requests.get(HEALTHCHECK_URL, timeout=10)
    except Exception as e:
        log.warning(f"Healthcheck ping failed: {e}")

def alert_admin(message: str):
    if not TELEGRAM_ADMIN_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_ADMIN_ID, "text": f"🚨 *stockbot alert*\n{message}", "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass

# ── Main loop ────────────────────────────────────────────────────────────────

def run():
    log.info(f"🚀 Bot started — monitoring Truth Social/@realDonaldTrump every {POLL_INTERVAL}s")
    alert_admin("🟢 Bot started successfully.")
    consecutive_errors = 0

    while True:
        try:
            state = load_state()
            ts_last_id = state.get("ts_last_id")

            log.info(f"Polling Truth Social/@realDonaldTrump (since_id={ts_last_id})")
            posts = fetch_truth_posts(ts_last_id)

            if not posts:
                log.info("No new posts.")
            else:
                log.info(f"Found {len(posts)} new post(s).")

            for post in posts:
                post_id = post["id"]
                text    = post["text"]
                url     = post["url"]
                created = post["created_at"]

                if text.startswith("RT @"):
                    log.info(f"  Skipping retruth {post_id}")
                else:
                    log.info(f"  Classifying {post_id}: {text[:80]}…")
                    relevant, reason = is_finance_relevant(text)
                    if relevant:
                        log.info(f"  ✅ Relevant ({reason}) — sending to Telegram")
                        send_telegram(post_id, text, created, url, reason)
                    else:
                        log.info(f"  ❌ Not relevant ({reason})")

                state["ts_last_id"] = post_id
                save_state(state)

            consecutive_errors = 0
            ping_healthcheck()

        except Exception as e:
            consecutive_errors += 1
            log.error(f"Poll cycle failed: {e}")
            if consecutive_errors >= 3:
                alert_admin(f"❌ Bot has failed {consecutive_errors} polls in a row:\n`{e}`")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()
