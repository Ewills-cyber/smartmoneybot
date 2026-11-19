# Smartmoney bot

A Telegram bot that watches new Solana pairs and sends **paper** calls when a pair passes a simple gate. It does not hold a wallet and it does not send swaps.

Assumption this version is built on: the unfinished bot was a DexScreener scanner with a market-cap / liquidity / volume / age gate, mint and holder checks, and paper alerts. The unused cluster settings are a smart-money add-on. A call still has to pass the original gate. A cluster is extra evidence, and it is required only when you set `REQUIRE_CLUSTER=1`.

## What a scan does

1. Read the latest Solana token profiles and boosts from the public [DexScreener API](https://docs.dexscreener.com/api/reference).
2. Optionally listen to DexScreener's profile websocket and reconnect if it drops. REST polling still runs every cycle.
3. If `SMART_WALLETS` is set, read recent transactions with Solana JSON-RPC (Helius RPC when `HELIUS_API_KEY` is set, otherwise the public RPC). A buy counts when that wallet **signed** the transaction and its balance of a mint went up.
4. Keep the deepest-liquidity pool for each mint.
5. Pass the gate: Solana, market cap above 0 and at most $250k, at least $12k liquidity, at least $8k volume in the last hour, pair younger than 72 hours when age is known.
6. Check the mint account. An active mint authority or freeze authority blocks the call. If the RPC has no answer, the call still passes unless `REQUIRE_AUTHORITIES=1`.
7. If `BIRDEYE_API_KEY` is set, block tokens with fewer than 150 holders or a top holder above 15% of supply. Supply is estimated from FDV and price when Birdeye does not return a percentage. With no key, holder checks are skipped unless `REQUIRE_HOLDERS=1`.
8. Open a paper position of `0.05` SOL (about that much in USD when the SOL price is available) and send one Telegram alert. The same mint is not called again.
9. Keep watching open paper positions, including after they leave the discovery feed. Alert at 2x, 3x, and 5x. Close at 2.5x take-profit, 0.65x stop-loss, or 180 minutes. Stop opening new calls after 4 open positions or 0.3 SOL of closed loss in the UTC day.

## Commands

`/start`, `/help`, `/status`, `/signals`, `/positions`, `/scan`, `/config`, `/wallets`, `/pause`, `/resume`.

`status`, `signals`, `scan`, and `how are you doing` still work.

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

The free plan has an ephemeral disk. `smartmoney.db` is stored at `/tmp/smartmoney.db` and is wiped on restart, redeploy, or spin-down. Paper history does not survive that. A paid disk would, and the free plan does not offer one.

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
| `TELEGRAM_CHAT_ID` | for replies | One chat id, or a comma-separated list |
| `BIRDEYE_API_KEY` | no | Holder count and top-holder share |
| `HELIUS_API_KEY` | no | Helius mainnet RPC |
| `SOLANA_RPC` | no | Full RPC URL. Wins over the Helius key |
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

The old pump.fun website endpoint is gone. It was an undocumented private API. DexScreener's documented profile and boost lists replace that feed.

## Project layout

- `main.py` — local scanner, Telegram poller, and terminal prompt
- `web.py` — Render process, `/` and `/health`
- `smartmoney_bot/` — scan, gate, paper rules, alerts, storage
- `render.yaml` — free web service Blueprint
- `Dockerfile` — container equivalent of the same process
