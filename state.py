"""Persistent state (SQLite, stdlib only).

Stores completed trades and a small key/value table, and derives the counters
the risk manager needs (trades today, daily/weekly realized P&L, consecutive
losses, peak equity for drawdown). SQLite keeps this dependency-free and easy to
test; swap for Postgres later if you want history to survive Railway redeploys.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone


def _utcnow():
    return datetime.now(timezone.utc)


class State:
    def __init__(self, path: str = "swing_agent.db"):
        self.path = os.getenv("STATE_DB", path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        c = self.conn
        c.execute("""CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, symbol TEXT, side TEXT, qty REAL,
            entry REAL, exit REAL, realized_pl REAL, outcome TEXT, mode TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, message TEXT)""")
        c.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
        c.execute("""CREATE TABLE IF NOT EXISTS equity(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, equity REAL, cash REAL)""")
        c.commit()

    # --- writes ---------------------------------------------------------- #
    def record_trade(self, symbol, side, qty, entry, exit_, realized_pl, outcome, mode):
        self.conn.execute(
            "INSERT INTO trades(ts,symbol,side,qty,entry,exit,realized_pl,outcome,mode)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (_utcnow().isoformat(), symbol, side, qty, entry, exit_, realized_pl, outcome, mode))
        self.conn.commit()

    def record_equity(self, equity, cash):
        self.conn.execute("INSERT INTO equity(ts,equity,cash) VALUES(?,?,?)",
                          (_utcnow().isoformat(), equity, cash))
        self.conn.commit()

    def log(self, message, level="info"):
        self.conn.execute("INSERT INTO logs(ts,level,message) VALUES(?,?,?)",
                          (_utcnow().isoformat(), level, message))
        self.conn.commit()

    def set_meta(self, key, value):
        self.conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, str(value)))
        self.conn.commit()

    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # --- derived counters ------------------------------------------------ #
    def _closed(self):
        return self.conn.execute(
            "SELECT ts, realized_pl, mode FROM trades WHERE side='sell' ORDER BY id").fetchall()

    def total_trades(self, mode=None) -> int:
        q = "SELECT COUNT(*) n FROM trades WHERE side='sell'"
        args = ()
        if mode:
            q += " AND mode=?"; args = (mode,)
        return int(self.conn.execute(q, args).fetchone()["n"])

    def trades_today(self, now=None) -> int:
        now = now or _utcnow()
        day = now.date().isoformat()
        return int(self.conn.execute(
            "SELECT COUNT(*) n FROM trades WHERE side='buy' AND substr(ts,1,10)=?",
            (day,)).fetchone()["n"])

    def realized_pl_since(self, since: datetime) -> float:
        rows = self.conn.execute(
            "SELECT COALESCE(SUM(realized_pl),0) s FROM trades WHERE side='sell' AND ts>=?",
            (since.isoformat(),)).fetchone()
        return float(rows["s"] or 0.0)

    def pl_today(self, now=None) -> float:
        now = now or _utcnow()
        start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        return self.realized_pl_since(start)

    def pl_this_week(self, now=None) -> float:
        now = now or _utcnow()
        start = now - timedelta(days=now.weekday())
        start = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        return self.realized_pl_since(start)

    def consecutive_losses(self) -> int:
        rows = self.conn.execute(
            "SELECT realized_pl FROM trades WHERE side='sell' ORDER BY id DESC LIMIT 20").fetchall()
        n = 0
        for r in rows:
            if (r["realized_pl"] or 0) < 0:
                n += 1
            else:
                break
        return n

    # --- reads for the dashboard ----------------------------------------- #
    def fetch_trades(self, limit=100):
        rows = self.conn.execute(
            "SELECT ts,symbol,side,qty,entry,exit,realized_pl,outcome,mode "
            "FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def fetch_logs(self, limit=100):
        rows = self.conn.execute(
            "SELECT ts,level,message FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def fetch_equity(self, limit=500):
        rows = self.conn.execute(
            "SELECT ts,equity,cash FROM equity ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def peak_equity(self, current: float) -> float:
        raw = self.get_meta("peak_equity")
        prev = float(raw) if raw is not None else current
        peak = max(prev, current)
        self.set_meta("peak_equity", peak)   # always persist the high-water mark
        return peak
