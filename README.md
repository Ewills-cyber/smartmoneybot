# Smartmoney bot

A Telegram bot that watches new Solana pairs and alerts when one passes a simple gate. `TRADE_MODE=paper` is the default: those alerts are paper calls and no swap is sent. `TRADE_MODE=live` can buy through GMGN.

Assumption this version is built on: the unfinished bot was a DexScreener scanner with a market-cap / liquidity / volume / age gate, mint and holder checks, and paper alerts. The unused cluster settings are a smart-money add-on. A call still has to pass the original gate. A cluster is extra evidence, and it is required only when you set `REQUIRE_CLUSTER=1`. Live buys use that same gate.

## What a scan does

1. Read the latest Solana token profiles and boosts from the public [DexScreener API](https://docs.dexscreener.com/api/reference).
2. Optionally listen to DexScreener's profile websocket and reconnect if it drops. REST polling still runs every cycle.
3. If `SMART_WALLETS` is set, read recent transactions with Solana JSON-RPC (Helius RPC when `HELIUS_API_KEY` is set, otherwise the public RPC). A buy counts when that wallet **signed** the transaction and its balance of a mint went up.
4. Keep the deepest-liquidity pool for each mint.
5. Pass the gate: Solana, market cap above 0 and at most $250k, at least $12k liquidity, at least $8k volume in the last hour, pair younger than 72 hours when age is known.
6. Check the mint account. An active mint authority or freeze authority blocks the call. If the RPC has no answer, the call still passes unless `REQUIRE_AUTHORITIES=1`.
7. If `BIRDEYE_API_KEY` is set, block tokens with fewer than 150 holders or a top holder above 15% of supply. Supply is estimated from FDV and price when Birdeye does not return a percentage. With no key, holder checks are skipped unless `REQUIRE_HOLDERS=1`.
8. Open a paper position of `0.05` SOL (about that much in USD when the SOL price is available) and send one Telegram alert. The same mint is not called again.
9. Keep watching open positions, including after they leave the discovery feed. Alert at 2x, 3x, and 5x. Close at 2.5x take-profit, 0.65x stop-loss, or 180 minutes. Stop opening new calls after 4 open positions or 0.3 SOL of closed loss in the UTC day.

## Live trading

`off` scans and does not open a position. `paper` is the default and does not send a swap. `live` buys through [GMGN's Agent API](https://docs.gmgn.ai/index/gmgn-agent-api).

The signer follows `GMGNAI/gmgn-skills` `src/client/signer.ts` and `OpenApiClient.ts`:

- Every call sends `X-APIKEY`. `timestamp` (Unix seconds, accepted for about 5 seconds) and `client_id` (a new UUID, rejected if replayed for about 7 seconds) are query parameters.
- `POST https://openapi.gmgn.ai/v1/trade/swap` and `GET /v1/trade/query_order` also send `X-Signature`.
- The signed string is `path:sorted query:json body:timestamp`. Query keys are sorted and encoded like JavaScript `encodeURIComponent`. The body is compact JSON. Ed25519 signs the raw UTF-8 bytes, and the header is standard base64. A GET signs an empty body.
- `GMGN_PRIVATE_KEY` is the PEM for the public key uploaded at [gmgn.ai/ai](https://gmgn.ai/ai). The bot uses it to sign. It is not sent to GMGN, written to logs, or shown by `/mode`.

A live buy spends `TRADE_SIZE_SOL` lamports of SOL and attaches `condition_orders` for the configured take-profit and stop-loss. On SOL those orders also send `priority_fee` and `tip_fee` (default `0.00001`). The bot polls the order until it is confirmed, failed, expired, or the attempts run out, stores the fill, and sends a Telegram message for the buy, the sell, or the failure.

GMGN condition orders cover take-profit and stop-loss. They do not cover the time stop. When GMGN returns a `strategy_order_id`, this bot leaves those two exits to GMGN and still sells 100% itself at the time stop. If the condition orders were not accepted, take-profit and stop-loss are sold the same way.

`/pause` stops new buys. It does not cancel an order GMGN already accepted. `/mode` shows the mode and cannot turn live on.

Do not set `TRADE_MODE=live` on the free web service. Live buys are allowed when `DATABASE_URL` points at Postgres, because that database survives a restart. They are still refused while the only database is a SQLite file under `/tmp`, unless `GMGN_ALLOW_EPHEMERAL_DB=1`. The Blueprint leaves `TRADE_MODE` at `paper`. GMGN also rejects IPv6 callers.

## Keep data across restarts

Render's free web service disk is wiped on restart, redeploy, and spin-down. With no `DATABASE_URL`, the bot uses SQLite and logs a warning that the file is ephemeral. Subscribers, paper wallets, open trades, watches, and `/settings` then start over.

To keep them, create a Postgres database and set `DATABASE_URL` to its connection string. The bot logs `database backend: postgres via DATABASE_URL` and does not print the URL. On startup it creates any missing tables and adds any missing columns, so an existing database is not wiped.

- **Render:** Dashboard → New → PostgreSQL. The free instance expires after 30 days. Copy the external connection string into the bot's `DATABASE_URL`.
- **Neon:** Create a project at [neon.tech](https://neon.tech), then copy the pooled connection string.
- **Supabase:** Project → Project Settings → Database → URI. Use the URI that starts with `postgresql://`.

The string looks like `postgresql://user:password@host:5432/dbname`. Paste it only in the host's secret env vars, not in git. `psycopg` is already in `requirements.txt`.

X-account follow alerts (Netro's `/follows`) are not included. X does not offer a documented free API for "who followed these accounts" without paid access, and scraping is not a substitute.

## Commands

`/start` opens a button menu: safety check, paper wallet, positions, alerts, watch wallets, leaderboard, fumbles, and help. Paste a Solana or Base contract address any time for a BUY / CAUTION / SKIP card with quick-buy links. `/wallet`, `/alerts`, `/watch`, `/fumbles`, and `/leaderboard` are the same screens.

Each subscriber gets a $1,000 paper wallet. Buy buttons on a token card spend that person's three saved sizes, which start at $25, $50, and $100. New paper buys use that person's take-profit and stop-loss, which start at 2x and 0.5x. Open positions keep the exits they were opened with, even if `/settings` changes later. `/watch <address> copy` paper-copies buys for that person's copy size, which starts at $50.

`/settings` edits those values with buttons or a typed number. Take profit has to be above 1x. Stop loss has to be above 0 and below 1x. Sizes have to be above 0 and no larger than the paper cash on hand.

`/status`, `/signals`, `/scan`, `/config`, `/mode`, `/pause`, and `/resume` are still there. `status`, `signals`, and `scan` work without the slash.

If `TELEGRAM_CHAT_ID` is empty, the first `/start` subscribes that chat. Env chat ids are seeded into the same table on boot so a wiped free-tier disk still alerts the owner after you set the variable. Alerts go to every subscriber, filtered by their toggles. GMGN live orders stay on `TRADE_MODE=live` and are not available from a button.

## Run it locally

You need Python 3.12.

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Copy the example env file and paste the token:

```bash
cp .env.example .env
```

3. Install and start:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
set -a && source .env && set +a
python main.py
```

On Windows PowerShell, set variables one by one (`$env:TELEGRAM_BOT_TOKEN="..."`) and run `python main.py`.

4. In Telegram, open your bot and send `/start`. If `TELEGRAM_CHAT_ID` is empty, the process log prints the chat id. Put that id in `.env`, stop the process, and start it again.

You can also get a chat id from [@userinfobot](https://t.me/userinfobot).

Optional keys:

- [Birdeye](https://birdeye.so) for holder concentration.
- [Helius](https://www.helius.dev) free RPC so authority and wallet reads are less likely to be rate limited.
- `SMART_WALLETS`: public addresses you already treat as smart money. The bot only reads them.

The terminal accepts the same commands. `exit` stops it.

```bash
pip install -r requirements-dev.txt
pytest
```

## Deploy on Render's free web service

The web process serves `/` and `/health`, and runs the scanner in the background. Render's health check uses `/health`. Free web services sleep after about 15 minutes without a request, so the bot also pings its own public `/health` every 8 minutes when `KEEP_AWAKE=1` and `RENDER_EXTERNAL_URL` is set (Render sets that URL for you).

A self-ping cannot wake a service that is already asleep. For a bot that should stay up, add a free external monitor that requests `https://<your-service>.onrender.com/health` every 10 minutes. [UptimeRobot](https://uptimerobot.com) or [cron-job.org](https://cron-job.org) both work. Use a 10 minute interval, which is inside the 15 minute sleep window.

The free plan has an ephemeral disk. `smartmoney.db` is stored at `/tmp/smartmoney.db` and is wiped on restart, redeploy, or spin-down. Paper history does not survive that. The Blueprint sets `TRADE_MODE=paper`. Live buys are refused on that `/tmp` path so a restart cannot send the same buy again. A paid disk would keep the file, and the free plan does not offer one.

`render.yaml` is a Blueprint for one free Python web service. The Dockerfile is the same app for a Docker deploy or a local container. The Blueprint uses the native Python runtime.

1. Push this repo to GitHub, GitLab, or Bitbucket.
2. In the Render Dashboard, open **New** → **Blueprint** and select the repo. Or open `https://dashboard.render.com/blueprint/new?repo=<your https repo url>`.
3. When Render asks for env vars, paste the Telegram token and chat id. For any optional key you do not have, enter `-`. The bot treats `-`, blank, and `unused` as unset.
4. Apply the Blueprint. The start command is `python web.py`. It listens on `0.0.0.0:$PORT`.
5. Open `/health`. A healthy process returns JSON with `"status": "ok"`.
6. Point an external monitor at `https://<service>.onrender.com/health`.

To run the container yourself:

```bash
docker build -t smartmoneybot .
docker run --env-file .env -e PORT=10000 -p 10000:10000 smartmoneybot
```

## Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | for Telegram | BotFather token |
| `TELEGRAM_CHAT_ID` | no | Owner chats. Seeded as subscribers. Empty means anyone can `/start` |
| `TELEGRAM_BOT_USERNAME` | no | Username without @, used in invite links |
| `DATABASE_URL` | no | Postgres URL. Empty uses SQLite |
| `BIRDEYE_API_KEY` | no | Holder count and top-holder share |
| `HELIUS_API_KEY` | no | Helius mainnet RPC |
| `SOLANA_RPC` | no | Full RPC URL. Wins over the Helius key |
| `TRADE_MODE` | no | `paper` (default), `off`, or `live` |
| `GMGN_API_KEY` | live only | API key from gmgn.ai/ai |
| `GMGN_PRIVATE_KEY` | live only | Ed25519 PEM. Newlines may be written as `\n` |
| `GMGN_WALLET` | live only | `from_address` bound to that API key |
| `GMGN_SLIPPAGE` | no | Integer 0–100. Unset uses automatic slippage |
| `GMGN_ANTI_MEV` | no | Default on |
| `GMGN_PRIORITY_FEE` | no | SOL priority fee sent with take-profit / stop-loss. Default `0.00001` |
| `GMGN_TIP_FEE` | no | SOL tip fee sent with those orders. Default `0.00001` |
| `GMGN_ALLOW_EPHEMERAL_DB` | no | Set `1` only if you accept duplicate buys after a wipe |
| `SMART_WALLETS` | no | Wallets whose signed buys form a cluster |
| `REQUIRE_CLUSTER` | no | `1` means a call also needs a cluster |
| `REQUIRE_AUTHORITIES` | no | `1` blocks a call when the RPC check failed |
| `REQUIRE_HOLDERS` | no | `1` blocks a call when holder data is missing |
| `MAX_ENTRY_MCAP` | no | Default `250000` |
| `MIN_LIQUIDITY_USD` | no | Default `12000` |
| `MIN_VOLUME_H1` | no | Default `8000` |
| `MAX_PAIR_AGE_HOURS` | no | Default `72` |
| `MIN_HOLDERS` | no | Default `150` |
| `MAX_TOP_HOLDER_PCT` | no | Default `15` |
| `TRADE_SIZE_SOL` | no | Default `0.05` |
| `MAX_OPEN_POSITIONS` | no | Default `4` |
| `MAX_DAILY_LOSS_SOL` | no | Default `0.3` |
| `TAKE_PROFIT_MULTIPLE` | no | Default `2.5` |
| `STOP_LOSS_MULTIPLE` | no | Default `0.65` |
| `TIME_STOP_MINUTES` | no | Default `180` |
| `MIN_CLUSTER_WALLETS` | no | Default `2` |
| `CLUSTER_WINDOW_MINUTES` | no | Default `15` |
| `POLL_SECONDS` | no | Default `45`. Values under 15 are ignored |
| `ENABLE_PROFILE_STREAM` | no | Default on. Set `0` to use REST only |
| `KEEP_AWAKE` | no | Default on when a public URL is set |
| `KEEP_AWAKE_URL` | no | Defaults to `RENDER_EXTERNAL_URL` |
| `SMARTMONEY_DB` | no | SQLite path |
| `LOG_LEVEL` | no | Default `INFO` |
| `PORT` | set by Render | HTTP port |

## Data sources

| Source | Used for | Docs |
| --- | --- | --- |
| DexScreener REST | New profiles, boosts, pair stats, SOL price | https://docs.dexscreener.com/api/reference |
| DexScreener websocket | Extra profile addresses, with reconnect | https://docs.dexscreener.com/api/websockets |
| Solana JSON-RPC | Mint and freeze authority, wallet buys | https://solana.com/docs/rpc |
| Helius RPC | Same JSON-RPC methods, with an API key | https://www.helius.dev/docs |
| Birdeye `GET /defi/v3/token/holder` | Holder count and top holder | https://docs.birdeye.so |
| RugCheck token report | Solana risk flags, LP lock, top holders | https://api.rugcheck.xyz/swagger/index.html |
| GoPlus token security | Solana mint/freeze/transfer flags; Base honeypot and sell tax | https://docs.gopluslabs.io |
| GMGN swap and order status | Live buys and sells when `TRADE_MODE=live` | https://docs.gmgn.ai/index/gmgn-agent-api |

The old pump.fun website endpoint is gone. It was an undocumented private API. DexScreener's documented profile and boost lists replace that feed.

## Project layout

- `main.py` — local scanner, Telegram poller, and terminal prompt
- `web.py` — Render process, `/` and `/health`
- `smartmoney_bot/` — scan, gate, paper rules, alerts, storage
- `render.yaml` — free web service Blueprint
- `Dockerfile` — container equivalent of the same process
