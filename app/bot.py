"""
X → Telegram Stock/Finance Alert Bot
Polls a target X account, uses Claude AI to classify tweets,
and forwards finance-relevant ones to a Telegram chat.
"""

import os
import json
import time
import logging
import requests
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
TWITTER_API_KEY   = os.environ["TWITTER_API_KEY"]       # twitterapi.io key
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]     # Anthropic key
TELEGRAM_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]    # BotFather token
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]      # Target chat/channel ID
TARGET_USERNAME   = os.environ["TARGET_X_USERNAME"]     # e.g. "elonmusk" (no @)
POLL_INTERVAL     = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))  # default 5 min

STATE_FILE = Path("/data/last_tweet_id.json")

# ── Persistence: track the last seen tweet ID ────────────────────────────────

def load_last_id() -> str | None:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text()).get("last_id")
        except Exception:
            pass
    return None

def save_last_id(tweet_id: str):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"last_id": tweet_id}))

# ── X / twitterapi.io ────────────────────────────────────────────────────────

_user_id_cache: dict[str, str] = {}

def resolve_user_id(username: str) -> str | None:
    if username in _user_id_cache:
        return _user_id_cache[username]
    try:
        resp = requests.get(
            "https://api.twitterapi.io/twitter/user/info",
            params={"userName": username},
            headers={"X-API-Key": TWITTER_API_KEY},
            timeout=15,
        )
        resp.raise_for_status()
        user_id = resp.json().get("data", {}).get("id")
        if user_id:
            _user_id_cache[username] = user_id
        return user_id
    except requests.RequestException as e:
        log.error(f"Failed to resolve user ID for @{username}: {e}")
        return None

def fetch_recent_tweets(username: str, since_id: str | None) -> list[dict]:
    """
    Fetch the latest tweets from a user's timeline via twitterapi.io.
    Returns tweets in chronological order (oldest first).
    """
    user_id = resolve_user_id(username)
    if not user_id:
        log.error(f"Cannot fetch tweets — no user ID for @{username}")
        return []

    url = "https://api.twitterapi.io/twitter/user/tweet_timeline"
    params = {"userId": user_id, "count": 20}
    if since_id:
        params["sinceId"] = since_id

    headers = {"X-API-Key": TWITTER_API_KEY}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        log.error(f"Twitter API request failed: {e}")
        return []

    tweets = data.get("data", {}).get("tweets", [])
    # Return oldest first so we process & save state in correct order
    return list(reversed(tweets))

# ── Claude AI classification ─────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a financial content classifier. 
Your job is to determine whether a tweet is relevant to:
- Stock markets (any exchange, any country)
- Individual company stocks or share prices
- Earnings, revenue, profit/loss announcements
- IPOs, mergers, acquisitions, or corporate actions
- Economic indicators that directly affect markets (interest rates, inflation, GDP)
- Specific named companies (even without explicit stock mention)
- Cryptocurrency markets or tokens

Respond ONLY with a JSON object in this exact format:
{"relevant": true, "reason": "brief reason"}
or
{"relevant": false, "reason": "brief reason"}

Do not include any other text."""

def is_finance_relevant(tweet_text: str) -> tuple[bool, str]:
    """
    Ask Claude to classify whether a tweet is finance/stock relevant.
    Returns (is_relevant, reason).
    """
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
                "messages": [{"role": "user", "content": tweet_text}],
            },
            timeout=20,
        )
        resp.raise_for_status()
        content = resp.json()["content"][0]["text"].strip()

        # Strip markdown code fences if present
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        content = content.strip()

        result = json.loads(content)
        return result.get("relevant", False), result.get("reason", "")

    except Exception as e:
        log.warning(f"Claude classification failed: {e} — defaulting to keyword check")
        return keyword_fallback(tweet_text)

def keyword_fallback(text: str) -> tuple[bool, str]:
    """Simple keyword fallback if Claude API is unavailable."""
    keywords = [
        "$", "stock", "share", "market", "ipo", "nasdaq", "nyse",
        "earnings", "revenue", "profit", "loss", "acquisition", "merger",
        "bull", "bear", "rally", "crash", "fed", "interest rate",
        "inflation", "gdp", "sec", "trading", "invest", "dividend",
        "ticker", "etf", "s&p", "dow jones", "dax", "ftse",
    ]
    text_lower = text.lower()
    for kw in keywords:
        if kw in text_lower:
            return True, f"keyword match: '{kw}'"
    return False, "no finance keywords found"

# ── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(tweet: dict, reason: str):
    """Send a formatted tweet alert to the Telegram chat."""
    username   = tweet.get("author", {}).get("userName", TARGET_USERNAME)
    tweet_id   = tweet.get("id", "")
    text       = tweet.get("text", "")
    created_at = tweet.get("createdAt", "")
    url        = f"https://x.com/{username}/status/{tweet_id}"

    # Format timestamp nicely if available
    ts = ""
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            ts = dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            ts = created_at

    message = (
        f"📈 *Finance mention detected*\n"
        f"👤 @{username}"
        + (f"  ·  {ts}" if ts else "") + "\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{text}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 _{reason}_\n"
        f"🔗 [View on X]({url})"
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
        log.info(f"✅ Sent to Telegram: {tweet_id}")
    except requests.RequestException as e:
        log.error(f"Telegram send failed: {e}")

# ── Main loop ────────────────────────────────────────────────────────────────

def run():
    log.info(f"🚀 Bot started — monitoring @{TARGET_USERNAME} every {POLL_INTERVAL}s")

    while True:
        last_id = load_last_id()
        log.info(f"Polling @{TARGET_USERNAME} (since_id={last_id})")

        tweets = fetch_recent_tweets(TARGET_USERNAME, last_id)

        if not tweets:
            log.info("No new tweets.")
        else:
            log.info(f"Found {len(tweets)} new tweet(s) to evaluate.")

        for tweet in tweets:
            tweet_id   = tweet.get("id", "")
            tweet_text = tweet.get("text", "")

            if not tweet_id or not tweet_text:
                continue

            # Skip retweets (text starts with "RT @")
            if tweet_text.startswith("RT @"):
                log.info(f"  Skipping retweet {tweet_id}")
                save_last_id(tweet_id)
                continue

            log.info(f"  Classifying tweet {tweet_id}: {tweet_text[:80]}…")
            relevant, reason = is_finance_relevant(tweet_text)

            if relevant:
                log.info(f"  ✅ Relevant ({reason}) — sending to Telegram")
                send_telegram(tweet, reason)
            else:
                log.info(f"  ❌ Not relevant ({reason})")

            # Always advance the cursor
            save_last_id(tweet_id)

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()
