# Small-Account Swing Agent

A conservative, **long-only swing-trading** agent built for a small account
(~$2,000) — deliberately *not* a day trader. It holds positions overnight, so it
never accrues Pattern-Day-Trader (PDT) flags, and every risk limit is scaled for
a small account. All behavior is driven by `config.yaml`.

This is a fresh project that reuses the good ideas from the day-trading agent
(broker abstraction, Finnhub filters, risk gating) but is redesigned around the
constraints of a small account.

## Why swing, not day trading

A "day trade" is buying and selling the same stock the same day. In a margin
account under **$25,000**, you're limited to 3 day-trades per 5 business days —
a day-trading bot hits that wall almost immediately. This agent **holds every
position at least overnight**, so it accrues *zero* day-trades and can run on any
account size. On $2k it also uses whole shares of $10+ names within a 25%
position cap, and risks ~$5 per trade.

## How it decides (trend-pullback)

1. **Trend (1H):** fast EMA > slow EMA, price above the slow EMA, slow EMA rising.
2. **Pullback (1H):** price recently dipped to the fast-EMA zone and is holding
   above it — buying the dip, not chasing a breakout.
3. **Confirm (15m):** short-term momentum has turned back up.

It enters with a **bracket order** (limit entry + server-side stop + take-profit)
sized for at least the configured reward:risk. The broker-side stop protects you
even if the bot or Railway is down — important for overnight holds.

## Risk & filters (all in `config.yaml`)

- Risk per trade 0.25% (~$5), one position at a time, max 2 new trades/day.
- Daily loss limit 0.75%, weekly 2%, hard halt at 6% drawdown, pause after 3
  losses in a row, no averaging down, minimum 1.5:1 reward:risk.
- Filters: minimum price $10, spread, regular hours only, no entries in the
  first 30 minutes or after 15:00 ET, earnings blackout and breaking-news block
  (both via Finnhub, free tier; they fail *open* if Finnhub is unreachable).

## Run it

```bash
pip install -r requirements.txt
python run.py                    # mode: sim in config.yaml — offline, no keys
```

Sim runs a fast, bounded loop on synthetic data so you can watch the full
pipeline. For paper trading, set `AGENT_MODE=paper` and add your Alpaca **paper**
keys (and optionally `FINNHUB_API_KEY`) in `.env`.

## Files

| File | Role |
|------|------|
| `config.yaml` | All account / risk / strategy / execution settings |
| `config.py` | Loads the YAML + secrets into typed config |
| `data.py` | Multi-timeframe bars (Alpaca; synthetic in sim) |
| `strategy.py` | Trend-pullback setup detection |
| `risk.py` | Position sizing + account-level risk gates |
| `filters.py` | Price, spread, time-window, earnings, news |
| `broker.py` | Bracket orders — `SimBroker` + `AlpacaBroker` |
| `state.py` | SQLite: trades, counters, drawdown peak |
| `engine.py` | One cycle wiring it all together |
| `run.py` | Entry point (sim loop / live poll) |

## Web service & dashboard (runs on Railway)

`app.py` is a FastAPI service with a dashboard and a background scheduler, so the
whole thing runs on Railway with nothing on your machine:

- `GET /` — dashboard (equity curve, positions with stop/target, trades, logs,
  P&L today/week, drawdown, paper-trade progress toward the 100-trade gate).
- `GET /health` — Railway healthcheck.
- `GET /api/status` · `/api/trades` · `/api/logs` · `/api/equity`.
- `POST /api/cycle` — run one cycle (token-protected if `API_TOKEN` is set).

Set `ENABLE_SCHEDULER=true` and the engine ticks every `POLL_SECONDS` during US
market hours automatically.

### Deploy

1. Push this folder to its own GitHub repo.
2. Railway → New Project → Deploy from GitHub repo → pick it. Nixpacks builds it;
   `railway.json` sets the start command and `/health` check.
3. **Storage — pick one:**
   - **Postgres (recommended):** add a PostgreSQL database in the project, then
     on the web service **reference its `DATABASE_URL`** (Variables → New →
     Add Reference → Postgres → `DATABASE_URL`). The agent auto-creates its
     tables on boot and they show up in Railway's Data tab. If you added Postgres
     but the tables never appear, the `DATABASE_URL` isn't linked to the service —
     that reference is the fix.
   - **SQLite on a volume:** add a Volume mounted at `/data` and set
     `STATE_DB=/data/swing_agent.db`. (Without a volume, SQLite is wiped on every
     redeploy.)
4. Variables: `AGENT_MODE=paper`, `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` (paper
   keys), `FINNHUB_API_KEY` (optional), `ENABLE_SCHEDULER=true`, `API_TOKEN`
   (any long random string), and either the referenced `DATABASE_URL` (Postgres)
   or `STATE_DB=/data/swing_agent.db` (volume).
5. Generate a domain, open it, and watch it run.

### The safe ramp (in `config.yaml`)

**Paper first**, take at least 100 paper trades, then go live risking **$2/trade**
for the first trades before stepping to $5. And remember: at ~$2k this must stay
a *swing* agent (holds overnight) to avoid the PDT rule.

## Honest caveat

The plumbing, risk controls, and PDT-safety are solid. What this **cannot** give
you is a proven edge — trend-pullback is a sensible, well-known setup, but like
any strategy it must be validated on real data (paper) before real money, and a
$2k account grows slowly and punishes mistakes proportionally more. This is not
investment advice. Validate in paper, size tiny going live, and expect losing
streaks.
