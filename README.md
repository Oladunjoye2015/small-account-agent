# Small-Account Swing Researcher

A conservative, **long-only swing-trade research system** built for a small account
(~$2,000) — deliberately *not* a day trader. It holds positions overnight, so it
never accrues Pattern-Day-Trader (PDT) flags, and every risk limit is scaled for
a small account. All behavior is driven by `config.yaml`.

Its primary job is to find, explain, size, and validate potential trades. This
project reuses the good ideas from the day-trading agent
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

The virtual forward-test ledger models a limit entry, stop, and take-profit
sized for at least the configured reward:risk. These are polling-based virtual
orders—not broker-side protection—and must never be mistaken for live brackets.

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
pipeline. For virtual forward-testing, set `AGENT_MODE=paper` and add your
Alpaca data credentials (and optionally `FINNHUB_API_KEY`) in `.env`.

## Find trades for a $2,000 account

The primary research command scans the configured universe and ranks every
symbol, including near-misses:

```bash
python scan.py
python scan.py --json
```

For actionable setups it reports the proposed limit entry, stop, target,
reward/risk, fractional share quantity, dollars at risk, and capital required.
It also reports the affordable whole-share quantity and risk, since broker
protection available for fractional positions can differ by order type.
Sizing uses the configured $2,000 equity, 0.25% risk budget, 25% position cap,
and available cash. A symbol is not labelled actionable when its spread,
earnings/news filters, setup checks, or current ask fail. This is research
output for review, not an order recommendation or automatic submission.

## Files

| File | Role |
|------|------|
| `config.yaml` | All account / risk / strategy / execution settings |
| `config.py` | Loads the YAML + secrets into typed config |
| `data.py` | Multi-timeframe bars (Alpaca; synthetic in sim) |
| `strategy.py` | Trend-pullback setup detection |
| `risk.py` | Position sizing + account-level risk gates |
| `filters.py` | Price, spread, time-window, earnings, news |
| `broker.py` | Simulation, virtual ledger, and dormant Alpaca broker adapter |
| `state.py` | SQLite: trades, counters, drawdown peak |
| `engine.py` | One cycle wiring it all together |
| `run.py` | Entry point (sim loop / live poll) |
| `backtest.py` | Chronological CSV backtest and performance report |
| `scan.py` | Ranked current-market research for the small account |

## Virtual $2k account (paper)

Alpaca paper accounts default to $100k and can't always be resized. So in
`paper` mode this agent runs a **virtual account** seeded at
`account.starting_equity` (from `config.yaml`) that trades on **real Alpaca
market data** but keeps its own cash + positions ledger — persisted in the
database so it survives redeploys. Sizing, P&L, and drawdown are all measured
against the virtual $2k, completely decoupled from your real Alpaca balance (and
immune to any leftover positions on that account).

It does **not** place orders on the oversized/shared Alpaca account — it fills
its own ledger at real prices and manages stops/targets against the live quote.
Virtual fills are an approximation, not broker paper fills, so use them for
forward observation rather than proof of achievable execution or profitability.
`AGENT_MODE=live` deliberately fails at startup until real-order reconciliation
and broker-level integration tests are implemented.

After switching modes or accounts, call **`POST /api/reset`** once to clear old
data and start the virtual account clean at `starting_equity`.

## Web service & dashboard (runs on Railway)

`app.py` is a FastAPI service with a dashboard and a background scheduler, so the
whole thing runs on Railway with nothing on your machine:

- `GET /` — dashboard (equity curve, positions with stop/target, trades, logs,
  P&L today/week, drawdown, paper-trade progress toward the 100-trade gate).
- `GET /health` — Railway healthcheck.
- `GET /api/status` · `/api/trades` · `/api/logs` · `/api/equity`.
- `POST /api/cycle` — run one cycle (`API_TOKEN` required).

Set `ENABLE_SCHEDULER=true` and the engine ticks every `POLL_SECONDS` during US
market hours automatically.

## Historical backtest

Run the strategy chronologically against completed 15-minute OHLC bars:

```bash
python download_data.py --symbol SPY --start 2021-01-01 --end 2026-01-01 \
  --output data/SPY-15m.csv
python backtest.py data/SPY-15m.csv --symbol SPY --slippage-bps 2 --fee 0 --yearly
```

The CSV needs `timestamp,open,high,low,close`. Signals only see completed bars,
entries occur on the following bar, stop gaps receive the worse opening price,
and ambiguous bars touching both stop and target count as stops. The JSON report
includes return, maximum drawdown, win rate, profit factor, and a trade log.
The downloader uses Alpaca's adjusted historical bars and requires the two
Alpaca environment variables. Use `--feed sip` only if your subscription allows
it; the default is IEX.

### Shared Massive archive

The local 3.4 GB equity archive can be used directly without copying it:

```bash
python massive_research.py --start-year 2021 --end-year 2025 \
  --output massive-research-report.json
python massive_research.py --all-symbols --start-year 2021 --end-year 2025 \
  --output massive-all-symbols.json
```

By default it tests the configured stock/ETF universe that exists in the
archive. Set `MASSIVE_RESEARCH_ROOT` when the archive is stored elsewhere. It
aggregates minute Parquet data to 15-minute bars, runs this strategy with the
$2,000 sizing and cost assumptions, reports each calendar year independently,
and applies only an initial screen—not a live-trading authorization.

Select a small virtual watch universe using 2021–2024 for development and 2025
as a frozen holdout, then apply overlapping signals to one shared $2,000 ledger:

```bash
python candidate_selection.py --symbols SPY QQQ IWM AMD AAPL MSFT NVDA GOOGL META AMZN
```

The current focused universe is `NVDA, GOOGL, SPY`. On the frozen 2025 shared-
account overlay it produced 32 non-overlapping trades, +2.67% return, 1.64
profit factor, and 1.21% maximum drawdown. Re-run selection before changing the
universe; do not add symbols based only on their full-period ranking.

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
