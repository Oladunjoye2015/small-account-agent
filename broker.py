"""Broker layer: one interface, a local simulator, and Alpaca.

Designed around **bracket orders** — an entry paired with a server-side
stop-loss and take-profit (OCO). That matters for a swing agent that holds
overnight: the stop lives at the broker, so it protects you even if the bot,
Railway, or your laptop is down. That's a big safety upgrade over polling-based
stops.

Whole shares only. Alpaca allows fractional shares *only* for plain market
orders, not for bracket/stop/limit orders — so to get real server-side stop
protection we trade whole shares. On a $2k account with $10+ names and a 25%
position cap that's fine (a few to a few dozen shares).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class Account:
    equity: float
    cash: float
    buying_power: float


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry_price: float
    stop: float
    target: float
    market_value: float = 0.0
    unrealized_pl: float = 0.0


@dataclass
class Quote:
    bid: float
    ask: float
    last: float

    @property
    def spread_pct(self) -> float:
        mid = (self.bid + self.ask) / 2 if (self.bid and self.ask) else self.last
        return (self.ask - self.bid) / mid if mid else 1.0


@dataclass
class Fill:
    symbol: str
    qty: float
    price: float
    side: str          # "buy" | "sell"
    realized_pl: float = 0.0
    outcome: str = ""   # "", "target", "stop", "manual", "eod"


class Broker(ABC):
    @abstractmethod
    def get_account(self) -> Account: ...
    @abstractmethod
    def get_positions(self) -> list[Position]: ...
    @abstractmethod
    def get_quote(self, symbol: str) -> Quote: ...
    @abstractmethod
    def submit_bracket(self, symbol: str, qty: float, entry: float,
                       stop: float, target: float) -> str: ...
    @abstractmethod
    def close_position(self, symbol: str) -> Fill | None: ...

    def get_latest_price(self, symbol: str) -> float:
        return self.get_quote(symbol).last

    # Optional hooks (no-ops by default).
    def cancel_stale_entries(self, max_minutes: int) -> int: return 0
    def positions_missing_stop(self) -> list[str]: return []
    def has_open_entry(self, symbol: str) -> bool: return False


# --------------------------------------------------------------------------- #
# Local simulator: prices are injected each cycle; it manages brackets itself. #
# --------------------------------------------------------------------------- #
class SimBroker(Broker):
    def __init__(self, universe: list[str], starting_cash: float = 2000.0, seed: int = 7):
        import random
        self._rng = random.Random(seed)
        self._cash = starting_cash
        self._positions: dict[str, Position] = {}
        self._prices = {s: round(self._rng.uniform(20, 120), 2) for s in universe}

    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price

    def tick(self) -> None:
        for s, p in self._prices.items():
            self._prices[s] = round(max(1.0, p * (1 + self._rng.gauss(0, 0.006))), 2)

    def get_quote(self, symbol: str) -> Quote:
        p = self._prices[symbol]
        return Quote(bid=round(p * 0.9995, 2), ask=round(p * 1.0005, 2), last=p)

    def get_account(self) -> Account:
        pv = sum(pos.qty * self._prices[pos.symbol] for pos in self._positions.values())
        eq = self._cash + pv
        return Account(equity=eq, cash=self._cash, buying_power=self._cash)

    def get_positions(self) -> list[Position]:
        out = []
        for sym, pos in self._positions.items():
            price = self._prices[sym]
            pos.market_value = round(pos.qty * price, 2)
            pos.unrealized_pl = round((price - pos.avg_entry_price) * pos.qty, 2)
            out.append(pos)
        return out

    def submit_bracket(self, symbol, qty, entry, stop, target) -> str:
        # Sim fills the entry immediately at the entry price.
        cost = qty * entry
        if cost > self._cash or qty <= 0:
            return ""
        self._cash -= cost
        self._positions[symbol] = Position(symbol, qty, entry, stop, target)
        return f"sim-{symbol}-{int(datetime.now(timezone.utc).timestamp())}"

    def manage(self) -> list[Fill]:
        """Check every open position's bracket against the current price and
        close any that hit their stop or target. Call once per cycle."""
        fills = []
        for sym in list(self._positions):
            pos = self._positions[sym]
            price = self._prices[sym]
            hit = None
            if price <= pos.stop:
                hit, fill_px = "stop", pos.stop
            elif price >= pos.target:
                hit, fill_px = "target", pos.target
            if hit:
                fills.append(self._close(sym, fill_px, hit))
        return fills

    def _close(self, symbol, price, outcome) -> Fill:
        pos = self._positions.pop(symbol)
        self._cash += pos.qty * price
        pl = round((price - pos.avg_entry_price) * pos.qty, 2)
        return Fill(symbol, pos.qty, price, "sell", realized_pl=pl, outcome=outcome)

    def close_position(self, symbol) -> Fill | None:
        if symbol not in self._positions:
            return None
        return self._close(symbol, self._prices[symbol], "manual")


# --------------------------------------------------------------------------- #
# Virtual account: REAL Alpaca market data, but its own $2k ledger.            #
# Decouples the strategy from an oversized/shared Alpaca paper balance — the   #
# agent trades a self-contained small account on live prices. Ledger (cash +   #
# positions) is persisted in State so it survives redeploys.                   #
# --------------------------------------------------------------------------- #
class VirtualBroker(Broker):
    def __init__(self, cfg, state, quote_fn=None):
        self.cfg = cfg
        self.state = state
        self._quote_fn = quote_fn        # injectable for tests; else Alpaca data
        if quote_fn is None:
            from alpaca.data.historical import StockHistoricalDataClient
            self._data = StockHistoricalDataClient(cfg.alpaca_key, cfg.alpaca_secret)
        raw = state.get_meta("vcash")
        self._cash = float(raw) if raw is not None else float(cfg.account.starting_equity)
        self._positions: dict[str, Position] = {}
        for p in state.load_positions():
            self._positions[p["symbol"]] = Position(
                p["symbol"], p["qty"], p["entry"], p["stop"], p["target"])
        self._pcache: dict[str, float] = {}

    def _quote(self, symbol) -> Quote:
        if self._quote_fn is not None:
            return self._quote_fn(symbol)
        from alpaca.data.requests import StockLatestQuoteRequest
        q = self._data.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=symbol))[symbol]
        bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
        last = (bid + ask) / 2 if (bid and ask) else (bid or ask)
        return Quote(bid, ask, last)

    def _price(self, symbol) -> float:
        if symbol not in self._pcache:
            self._pcache[symbol] = self._quote(symbol).last
        return self._pcache[symbol]

    def get_quote(self, symbol) -> Quote:
        qt = self._quote(symbol)
        self._pcache[symbol] = qt.last
        return qt

    def get_account(self) -> Account:
        self._pcache = {}   # refresh prices once per cycle (get_account is first)
        mv = sum(p.qty * self._price(s) for s, p in self._positions.items())
        eq = self._cash + mv
        return Account(round(eq, 2), round(self._cash, 2), round(self._cash, 2))

    def get_positions(self) -> list[Position]:
        out = []
        for s, p in self._positions.items():
            price = self._price(s)
            p.market_value = round(p.qty * price, 2)
            p.unrealized_pl = round((price - p.avg_entry_price) * p.qty, 2)
            out.append(p)
        return out

    def submit_bracket(self, symbol, qty, entry, stop, target) -> str:
        cost = qty * entry
        if qty <= 0 or cost > self._cash:
            return ""
        self._cash -= cost
        self._positions[symbol] = Position(symbol, qty, entry, stop, target)
        self.state.set_meta("vcash", self._cash)
        self.state.save_position(symbol, qty, entry, stop, target)
        return f"virt-{symbol}"

    def manage(self) -> list[Fill]:
        fills = []
        for s in list(self._positions):
            p = self._positions[s]
            price = self._price(s)
            hit = None
            if price <= p.stop:
                hit, fx = "stop", p.stop
            elif price >= p.target:
                hit, fx = "target", p.target
            if hit:
                fills.append(self._close(s, fx, hit))
        return fills

    def _close(self, symbol, price, outcome) -> Fill:
        p = self._positions.pop(symbol)
        self._cash += p.qty * price
        pl = round((price - p.avg_entry_price) * p.qty, 2)
        self.state.set_meta("vcash", self._cash)
        self.state.delete_position(symbol)
        return Fill(symbol, p.qty, price, "sell", realized_pl=pl, outcome=outcome)

    def close_position(self, symbol) -> Fill | None:
        if symbol not in self._positions:
            return None
        return self._close(symbol, self._price(symbol), "manual")


# --------------------------------------------------------------------------- #
# Alpaca: real bracket orders with server-side stops.                         #
# --------------------------------------------------------------------------- #
class AlpacaBroker(Broker):
    def __init__(self, api_key: str, secret: str, paper: bool = True):
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient
        self._trading = TradingClient(api_key, secret, paper=paper)
        self._data = StockHistoricalDataClient(api_key, secret)

    def get_account(self) -> Account:
        a = self._trading.get_account()
        return Account(float(a.equity), float(a.cash), float(a.buying_power))

    def get_positions(self) -> list[Position]:
        out = []
        for p in self._trading.get_all_positions():
            out.append(Position(
                symbol=p.symbol, qty=float(p.qty), avg_entry_price=float(p.avg_entry_price),
                stop=0.0, target=0.0, market_value=float(p.market_value),
                unrealized_pl=float(p.unrealized_pl),
            ))
        return out

    def get_quote(self, symbol: str) -> Quote:
        from alpaca.data.requests import StockLatestQuoteRequest
        q = self._data.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))[symbol]
        bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
        last = (bid + ask) / 2 if (bid and ask) else (bid or ask)
        return Quote(bid=bid, ask=ask, last=last)

    def submit_bracket(self, symbol, qty, entry, stop, target) -> str:
        from alpaca.trading.requests import (LimitOrderRequest, TakeProfitRequest,
                                             StopLossRequest)
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
        req = LimitOrderRequest(
            symbol=symbol, qty=int(qty), side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            limit_price=round(entry, 2), order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(target, 2)),
            stop_loss=StopLossRequest(stop_price=round(stop, 2)),
        )
        order = self._trading.submit_order(req)
        return str(order.id)

    def close_position(self, symbol) -> Fill | None:
        try:
            price = self.get_latest_price(symbol)
        except Exception:
            price = 0.0
        try:
            self._trading.close_position(symbol)
        except Exception:
            return None
        return Fill(symbol, 0.0, price, "sell", outcome="manual")

    # --- execution hygiene ------------------------------------------------ #
    def cancel_stale_entries(self, max_minutes: int) -> int:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus, OrderSide
        cancelled = 0
        now = datetime.now(timezone.utc)
        try:
            orders = self._trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
        except Exception:
            return 0
        for o in orders:
            if o.side == OrderSide.BUY and o.filled_qty in (None, 0, "0"):
                age = (now - o.submitted_at).total_seconds() / 60 if o.submitted_at else 0
                if age >= max_minutes:
                    try:
                        self._trading.cancel_order_by_id(o.id)
                        cancelled += 1
                    except Exception:
                        pass
        return cancelled

    def positions_missing_stop(self) -> list[str]:
        """Symbols we hold that have no open stop order (bracket leg missing)."""
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        held = {p.symbol for p in self.get_positions()}
        if not held:
            return []
        try:
            orders = self._trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
        except Exception:
            return []
        protected = {o.symbol for o in orders if getattr(o, "stop_price", None)}
        return sorted(held - protected)

    def has_open_entry(self, symbol: str) -> bool:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus, OrderSide
        try:
            orders = self._trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
        except Exception:
            return False
        return any(o.symbol == symbol and o.side == OrderSide.BUY for o in orders)
