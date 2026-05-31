# X → Telegram Finance Alert Bot

Monitors a target X account and forwards any tweet mentioning stocks, companies,
or financial markets to a Telegram chat — powered by Claude AI for smart classification.

## How it works

```
Every 5 min → fetch new tweets from @TARGET → Claude classifies each one
    → if finance-relevant → send formatted alert to Telegram
```

Retweets are skipped. State (last seen tweet ID) is persisted in a Docker volume
so the bot picks up where it left off after a restart.

---

## Setup

### 1. Get your API keys

| Key | Where |
|-----|-------|
| `TWITTER_API_KEY` | [twitterapi.io](https://twitterapi.io) — 100k free credits on signup |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) |
| `TELEGRAM_BOT_TOKEN` | Talk to [@BotFather](https://t.me/BotFather) → `/newbot` |
| `TELEGRAM_CHAT_ID` | See instructions in `.env.example` |

### 2. Create your Telegram bot and chat

1. Open Telegram, message `@BotFather`
2. Send `/newbot`, follow the prompts, copy the token
3. Create a new group or channel for alerts
4. Add your bot to the group/channel as admin
5. Get the chat ID (negative number for groups/channels):
   - Add `@userinfobot` to the group — it will reply with the chat ID
   - Or visit `https://api.telegram.org/bot<TOKEN>/getUpdates` after sending a message

### 3. Configure

```bash
cp .env.example .env
# Edit .env with your actual keys and target username
nano .env
```

### 4. Run

```bash
docker compose up -d --build
```

### 5. Check logs

```bash
docker compose logs -f
```

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `TARGET_X_USERNAME` | — | X handle to monitor (no `@`) |
| `POLL_INTERVAL_SECONDS` | `300` | How often to check for new tweets (seconds) |

---

## What Claude detects

- Stock tickers (`$TSLA`, `$AAPL`, etc.)
- Named companies (even without a ticker)
- Earnings, revenue, profit/loss announcements
- IPOs, mergers, acquisitions
- Market indices (S&P 500, Nasdaq, DAX, FTSE…)
- Interest rates, inflation, economic indicators affecting markets
- Cryptocurrency markets

If the Claude API is temporarily unavailable, the bot falls back to keyword matching.

---

## Cost estimate (5-min polling, 1 account)

| Service | Usage | Cost |
|---------|-------|------|
| twitterapi.io | ~288 polls/day, ~1 credit each | ~$0.04/day → free tier covers months |
| Claude Haiku | ~288 classifications/day at ~100 tokens each | ~$0.005/day |
| Telegram | Unlimited | Free |

---

## Updating the target account

Edit `.env` and restart:
```bash
docker compose down && docker compose up -d
```

## Monitoring multiple accounts

Duplicate the service in `docker-compose.yml` with a different `TARGET_X_USERNAME`
and a different volume name per instance.
