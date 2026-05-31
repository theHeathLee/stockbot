"""
X + Truth Social → Telegram Stock/Finance Alert Bot
Polls target accounts on both platforms, uses Claude AI to classify posts,
and forwards finance-relevant ones to a Telegram chat.
"""

import os
import re
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
TWITTER_API_KEY   = os.environ["TWITTER_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
TARGET_X_USERNAME = os.environ["TARGET_X_USERNAME"]
TARGET_TS_USERNAME = os.getenv("TARGET_TRUTH_SOCIAL_USERNAME", "")
POLL_INTERVAL     = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))

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

# ── X / twitterapi.io ────────────────────────────────────────────────────────

_twitter_user_id_cache: dict[str, str] = {}

def resolve_twitter_user_id(username: str) -> str | None:
    if username in _twitter_user_id_cache:
        return _twitter_user_id_cache[username]
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
            _twitter_user_id_cache[username] = user_id
        return user_id
    except requests.RequestException as e:
        log.error(f"Failed to resolve Twitter user ID for @{username}: {e}")
        return None

def fetch_tweets(username: str, since_id: str | None) -> list[dict]:
    user_id = resolve_twitter_user_id(username)
    if not user_id:
        return []

    params = {"userId": user_id, "count": 20}

    try:
        resp = requests.get(
            "https://api.twitterapi.io/twitter/user/tweet_timeline",
            params=params,
            headers={"X-API-Key": TWITTER_API_KEY},
            timeout=15,
        )
        resp.raise_for_status()
        tweets = resp.json().get("data", {}).get("tweets", [])
    except requests.RequestException as e:
        log.error(f"Twitter API request failed: {e}")
        return []

    # sinceId is ignored by twitterapi.io, so filter client-side
    if since_id:
        tweets = [t for t in tweets if int(t.get("id", 0)) > int(since_id)]

    return list(reversed(tweets))

# ── Truth Social (Mastodon API) ───────────────────────────────────────────────

TRUTH_BASE = "https://truthsocial.com"
_truth_user_id_cache: dict[str, str] = {}

def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()

def resolve_truth_user_id(username: str) -> str | None:
    if username in _truth_user_id_cache:
        return _truth_user_id_cache[username]
    try:
        resp = requests.get(
            f"{TRUTH_BASE}/api/v1/accounts/lookup",
            params={"acct": username},
            timeout=15,
        )
        resp.raise_for_status()
        user_id = resp.json().get("id")
        if user_id:
            _truth_user_id_cache[username] = user_id
        return user_id
    except requests.RequestException as e:
        log.error(f"Failed to resolve Truth Social user ID for @{username}: {e}")
        return None

def fetch_truth_posts(username: str, since_id: str | None) -> list[dict]:
    user_id = resolve_truth_user_id(username)
    if not user_id:
        return []

    params = {"limit": 20, "exclude_replies": "true", "exclude_reblogs": "true"}
    if since_id:
        params["since_id"] = since_id

    try:
        resp = requests.get(
            f"{TRUTH_BASE}/api/v1/accounts/{user_id}/statuses",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        posts = resp.json()
    except requests.RequestException as e:
        log.error(f"Truth Social API request failed: {e}")
        return []

    # Mastodon returns newest first
    return list(reversed(posts))

# ── Claude AI classification ─────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a financial content classifier.
Your job is to determine whether a post is relevant to:
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
    ]
    text_lower = text.lower()
    for kw in keywords:
        if kw in text_lower:
            return True, f"keyword match: '{kw}'"
    return False, "no finance keywords found"

# ── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(username: str, post_id: str, text: str, created_at: str, url: str, reason: str, platform: str):
    ts = ""
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            ts = dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            ts = created_at

    platform_label = "🐦 X (Twitter)" if platform == "x" else "🟥 Truth Social"

    message = (
        f"📈 *Finance mention detected*\n"
        f"{platform_label}  ·  @{username}"
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

# ── Main loop ────────────────────────────────────────────────────────────────

def process_tweet(tweet: dict):
    post_id   = tweet.get("id", "")
    text      = tweet.get("text", "")
    username  = tweet.get("author", {}).get("userName", TARGET_X_USERNAME)
    created   = tweet.get("createdAt", "")
    url       = f"https://x.com/{username}/status/{post_id}"
    return post_id, text, username, created, url

def process_truth_post(post: dict):
    post_id  = post.get("id", "")
    text     = strip_html(post.get("content", ""))
    username = post.get("account", {}).get("username", TARGET_TS_USERNAME)
    created  = post.get("created_at", "")
    url      = post.get("url", f"{TRUTH_BASE}/@{username}/{post_id}")
    return post_id, text, username, created, url

def run():
    sources = [f"X/@{TARGET_X_USERNAME}"]
    if TARGET_TS_USERNAME:
        sources.append(f"Truth Social/@{TARGET_TS_USERNAME}")
    log.info(f"🚀 Bot started — monitoring {', '.join(sources)} every {POLL_INTERVAL}s")

    while True:
        state = load_state()

        # ── Poll X ──────────────────────────────────────────────────────────
        x_last_id = state.get("x_last_id")
        log.info(f"Polling X/@{TARGET_X_USERNAME} (since_id={x_last_id})")
        tweets = fetch_tweets(TARGET_X_USERNAME, x_last_id)

        if not tweets:
            log.info("X: No new tweets.")
        else:
            log.info(f"X: Found {len(tweets)} new tweet(s).")

        for tweet in tweets:
            post_id, text, username, created, url = process_tweet(tweet)
            if not post_id or not text:
                continue
            if text.startswith("RT @"):
                log.info(f"  X: Skipping retweet {post_id}")
            else:
                log.info(f"  X: Classifying {post_id}: {text[:80]}…")
                relevant, reason = is_finance_relevant(text)
                if relevant:
                    log.info(f"  X: ✅ Relevant ({reason}) — sending to Telegram")
                    send_telegram(username, post_id, text, created, url, reason, "x")
                else:
                    log.info(f"  X: ❌ Not relevant ({reason})")
            state["x_last_id"] = post_id
            save_state(state)

        # ── Poll Truth Social ────────────────────────────────────────────────
        if TARGET_TS_USERNAME:
            ts_last_id = state.get("ts_last_id")
            log.info(f"Polling Truth Social/@{TARGET_TS_USERNAME} (since_id={ts_last_id})")
            posts = fetch_truth_posts(TARGET_TS_USERNAME, ts_last_id)

            if not posts:
                log.info("Truth Social: No new posts.")
            else:
                log.info(f"Truth Social: Found {len(posts)} new post(s).")

            for post in posts:
                post_id, text, username, created, url = process_truth_post(post)
                if not post_id or not text:
                    continue
                log.info(f"  TS: Classifying {post_id}: {text[:80]}…")
                relevant, reason = is_finance_relevant(text)
                if relevant:
                    log.info(f"  TS: ✅ Relevant ({reason}) — sending to Telegram")
                    send_telegram(username, post_id, text, created, url, reason, "truth")
                else:
                    log.info(f"  TS: ❌ Not relevant ({reason})")
                state["ts_last_id"] = post_id
                save_state(state)

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()
