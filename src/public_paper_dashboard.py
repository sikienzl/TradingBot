"""Read-only, authenticated dashboard for an isolated paper-trading run."""

from __future__ import annotations

import csv
import hmac
import ipaddress
import json
import logging
import math
import os
import threading
import time
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("public_paper_dashboard")
security = HTTPBasic(auto_error=False)


class DashboardConfigurationError(RuntimeError):
    """Raised when the private dashboard configuration is incomplete."""


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise DashboardConfigurationError(f"{name} must be configured")
    return value


def _allowed_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    raw = _required_env("DASHBOARD_ALLOWED_NETWORKS")
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    try:
        for item in raw.split(","):
            network = ipaddress.ip_network(item.strip(), strict=False)
            if not (network.is_private or network.is_loopback):
                raise DashboardConfigurationError(
                    "DASHBOARD_ALLOWED_NETWORKS only accepts private or loopback CIDRs"
                )
            networks.append(network)
    except ValueError as exc:
        raise DashboardConfigurationError(
            "DASHBOARD_ALLOWED_NETWORKS must be a comma-separated CIDR list"
        ) from exc
    if not networks:
        raise DashboardConfigurationError("DASHBOARD_ALLOWED_NETWORKS is empty")
    return tuple(networks)


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.error("Unable to read dashboard %s: %s", label, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Paper {label} is temporarily unavailable.",
        ) from exc
    if not isinstance(value, dict):
        logger.error("Dashboard %s has an invalid JSON shape", label)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Paper {label} is temporarily unavailable.",
        )
    return value


def _load_journal(path: Path) -> list[dict[str, str]]:
    try:
        if path.stat().st_size > 100 * 1024 * 1024:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Paper journal exceeds the dashboard read limit.",
            )
        with path.open(encoding="utf-8", newline="") as journal:
            reader = csv.DictReader(journal)
            if not {"timestamp", "action"}.issubset(reader.fieldnames or []):
                raise ValueError("journal is missing required columns")
            return [
                row for row in reader
                if row.get("timestamp") and row.get("action")
            ]
    except HTTPException:
        raise
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        logger.error("Unable to read paper journal: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Paper journal is temporarily unavailable.",
        ) from exc


def _net_value(rows: list[dict[str, str]]) -> float | None:
    sells = [row for row in rows if row.get("action", "").lower() == "sell"]
    if not sells:
        return 0.0
    values = [
        _number(row.get("net_pnl_base"))
        if row.get("execution_cost_mode") == "net_costs"
        and row.get("entry_cost_mode") == "net_costs"
        else None
        for row in sells
    ]
    if any(value is None for value in values):
        return None
    return round(sum(value for value in values if value is not None), 8)


def _field_total(rows: list[dict[str, str]], field: str) -> float | None:
    if not rows:
        return 0.0
    values = [_number(row.get(field)) for row in rows]
    if any(value is None for value in values):
        return None
    return round(sum(value for value in values if value is not None), 8)


def _read_log_counts(path: Path, start: datetime) -> dict[str, int | None]:
    counts: Counter[str] = Counter()
    try:
        if not path.exists():
            return {"atr_guard": None, "tabular_sell_veto": None, "hailo_failures": None}
        if path.stat().st_size > 100 * 1024 * 1024:
            raise OSError("log file exceeds the dashboard read limit")
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            timestamp_text = line[:23]
            try:
                timestamp = datetime.strptime(
                    timestamp_text, "%Y-%m-%d %H:%M:%S,%f"
                ).replace(tzinfo=UTC)
            except ValueError:
                continue
            if timestamp < start:
                continue
            if "ATR(14)/price volatility proxy" in line and "Entry withheld" in line:
                counts["atr_guard"] += 1
            if "Confident tabular SELL vetoed new entry" in line:
                counts["tabular_sell_veto"] += 1
            if "Hailo prediction for" in line and "failed; suppressing" in line:
                counts["hailo_failures"] += 1
    except (OSError, UnicodeError) as exc:
        logger.error("Unable to read paper bot log: %s", exc)
        return {"atr_guard": None, "tabular_sell_veto": None, "hailo_failures": None}
    return {
        "atr_guard": counts["atr_guard"],
        "tabular_sell_veto": counts["tabular_sell_veto"],
        "hailo_failures": counts["hailo_failures"],
    }


def load_dashboard_data() -> dict[str, Any]:
    data_dir = Path(os.getenv("DASHBOARD_DATA_DIR", "/data"))
    report_file = Path(os.getenv("DASHBOARD_REPORT_FILE", "/reports/latest.json"))
    journal_path = data_dir / "trade_journal.csv"
    state_path = data_dir / ".portfolio_state.json"
    log_path = Path(os.getenv("DASHBOARD_LOG_FILE", str(data_dir / "trading_bot.log")))

    state = _load_json(state_path, "portfolio state")
    rows = _load_journal(journal_path)
    now = datetime.now(UTC)
    today = now.date()
    since_7d = now - timedelta(days=7)
    parsed_rows: list[tuple[datetime, dict[str, str]]] = []
    for row in rows:
        timestamp = _parse_utc(row.get("timestamp"))
        if timestamp:
            parsed_rows.append((timestamp, row))
    parsed_rows.sort(key=lambda pair: pair[0])

    today_rows = [row for stamp, row in parsed_rows if stamp.date() == today]
    week_rows = [row for stamp, row in parsed_rows if stamp >= since_7d]
    all_sells = [row for row in rows if row.get("action", "").lower() == "sell"]
    today_sells = [row for row in today_rows if row.get("action", "").lower() == "sell"]
    week_sells = [row for row in week_rows if row.get("action", "").lower() == "sell"]
    daily: dict[date, list[dict[str, str]]] = defaultdict(list)
    for stamp, row in parsed_rows:
        if stamp >= now - timedelta(days=13):
            daily[stamp.date()].append(row)
    chart = [
        {
            "date": (today - timedelta(days=13 - offset)).isoformat(),
            "net_pnl": _net_value(daily.get(today - timedelta(days=13 - offset), [])),
        }
        for offset in range(14)
    ]

    open_trades = state.get("open_trades")
    if not isinstance(open_trades, dict):
        open_trades = {}
    positions = []
    for coin, trade in open_trades.items():
        if not isinstance(trade, dict):
            continue
        positions.append({
            "coin": str(coin),
            "amount_coin": _number(trade.get("amount_coin")),
            "amount_base": _number(trade.get("amount_base")),
            "buy_price": _number(trade.get("buy_price")),
            "timestamp": str(trade.get("timestamp") or ""),
        })

    holdings = state.get("holdings")
    if not isinstance(holdings, dict):
        holdings = {}
    safe_holdings = [
        {"coin": str(coin), "amount": amount}
        for coin, value in holdings.items()
        if (amount := _number(value)) is not None
    ]

    report: dict[str, Any] | None = None
    try:
        if report_file.exists():
            candidate = json.loads(report_file.read_text(encoding="utf-8"))
            if isinstance(candidate, dict):
                report = candidate
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning("Unable to read optional daily report: %s", exc)

    last_event = parsed_rows[-1][0].isoformat() if parsed_rows else None
    state_timestamp = str(state.get("timestamp") or "")
    recent = []
    for stamp, row in reversed(parsed_rows[-20:]):
        recent.append({
            "timestamp": stamp.isoformat(),
            "coin": str(row.get("coin") or ""),
            "action": row.get("action", "").lower(),
            "amount_base": _number(row.get("amount_base")),
            "net_pnl_base": (
                _number(row.get("net_pnl_base"))
                if row.get("execution_cost_mode") == "net_costs"
                and row.get("entry_cost_mode") == "net_costs"
                else None
            ),
        })

    currency = state.get("base_currency")
    if not isinstance(currency, str) or not currency.strip():
        currency = "—"
    return {
        "generated_at": now.isoformat(),
        "state_timestamp": state_timestamp,
        "last_journal_event": last_event,
        "portfolio": {
            "base_currency": currency,
            "cash": _number(state.get("cash")),
            "initial_value": _number(state.get("initial_portfolio_value")),
            "iteration": state.get("simulation_iteration"),
            "holdings": safe_holdings,
            "positions": positions,
        },
        "activity": {
            "today_buys": sum(row.get("action", "").lower() == "buy" for row in today_rows),
            "today_sell_events": len(today_sells),
            "today_net_pnl": _net_value(today_rows),
            "today_fees": _field_total(today_rows, "fee_base"),
            "today_slippage": _field_total(today_rows, "slippage_base"),
            "week_sell_events": len(week_sells),
            "week_net_pnl": _net_value(week_rows),
            "all_sell_events": len(all_sells),
        },
        "filters": _read_log_counts(log_path, datetime.combine(today, datetime.min.time(), UTC)),
        "daily_net_pnl": chart,
        "recent_events": recent,
        "daily_report": report,
    }


async def require_dashboard_access(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(security)],
) -> str:
    client_host = request.client.host if request.client else None
    try:
        client_ip = ipaddress.ip_address(client_host or "")
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied.") from None

    networks = _allowed_networks()
    if not any(client_ip in network for network in networks):
        raise HTTPException(status_code=403, detail="Access denied.")
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Basic"},
        )

    expected_user = _required_env("DASHBOARD_USERNAME").encode("utf-8")
    expected_password = _required_env("DASHBOARD_PASSWORD").encode("utf-8")
    supplied_user = credentials.username.encode("utf-8")
    supplied_password = credentials.password.encode("utf-8")
    user_ok = hmac.compare_digest(supplied_user, expected_user)
    password_ok = hmac.compare_digest(supplied_password, expected_password)
    if not (user_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.scheme != "https":
            response = JSONResponse(
                status_code=status.HTTP_426_UPGRADE_REQUIRED,
                content={"detail": "HTTPS is required."},
            )
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'"
        )
        return response


@asynccontextmanager
async def lifespan(_: FastAPI):
    _allowed_networks()
    _required_env("DASHBOARD_USERNAME")
    _required_env("DASHBOARD_PASSWORD")
    yield


app = FastAPI(
    title="Paper Trading Dashboard",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.add_middleware(SecurityHeadersMiddleware)

_snapshot_lock = threading.Lock()
_snapshot_cache: tuple[tuple[tuple[int, int] | None, ...], float, dict[str, Any]] | None = None


def _snapshot_signature() -> tuple[tuple[int, int] | None, ...]:
    data_dir = Path(os.getenv("DASHBOARD_DATA_DIR", "/data"))
    report_file = Path(os.getenv("DASHBOARD_REPORT_FILE", "/reports/latest.json"))
    log_file = Path(os.getenv("DASHBOARD_LOG_FILE", str(data_dir / "trading_bot.log")))
    signature = []
    for path in (data_dir / ".portfolio_state.json", data_dir / "trade_journal.csv", log_file, report_file):
        try:
            stat = path.stat()
            signature.append((stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            signature.append(None)
    return tuple(signature)


@app.get("/", response_class=HTMLResponse)
async def dashboard(_: Annotated[str, Depends(require_dashboard_access)]) -> str:
    return DASHBOARD_HTML


@app.get("/api/dashboard")
async def dashboard_data(
    _: Annotated[str, Depends(require_dashboard_access)],
) -> dict[str, Any]:
    global _snapshot_cache
    with _snapshot_lock:
        signature = _snapshot_signature()
        now = time.monotonic()
        if _snapshot_cache is not None:
            cached_signature, cached_at, cached_data = _snapshot_cache
            if signature == cached_signature and now - cached_at < 5:
                return cached_data
        data = load_dashboard_data()
        _snapshot_cache = (signature, now, data)
        return data


def main() -> None:
    host = os.getenv("DASHBOARD_BIND_HOST", "127.0.0.1").strip()
    try:
        ipaddress.ip_address(host)
    except ValueError as exc:
        raise DashboardConfigurationError(
            "DASHBOARD_BIND_HOST must be an IP address"
        ) from exc
    port = int(os.getenv("DASHBOARD_PORT", "8443"))
    certfile = _required_env("DASHBOARD_TLS_CERT")
    keyfile = _required_env("DASHBOARD_TLS_KEY")
    uvicorn.run(
        app,
        host=host,
        port=port,
        ssl_certfile=certfile,
        ssl_keyfile=keyfile,
        access_log=False,
    )


DASHBOARD_HTML = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>Paper Trading</title>
  <style>
    :root{font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#e8edf5;background:#0b1220}
    *{box-sizing:border-box}body{margin:0 auto;padding:24px;max-width:1280px}
    header{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:20px}
    .header-tools{display:flex;align-items:center;gap:16px}
    .clock{min-width:82px;color:#e8edf5;font-size:1.05rem;font-variant-numeric:tabular-nums;font-weight:650;letter-spacing:.04em}
    .refresh-countdown{color:#90a0b7;font-size:.82rem;font-variant-numeric:tabular-nums;white-space:nowrap}
    .language-switch{display:flex;gap:4px;padding:3px;border:1px solid #26354d;border-radius:8px}
    .language-switch button{border:0;border-radius:5px;padding:5px 8px;color:#90a0b7;background:transparent;font:inherit;font-size:.78rem;cursor:pointer}
    .language-switch button[aria-pressed="true"]{color:#e8edf5;background:#26354d}
    h1{font-size:1.55rem;margin:0}h2{font-size:1rem;margin:0 0 14px;color:#c5d2e5}
    .status{color:#8ce0b1;font-size:.88rem}.muted{color:#90a0b7;font-size:.82rem}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:12px;margin-bottom:16px}
    .card,section{background:#121d30;border:1px solid #26354d;border-radius:12px;padding:16px}
    .label{font-size:.78rem;color:#91a2ba}.value{font-size:1.42rem;font-weight:650;margin-top:8px;overflow-wrap:anywhere}
    .positive{color:#72db9d}.negative{color:#ff8585}.neutral{color:#e8edf5}
    .columns{display:grid;grid-template-columns:1.3fr 1fr;gap:14px;margin-bottom:14px}
    .chart{width:100%;height:220px}.chart text{fill:#90a0b7;font-size:11px}
    table{width:100%;border-collapse:collapse;font-size:.86rem}th,td{text-align:left;padding:9px 6px;border-bottom:1px solid #26354d}
    th{color:#91a2ba;font-weight:500}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
    .table-wrap{overflow:auto}.pills{display:flex;gap:8px;flex-wrap:wrap}
    .pill{background:#1b2b42;border-radius:999px;padding:7px 10px;font-size:.82rem}
    @media(max-width:720px){body{padding:14px}.columns{grid-template-columns:1fr}header{align-items:flex-start;flex-direction:column}.header-tools{gap:9px;flex-wrap:wrap}td,th{white-space:nowrap}}
  </style>
</head>
<body>
  <header><div><h1>Paper Trading</h1><div class="muted" data-i18n="readonly">Nur Anzeige · keine Order-Steuerung</div></div><div class="header-tools"><div id="updated" class="status">Aktualisiert: —</div><span id="refresh-countdown" class="refresh-countdown" role="timer" aria-live="off"></span><time id="local-clock" class="clock" aria-label="Lokale Uhrzeit">--:--:--</time><div class="language-switch" role="group" aria-label="Sprache"><button id="language-de" type="button" aria-pressed="true">DE</button><button id="language-en" type="button" aria-pressed="false">EN</button></div></div></header>
  <div id="metrics" class="grid"></div>
  <div class="columns">
    <section><h2 data-i18n="dailyPnl">Netto-PnL je UTC-Tag</h2><svg id="chart" class="chart" viewBox="0 0 700 220" role="img" aria-label="Netto-PnL der letzten 14 Tage"></svg></section>
    <section><h2 data-i18n="openPositions">Offene Positionen</h2><div id="positions" class="table-wrap"></div><h2 style="margin-top:20px" data-i18n="filtersFeed">Filter und Feed</h2><div id="filters" class="pills"></div></section>
  </div>
  <section><h2 data-i18n="recentEvents">Letzte Paper-Ereignisse</h2><div id="events" class="table-wrap"></div></section>
  <script>
    const REFRESH_INTERVAL_MS=15000;
    const translations={
      de:{readonly:'Nur Anzeige · keine Order-Steuerung',dailyPnl:'Netto-PnL je UTC-Tag',openPositions:'Offene Positionen',filtersFeed:'Filter und Feed',recentEvents:'Letzte Paper-Ereignisse',cash:'Verfügbares Cash',initialValue:'Startwert',todayPnl:'Netto-PnL heute',weekPnl:'Netto-PnL · 7 Tage',weekSells:'Verkaufsereignisse · 7 Tage',fees:'Gebühren heute',slippage:'Slippage heute',iteration:'Iteration',asset:'Asset',amount:'Menge',investment:'Einsatz',entry:'Einstieg',noneOpen:'Keine offenen Trades',atrFilter:'ATR-Filter',sellVeto:'Hailo-SELL-Veto',hailoErrors:'Hailo-Fehler',holdings:'Holdings',lastEvent:'Letztes Journal-Ereignis',timeLocal:'Zeit (lokal)',action:'Aktion',netPnl:'Netto-PnL',noEvents:'Noch keine Journal-Ereignisse',pnlUnavailable:'Netto-PnL nicht verfügbar',updated:'Aktualisiert',portfolioAt:'Portfolio-Stand',unavailable:'nicht verfügbar',refreshIn:'Aktualisierung in',seconds:'s',dashboardUnavailable:'Dashboard-Daten nicht erreichbar',buy:'Kauf',sell:'Verkauf',language:'Sprache',localTime:'Lokale Uhrzeit'},
      en:{readonly:'Read-only · no order controls',dailyPnl:'Net PnL by UTC day',openPositions:'Open positions',filtersFeed:'Filters and feed',recentEvents:'Recent paper events',cash:'Available cash',initialValue:'Starting value',todayPnl:'Net PnL today',weekPnl:'Net PnL · 7 days',weekSells:'Sell events · 7 days',fees:'Fees today',slippage:'Slippage today',iteration:'Iteration',asset:'Asset',amount:'Amount',investment:'Invested',entry:'Entry',noneOpen:'No open trades',atrFilter:'ATR filter',sellVeto:'Hailo SELL veto',hailoErrors:'Hailo errors',holdings:'Holdings',lastEvent:'Last journal event',timeLocal:'Time (local)',action:'Action',netPnl:'Net PnL',noEvents:'No journal events yet',pnlUnavailable:'Net PnL unavailable',updated:'Updated',portfolioAt:'Portfolio as of',unavailable:'unavailable',refreshIn:'Next update in',seconds:'s',dashboardUnavailable:'Dashboard data unavailable',buy:'Buy',sell:'Sell',language:'Language',localTime:'Local time'}
    };
    let locale='de';
    try{const saved=localStorage.getItem('paper-dashboard-language');if(saved==='de'||saved==='en')locale=saved}catch(e){console.warn('Language preference could not be loaded',e)}
    let nextRefreshAt=Date.now()+REFRESH_INTERVAL_MS;
    const t=key=>translations[locale][key];
    const fmt=(n,c='EUR')=>n===null||n===undefined?'—':new Intl.NumberFormat(locale==='de'?'de-DE':'en-US',{style:'currency',currency:c,maximumFractionDigits:2}).format(n);
    const esc=(s)=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const cls=n=>n===null||n===undefined?'neutral':n<0?'negative':n>0?'positive':'neutral';
    const localDateTime=value=>{
      const timestamp=Date.parse(value);
      return Number.isNaN(timestamp)?'—':new Intl.DateTimeFormat(locale==='de'?'de-DE':'en-US',{
        year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'
      }).format(timestamp);
    };
    function metric(label,value,color='neutral'){return `<div class="card"><div class="label">${label}</div><div class="value ${color}">${value}</div></div>`}
    function updateClock(){
      const clock=document.getElementById('local-clock'),now=new Date();
      clock.textContent=new Intl.DateTimeFormat(locale==='de'?'de-DE':'en-US',{
        hour:'2-digit',minute:'2-digit',second:'2-digit'
      }).format(now);
      clock.setAttribute('aria-label',`${t('localTime')}: ${clock.textContent}`);
    }
    function updateCountdown(){
      const remaining=Math.max(0,Math.ceil((nextRefreshAt-Date.now())/1000));
      document.getElementById('refresh-countdown').textContent=`${t('refreshIn')} ${remaining}${t('seconds')}`;
    }
    function updateLanguage(){
      document.documentElement.lang=locale;
      document.querySelectorAll('[data-i18n]').forEach(node=>{node.textContent=t(node.dataset.i18n)});
      document.getElementById('language-de').setAttribute('aria-pressed',String(locale==='de'));
      document.getElementById('language-en').setAttribute('aria-pressed',String(locale==='en'));
      document.querySelector('.language-switch').setAttribute('aria-label',t('language'));
      if(!window.dashboardData)document.getElementById('updated').textContent=`${t('updated')}: —`;
      updateClock();updateCountdown();
    }
    function setLanguage(value){
      locale=value;
      try{localStorage.setItem('paper-dashboard-language',locale)}catch(e){console.warn('Language preference could not be saved',e)}
      updateLanguage();
      if(window.dashboardData)render(window.dashboardData);
    }
    function drawChart(points){
      const svg=document.getElementById('chart'), w=700,h=220,p=34;
      const values=points.map(x=>x.net_pnl).filter(x=>x!==null);
      if(!values.length){svg.innerHTML=`<text x="20" y="35">${t('pnlUnavailable')}</text>`;return}
      const min=Math.min(0,...values),max=Math.max(0,...values),span=max-min||1;
      const y=v=>p+(max-v)/span*(h-p*2),zero=y(0),step=(w-p*2)/Math.max(1,points.length-1);
      const bars=points.map((x,i)=>{const v=x.net_pnl;if(v===null)return '';const yy=y(v),top=Math.min(yy,zero),height=Math.max(2,Math.abs(zero-yy));return `<rect x="${p+i*step-8}" y="${top}" width="16" height="${height}" rx="3" fill="${v<0?'#ff8585':'#72db9d'}"><title>${esc(x.date)}: ${fmt(v)}</title></rect>`}).join('');
      const labels=points.filter((_,i)=>i%3===0).map(x=>{const i=points.indexOf(x);return `<text x="${p+i*step}" y="${h-7}" text-anchor="middle">${esc(x.date.slice(5))}</text>`}).join('');
      svg.innerHTML=`<line x1="${p}" y1="${zero}" x2="${w-p}" y2="${zero}" stroke="#52647e"/>${bars}${labels}`;
    }
    function render(d){
      window.dashboardData=d;
      const c=d.portfolio.base_currency,a=d.activity;
      document.getElementById('metrics').innerHTML=[
        metric(t('cash'),fmt(d.portfolio.cash,c)),
        metric(t('initialValue'),fmt(d.portfolio.initial_value,c)),
        metric(t('todayPnl'),fmt(a.today_net_pnl,c),cls(a.today_net_pnl)),
        metric(t('weekPnl'),fmt(a.week_net_pnl,c),cls(a.week_net_pnl)),
        metric(t('weekSells'),a.week_sell_events),
        metric(t('fees'),fmt(a.today_fees,c)),
        metric(t('slippage'),fmt(a.today_slippage,c)),
        metric(t('iteration'),d.portfolio.iteration??'—')
      ].join('');
      document.querySelector('#chart').setAttribute('aria-label',`${t('dailyPnl')} · 14 ${locale==='de'?'Tage':'days'}`);
      drawChart(d.daily_net_pnl);
      const pos=d.portfolio.positions;
      document.getElementById('positions').innerHTML=pos.length?`<table><thead><tr><th>${t('asset')}</th><th class="num">${t('amount')}</th><th class="num">${t('investment')}</th><th>${t('entry')}</th></tr></thead><tbody>${pos.map(x=>`<tr><td>${esc(x.coin)}</td><td class="num">${x.amount_coin??'—'}</td><td class="num">${fmt(x.amount_base,c)}</td><td>${esc(localDateTime(x.timestamp))}</td></tr>`).join('')}</tbody></table>`:`<div class="muted">${t('noneOpen')}</div>`;
      const f=d.filters;
      document.getElementById('filters').innerHTML=[
        `${t('atrFilter')}: ${f.atr_guard??'—'}`,`${t('sellVeto')}: ${f.tabular_sell_veto??'—'}`,`${t('hailoErrors')}: ${f.hailo_failures??'—'}`,
        `${t('holdings')}: ${d.portfolio.holdings.length}`,`${t('lastEvent')}: ${esc(localDateTime(d.last_journal_event))}`
      ].map(x=>`<span class="pill">${x}</span>`).join('');
      document.getElementById('events').innerHTML=d.recent_events.length?`<table><thead><tr><th>${t('timeLocal')}</th><th>${t('asset')}</th><th>${t('action')}</th><th class="num">${t('amount')}</th><th class="num">${t('netPnl')}</th></tr></thead><tbody>${d.recent_events.map(x=>`<tr><td>${esc(localDateTime(x.timestamp))}</td><td>${esc(x.coin)}</td><td>${x.action==='buy'?t('buy'):x.action==='sell'?t('sell'):esc(x.action)}</td><td class="num">${fmt(x.amount_base,c)}</td><td class="num ${cls(x.net_pnl_base)}">${fmt(x.net_pnl_base,c)}</td></tr>`).join('')}</tbody></table>`:`<div class="muted">${t('noEvents')}</div>`;
    }
    async function refresh(){try{const r=await fetch('/api/dashboard',{cache:'no-store'});if(!r.ok)throw new Error(`HTTP ${r.status}`);render(await r.json());document.getElementById('updated').textContent=`${t('updated')}: ${new Date().toLocaleTimeString(locale==='de'?'de-DE':'en-US')}`}catch(e){document.getElementById('updated').textContent=t('dashboardUnavailable');console.error(e)}finally{nextRefreshAt=Date.now()+REFRESH_INTERVAL_MS;updateCountdown();setTimeout(refresh,REFRESH_INTERVAL_MS)}}
    document.getElementById('language-de').addEventListener('click',()=>setLanguage('de'));
    document.getElementById('language-en').addEventListener('click',()=>setLanguage('en'));
    updateLanguage();updateClock();updateCountdown();setInterval(updateClock,1000);setInterval(updateCountdown,1000);refresh();
  </script>
</body>
</html>"""


if __name__ == "__main__":
    main()
