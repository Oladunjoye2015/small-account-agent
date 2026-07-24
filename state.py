"""Persistent state — Postgres when available, SQLite otherwise.

If ``DATABASE_URL`` is set (Railway Postgres), the agent stores everything in
Postgres — durable across redeploys and browsable in Railway's Data tab. With no
``DATABASE_URL`` it falls back to a local SQLite file (great for sim / local dev,
but ephemeral on Railway unless you mount a volume).

Same tables and API either way; timestamps are stored as ISO **text** so the
date/prefix queries work identically on both engines.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone


def _utcnow():
    return datetime.now(timezone.utc)


def _pg_url() -> str | None:
    url = os.getenv("DATABASE_URL", "").strip()
    return url or None


class State:
    def __init__(self, path: str = "swing_agent.db"):
        self.pg_url = _pg_url()
        if self.pg_url:
            import psycopg
            from psycopg.rows import dict_row
            self.conn = psycopg.connect(self.pg_url, row_factory=dict_row)
            self.ph = "%s"
            self._autoinc = "SERIAL PRIMARY KEY"
            self._num = "double precision"
            self.backend = "postgres"
        else:
            self.path = os.getenv("STATE_DB", path)
            self.conn = sqlite3.connect(self.path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.ph = "?"
            self._autoinc = "INTEGER PRIMARY KEY AUTOINCREMENT"
            self._num = "real"
            self.backend = "sqlite"
        self._init()

    # --- low-level helpers ----------------------------------------------- #
    def _q(self, sql: str) -> str:
        return sql if self.ph == "?" else sql.replace("?", self.ph)

    def _exec(self, sql: str, params=()):
        cur = self.conn.execute(self._q(sql), params)
        return cur

    def _init(self):
        n, ai = self._num, self._autoinc
        self._exec(f"""CREATE TABLE IF NOT EXISTS trades(
            id {ai}, ts TEXT, symbol TEXT, side TEXT, qty {n},
            entry {n}, exit {n}, realized_pl {n}, outcome TEXT, mode TEXT)""")
        self._exec(f"""CREATE TABLE IF NOT EXISTS logs(
            id {ai}, ts TEXT, level TEXT, message TEXT)""")
        self._exec("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
        self._exec(f"""CREATE TABLE IF NOT EXISTS equity(
            id {ai}, ts TEXT, equity {n}, cash {n})""")
        self._exec(f"""CREATE TABLE IF NOT EXISTS positions(
            symbol TEXT PRIMARY KEY, qty {n}, entry {n}, stop {n}, target {n})""")
        self.conn.commit()

    # --- virtual-account ledger (persisted so it survives redeploys) ------ #
    def save_position(self, symbol, qty, entry, stop, target):
        self._exec("INSERT INTO positions(symbol,qty,entry,stop,target) VALUES(?,?,?,?,?) "
                   "ON CONFLICT(symbol) DO UPDATE SET qty=EXCLUDED.qty, entry=EXCLUDED.entry, "
                   "stop=EXCLUDED.stop, target=EXCLUDED.target",
                   (symbol, qty, entry, stop, target))
        self.conn.commit()

    def delete_position(self, symbol):
        self._exec("DELETE FROM positions WHERE symbol=?", (symbol,))
        self.conn.commit()

    def load_positions(self):
        rows = self._exec("SELECT symbol,qty,entry,stop,target FROM positions").fetchall()
        return [dict(r) for r in rows]

    # --- writes ---------------------------------------------------------- #
    def record_trade(self, symbol, side, qty, entry, exit_, realized_pl, outcome, mode):
        self._exec(
            "INSERT INTO trades(ts,symbol,side,qty,entry,exit,realized_pl,outcome,mode)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (_utcnow().isoformat(), symbol, side, qty, entry, exit_, realized_pl, outcome, mode))
        self.conn.commit()

    def record_equity(self, equity, cash):
        self._exec("INSERT INTO equity(ts,equity,cash) VALUES(?,?,?)",
                   (_utcnow().isoformat(), equity, cash))
        self.conn.commit()

    def log(self, message, level="info"):
        self._exec("INSERT INTO logs(ts,level,message) VALUES(?,?,?)",
                   (_utcnow().isoformat(), level, message))
        self.conn.commit()

    def set_meta(self, key, value):
        self._exec("INSERT INTO meta(key,value) VALUES(?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", (key, str(value)))
        self.conn.commit()

    def get_meta(self, key, default=None):
        row = self._exec("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # --- derived counters ------------------------------------------------ #
    def total_trades(self, mode=None) -> int:
        if mode:
            row = self._exec(
                "SELECT COUNT(*) AS n FROM trades WHERE side='sell' AND mode=?", (mode,)).fetchone()
        else:
            row = self._exec("SELECT COUNT(*) AS n FROM trades WHERE side='sell'").fetchone()
        return int(row["n"])

    def trades_today(self, now=None) -> int:
        now = now or _utcnow()
        row = self._exec(
            "SELECT COUNT(*) AS n FROM trades WHERE side='buy' AND substr(ts,1,10)=?",
            (now.date().isoformat(),)).fetchone()
        return int(row["n"])

    def realized_pl_since(self, since: datetime) -> float:
        row = self._exec(
            "SELECT COALESCE(SUM(realized_pl),0) AS s FROM trades WHERE side='sell' AND ts>=?",
            (since.isoformat(),)).fetchone()
        return float(row["s"] or 0.0)

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
        rows = self._exec(
            "SELECT realized_pl FROM trades WHERE side='sell' ORDER BY id DESC LIMIT 20").fetchall()
        n = 0
        for r in rows:
            if (r["realized_pl"] or 0) < 0:
                n += 1
            else:
                break
        return n

    def peak_equity(self, current: float) -> float:
        raw = self.get_meta("peak_equity")
        prev = float(raw) if raw is not None else current
        peak = max(prev, current)
        self.set_meta("peak_equity", peak)
        return peak

    def reset(self):
        """Wipe trades, equity snapshots, logs, and the drawdown peak — for a
        clean restart when switching modes/accounts."""
        for tbl in ("trades", "equity", "logs", "positions"):
            self._exec(f"DELETE FROM {tbl}")
        self._exec("DELETE FROM meta WHERE key IN (?,?)", ("peak_equity", "vcash"))
        self.conn.commit()

    def realized_total(self) -> float:
        row = self._exec(
            "SELECT COALESCE(SUM(realized_pl),0) AS s FROM trades WHERE side='sell'").fetchone()
        return float(row["s"] or 0.0)

    # --- reads for the dashboard ----------------------------------------- #
    def fetch_trades(self, limit=100):
        rows = self._exec(
            "SELECT ts,symbol,side,qty,entry,exit,realized_pl,outcome,mode "
            "FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def fetch_logs(self, limit=100):
        rows = self._exec(
            "SELECT ts,level,message FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def fetch_equity(self, limit=500):
        rows = self._exec(
            "SELECT ts,equity,cash FROM equity ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]
