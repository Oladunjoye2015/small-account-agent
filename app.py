"""FastAPI web service for the swing agent — dashboard + API + scheduler.

Runs entirely on Railway: a background scheduler ticks the engine during US
market hours, an HTML dashboard shows equity/positions/trades, and the API
exposes status, trades, logs, and a manual "Run cycle" (token-protected).

  GET  /              -> dashboard
  GET  /health        -> liveness probe
  GET  /api/status    -> equity, P&L, positions, risk counters
  POST /api/cycle     -> run one engine cycle (token)
  GET  /api/trades    -> recent trades
  GET  /api/logs      -> recent logs
  GET  /api/equity    -> equity curve
"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

import config as config_mod
from engine import SwingEngine
from state import State

API_TOKEN = os.getenv("API_TOKEN", "").strip()
ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "false").strip().lower() in ("1", "true", "yes")

_engine: SwingEngine | None = None
_lock = threading.Lock()
_scheduler = None
EASTERN = ZoneInfo("America/New_York")


def get_engine() -> SwingEngine:
    global _engine
    if _engine is None:
        _engine = SwingEngine(config_mod.load(), state=State())
    return _engine


def _market_is_open() -> bool:
    eng = get_engine()
    if eng.cfg.mode == "sim":
        return True
    now_et = datetime.now(timezone.utc).astimezone(EASTERN)
    if now_et.weekday() >= 5:
        return False
    return time(9, 30) <= now_et.time() <= time(16, 0)


def _run_cycle_guarded():
    with _lock:
        return get_engine().run_cycle()


def _scheduled():
    try:
        if _market_is_open():
            _run_cycle_guarded()
    except Exception as exc:  # scheduler must never crash the worker
        print(f"[SCHED] cycle error: {exc!r}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_engine()  # eager init so bad config/creds fail fast
    global _scheduler
    if ENABLE_SCHEDULER:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            interval = max(30, get_engine().cfg.poll_seconds)
            _scheduler = BackgroundScheduler(daemon=True)
            _scheduler.add_job(_scheduled, "interval", seconds=interval)
            _scheduler.start()
            print(f"[APP] scheduler on, every {interval}s during market hours")
        except Exception as exc:
            print(f"[APP] scheduler failed: {exc!r}")
    yield
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)


app = FastAPI(title="Small-Account Swing Researcher", version="2.0.0", lifespan=lifespan)


def require_token(authorization: str = Header(default="")):
    if not API_TOKEN:
        raise HTTPException(status_code=503, detail="API_TOKEN is required for mutation endpoints")
    if authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="invalid or missing API token")


@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/api/status")
def api_status():
    return get_engine().status()


@app.post("/api/cycle", dependencies=[Depends(require_token)])
def api_cycle():
    return _run_cycle_guarded()


@app.post("/api/reset", dependencies=[Depends(require_token)])
def api_reset():
    """Clear all trades/equity/logs and the drawdown peak — a clean restart
    (e.g. after switching from sim to paper, or resetting the paper account)."""
    with _lock:
        get_engine().reset()
    return {"ok": True, "reset": True}


@app.get("/api/trades")
def api_trades(limit: int = 100):
    return JSONResponse(get_engine().state.fetch_trades(max(1, min(limit, 1000))))


@app.get("/api/logs")
def api_logs(limit: int = 100):
    return JSONResponse(get_engine().state.fetch_logs(max(1, min(limit, 1000))))


@app.get("/api/equity")
def api_equity(limit: int = 500):
    return JSONResponse(get_engine().state.fetch_equity(max(1, min(limit, 5000))))


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Swing Researcher</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#0b0f14;--panel:#141b24;--line:#243140;--txt:#e6edf3;--mut:#8b9bb0;
--green:#3fb950;--red:#f85149;--accent:#58a6ff;}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:16px 22px;border-bottom:1px solid var(--line)}
h1{font-size:17px;margin:0;font-weight:600}
.badge{font-size:11px;padding:3px 9px;border-radius:999px;border:1px solid var(--line);
text-transform:uppercase;letter-spacing:.05em;color:var(--mut)}
.badge.paper{color:var(--accent);border-color:var(--accent)}
.badge.live{color:var(--red);border-color:var(--red)}
.badge.halt{color:#000;background:var(--red);border-color:var(--red)}
.spacer{flex:1}#msg{font-size:12px;color:var(--mut)}
button{background:var(--panel);color:var(--txt);border:1px solid var(--line);padding:8px 14px;border-radius:8px;cursor:pointer;font-size:13px}
button:hover{border-color:var(--accent)}button.primary{background:var(--accent);color:#06121f;border-color:var(--accent);font-weight:600}
main{padding:22px;max-width:1080px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:14px;margin-bottom:22px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
.card .k{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
.card .v{font-size:20px;font-weight:600;margin-top:6px}.pos{color:var(--green)}.neg{color:var(--red)}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:22px}
section h2{font-size:13px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin:0 0 12px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:500;font-size:11px;text-transform:uppercase}td.num{text-align:right;font-variant-numeric:tabular-nums}
.muted{color:var(--mut)}.logs{font-family:ui-monospace,Menlo,monospace;font-size:12px;max-height:240px;overflow:auto}
.logs div{padding:2px 0;border-bottom:1px solid #1b2530}.lvl-error{color:var(--red)}.lvl-warn{color:#d29922}
.chip{font-size:11px;padding:2px 8px;border-radius:6px;border:1px solid var(--line)}.b-buy{color:var(--green)}.b-sell{color:var(--red)}
</style></head><body>
<header><h1>Small-Account Swing Researcher</h1>
<span id="modeBadge" class="badge">…</span><span id="haltBadge" class="badge halt" style="display:none">HALTED</span>
<div class="spacer"></div><span id="msg"></span>
<button id="cycleBtn" class="primary">Run cycle</button><button id="tokenBtn" title="Set API token">🔑</button></header>
<main>
<div class="grid">
<div class="card"><div class="k">Equity</div><div class="v" id="equity">—</div></div>
<div class="card"><div class="k">P&amp;L</div><div class="v" id="pnl">—</div></div>
<div class="card"><div class="k">Today</div><div class="v" id="today">—</div></div>
<div class="card"><div class="k">This week</div><div class="v" id="week">—</div></div>
<div class="card"><div class="k">Drawdown</div><div class="v" id="dd">—</div></div>
<div class="card"><div class="k">Trades today</div><div class="v" id="tt">—</div></div>
<div class="card"><div class="k">Paper trades</div><div class="v" id="paper">—</div></div>
</div>
<section><h2>Equity</h2><canvas id="eqChart" height="90"></canvas></section>
<section><h2>Open positions</h2><table><thead><tr><th>Symbol</th><th class="num">Qty</th>
<th class="num">Entry</th><th class="num">Stop</th><th class="num">Target</th><th class="num">Unrealized</th></tr></thead>
<tbody id="posBody"><tr><td colspan="6" class="muted">—</td></tr></tbody></table></section>
<section><h2>Recent trades</h2><table><thead><tr><th>Time</th><th>Symbol</th><th>Side</th>
<th class="num">Qty</th><th class="num">Price</th><th class="num">P&amp;L</th><th>Outcome</th></tr></thead>
<tbody id="tradeBody"><tr><td colspan="7" class="muted">—</td></tr></tbody></table></section>
<section><h2>Log</h2><div class="logs" id="logs"><div class="muted">—</div></div></section>
</main>
<script>
let TOKEN="";const $=id=>document.getElementById(id);
const money=n=>(n==null?"—":"$"+Number(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}));
const cls=n=>n>0?"pos":(n<0?"neg":"");
$("tokenBtn").onclick=()=>{const t=prompt("API token:",TOKEN);if(t!==null){TOKEN=t.trim();flash(TOKEN?"Token set":"Token cleared");}};
function flash(m){$("msg").textContent=m;setTimeout(()=>{if($("msg").textContent===m)$("msg").textContent="";},3000);}
function hdr(){return TOKEN?{"Authorization":"Bearer "+TOKEN}:{};}
async function jget(u){const r=await fetch(u);if(!r.ok)throw 0;return r.json();}
let chart;
function drawEq(pts){const l=pts.map(p=>new Date(p.ts).toLocaleString());const d=pts.map(p=>p.equity);
if(!chart){chart=new Chart($("eqChart"),{type:"line",data:{labels:l,datasets:[{data:d,borderColor:"#58a6ff",
backgroundColor:"rgba(88,166,255,.12)",fill:true,tension:.25,pointRadius:0,borderWidth:2}]},
options:{plugins:{legend:{display:false}},scales:{x:{ticks:{color:"#8b9bb0",maxTicksLimit:8},grid:{color:"#1b2530"}},
y:{ticks:{color:"#8b9bb0"},grid:{color:"#1b2530"}}}}});}else{chart.data.labels=l;chart.data.datasets[0].data=d;chart.update("none");}}
async function refresh(){
try{const s=await jget("/api/status");
const mb=$("modeBadge");mb.textContent=s.mode;mb.className="badge "+(s.mode||"");
$("haltBadge").style.display=(s.note&&s.note.indexOf("drawdown")>=0)?"":"none";
$("equity").textContent=money(s.equity);
const p=$("pnl");p.textContent=money(s.pnl);p.className="v "+cls(s.pnl);
const td=$("today");td.textContent=money(s.pl_today);td.className="v "+cls(s.pl_today);
const wk=$("week");wk.textContent=money(s.pl_week);wk.className="v "+cls(s.pl_week);
$("dd").textContent="-"+(s.drawdown_pct||0)+"%";
$("tt").textContent=(s.trades_today??"—")+" / "+2;
$("paper").textContent=(s.paper_trades_done??0)+" / "+(s.paper_minimum_trades??100);
const pb=$("posBody");const pos=s.open_positions_detail||[];
pb.innerHTML=pos.length?pos.map(x=>`<tr><td>${x.symbol}</td><td class="num">${x.qty}</td>
<td class="num">${money(x.avg_entry_price)}</td><td class="num">${money(x.stop)}</td>
<td class="num">${money(x.target)}</td><td class="num ${cls(x.unrealized_pl)}">${money(x.unrealized_pl)}</td></tr>`).join("")
:'<tr><td colspan="6" class="muted">No open positions</td></tr>';
}catch(e){flash("status error");}
try{drawEq(await jget("/api/equity?limit=500"));}catch(e){}
try{const t=await jget("/api/trades?limit=50");const tb=$("tradeBody");
tb.innerHTML=t.length?t.map(x=>`<tr><td class="muted">${new Date(x.ts).toLocaleString()}</td><td>${x.symbol}</td>
<td class="b-${x.side}">${(x.side||"").toUpperCase()}</td><td class="num">${x.qty}</td>
<td class="num">${money(x.exit||x.entry)}</td><td class="num ${cls(x.realized_pl)}">${x.side==="sell"?money(x.realized_pl):"—"}</td>
<td><span class="chip">${x.outcome||x.mode||""}</span></td></tr>`).join("")
:'<tr><td colspan="7" class="muted">No trades yet</td></tr>';}catch(e){}
try{const l=await jget("/api/logs?limit=80");$("logs").innerHTML=l.length?l.map(x=>`<div class="lvl-${x.level}">
<span class="muted">${new Date(x.ts).toLocaleTimeString()}</span> ${x.message}</div>`).join(""):'<div class="muted">No logs</div>';}catch(e){}
}
$("cycleBtn").onclick=async()=>{const b=$("cycleBtn");b.disabled=true;b.textContent="Running…";
try{const r=await fetch("/api/cycle",{method:"POST",headers:hdr()});
if(r.status===401)flash("Unauthorized — set token (🔑)");else{await r.json();flash("cycle ok");await refresh();}}
catch(e){flash("failed");}b.disabled=false;b.textContent="Run cycle";};
refresh();setInterval(refresh,15000);
</script></body></html>"""
