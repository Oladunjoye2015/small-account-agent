"""Entry point.

    python run.py                # uses config.yaml (mode: sim by default)
    AGENT_MODE=paper python run.py

In sim mode it runs a bounded, fast loop so you can watch the whole pipeline
offline. In paper/live it loops on poll_seconds; the engine itself enforces the
trading window, so off-hours cycles are cheap no-ops.
"""
from __future__ import annotations

import time

import config as config_mod
from broker import SimBroker
from engine import SwingEngine


def main():
    cfg = config_mod.load()
    engine = SwingEngine(cfg)

    if cfg.mode == "sim":
        # Fast, bounded run; advance synthetic prices between cycles.
        for i in range(300):
            if isinstance(engine.broker, SimBroker):
                engine.broker.tick()
            status = engine.run_cycle()
            if i % 25 == 0 or status["events"]:
                print(f"[{i:3d}] equity={status['equity']} "
                      f"pos={status['open_positions']} today={status['trades_today']} "
                      f"pl_today={status['pl_today']} {status['events']}")
        print("FINAL:", engine.broker.get_account().equity,
              "| total trades:", engine.state.total_trades())
        return

    while True:
        try:
            engine.run_cycle()
        except Exception as exc:  # never let one cycle kill the agent
            print(f"[RUN] cycle error: {exc!r}")
        time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    main()
