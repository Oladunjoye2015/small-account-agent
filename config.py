"""Configuration loader for the small-account swing agent.

Reads config.yaml into typed dataclasses and pulls secrets (Alpaca / Finnhub
keys) from the environment. Percentages in the YAML are PERCENT units; this
module also exposes ``*_frac`` helpers that return them as fractions (25 -> 0.25)
so the trading code never has to remember to divide by 100.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields


@dataclass
class AccountConfig:
    starting_equity: float = 2000.0
    use_margin: bool = False
    allow_shorts: bool = False
    allow_options: bool = False
    allow_crypto: bool = False


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.25
    max_position_pct: float = 25.0
    max_open_positions: int = 1
    max_trades_per_day: int = 2
    daily_loss_limit_pct: float = 0.75
    weekly_loss_limit_pct: float = 2.0
    max_drawdown_pct: float = 6.0
    consecutive_loss_limit: int = 3
    minimum_reward_risk: float = 1.5
    averaging_down: bool = False

    # Fraction helpers (percent -> fraction).
    @property
    def risk_per_trade_frac(self) -> float: return self.risk_per_trade_pct / 100.0
    @property
    def max_position_frac(self) -> float: return self.max_position_pct / 100.0
    @property
    def daily_loss_limit_frac(self) -> float: return self.daily_loss_limit_pct / 100.0
    @property
    def weekly_loss_limit_frac(self) -> float: return self.weekly_loss_limit_pct / 100.0
    @property
    def max_drawdown_frac(self) -> float: return self.max_drawdown_pct / 100.0


@dataclass
class StrategyConfig:
    timeframe: str = "1Hour"
    confirmation_timeframe: str = "15Min"
    direction: str = "long_only"
    setup: str = "trend_pullback"
    minimum_price: float = 10.0
    regular_hours_only: bool = True
    block_first_minutes: int = 30
    block_new_entries_after: str = "15:00"
    earnings_filter: bool = True
    news_filter: bool = True
    spread_filter: bool = True


@dataclass
class ExecutionConfig:
    order_type: str = "bracket"
    require_stop_loss: bool = True
    use_limit_entries: bool = True
    cancel_stale_entry_minutes: int = 5
    reject_duplicate_orders: bool = True
    close_if_stop_missing: bool = True


@dataclass
class DeploymentConfig:
    initial_mode: str = "paper"
    paper_minimum_trades: int = 100
    first_live_trade_risk_dollars: float = 2.0
    normal_trade_risk_dollars: float = 5.0


@dataclass
class Config:
    account: AccountConfig = field(default_factory=AccountConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    deployment: DeploymentConfig = field(default_factory=DeploymentConfig)
    universe: list = field(default_factory=lambda: ["AAPL", "MSFT", "NVDA"])
    mode: str = "sim"
    poll_seconds: int = 300

    # Secrets from env (never persisted to yaml).
    alpaca_key: str = ""
    alpaca_secret: str = ""
    finnhub_api_key: str = ""


def _apply(dc, data: dict):
    """Copy known keys from a dict onto a dataclass instance (ignores extras)."""
    names = {f.name for f in fields(dc)}
    for k, v in (data or {}).items():
        if k in names:
            setattr(dc, k, v)
    return dc


def load(path: str = "config.yaml") -> Config:
    # Load .env if present (no-op on Railway).
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    raw = {}
    try:
        import yaml
        with open(path) as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        pass  # fall back to defaults
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"failed to parse {path}: {exc!r}")

    cfg = Config()
    _apply(cfg.account, raw.get("account", {}))
    _apply(cfg.risk, raw.get("risk", {}))
    _apply(cfg.strategy, raw.get("strategy", {}))
    _apply(cfg.execution, raw.get("execution", {}))
    _apply(cfg.deployment, raw.get("deployment", {}))
    if raw.get("universe"):
        cfg.universe = [str(s).upper() for s in raw["universe"]]
    cfg.mode = os.getenv("AGENT_MODE", raw.get("mode", cfg.mode)).strip().lower()
    cfg.poll_seconds = int(os.getenv("POLL_SECONDS", raw.get("poll_seconds", cfg.poll_seconds)))

    cfg.alpaca_key = os.getenv("ALPACA_API_KEY", "").strip()
    cfg.alpaca_secret = os.getenv("ALPACA_SECRET_KEY", "").strip()
    cfg.finnhub_api_key = os.getenv("FINNHUB_API_KEY", "").strip()

    if cfg.mode in ("paper", "live") and not (cfg.alpaca_key and cfg.alpaca_secret):
        raise RuntimeError(
            f"mode={cfg.mode} needs ALPACA_API_KEY and ALPACA_SECRET_KEY. "
            "Set them in .env / Railway, or use mode=sim."
        )
    return cfg
