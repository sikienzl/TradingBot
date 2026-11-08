"""Standalone, authenticated, read-only analytics dashboard.

Run ``uvicorn src.dashboard.app:app --host 127.0.0.1 --port 8080``.
Set DASHBOARD_USER, DASHBOARD_PASSWORD and DASHBOARD_DATABASE_URL; the last
must use a dedicated PostgreSQL SELECT-only role. DASHBOARD_DB_SCHEMA defaults
to the bot's ``public`` schema, DASHBOARD_STALE_SECONDS to 300. For remote
access, put it behind an authenticated TLS reverse proxy, never expose Basic
authentication over plaintext. This app is not mounted into the existing API.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from psycopg.rows import dict_row


@dataclass(frozen=True)
class Settings:
    user: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    database_url: str = field(default="", repr=False)
    schema: str = "public"
    stale_seconds: int = 300

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", self.schema):
            raise ValueError("Invalid analytics schema identifier")
        if self.stale_seconds < 1:
            raise ValueError("DASHBOARD_STALE_SECONDS must be positive")

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            user=os.getenv("DASHBOARD_USER", ""),
            password=os.getenv("DASHBOARD_PASSWORD", ""),
            database_url=os.getenv("DASHBOARD_DATABASE_URL", ""),
            schema=os.getenv("DASHBOARD_DB_SCHEMA", "public"),
            stale_seconds=int(os.getenv("DASHBOARD_STALE_SECONDS", "300")),
        )


def _read_analytics(settings: Settings) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch bounded histories from one consistent, read-only transaction."""
    with psycopg.connect(
        settings.database_url,
        connect_timeout=5,
        options="-c statement_timeout=5000",
        autocommit=True,
        row_factory=dict_row,
    ) as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        cur.execute(
            f"""
                SELECT snapshot_ts, iteration, portfolio_value_base, cash_base,
                       base_currency, open_positions_count, dry_run, open_trades_json
                FROM {settings.schema}.portfolio_snapshots
                ORDER BY snapshot_ts DESC, id DESC LIMIT 100
                """
        )
        snapshots = cur.fetchall()
        cur.execute(
            f"""
                SELECT event_ts, iteration, coin, action, price, amount_coin,
                       amount_base, pnl_base, pnl_pct, signal_source, reason,
                       dry_run, base_currency
                FROM {settings.schema}.trade_events
                ORDER BY event_ts DESC, id DESC LIMIT 100
                """
        )
        events = cur.fetchall()
    return snapshots, events


def _text(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return escape(value.isoformat())
    return escape(str(value))


def _mode(value: Any) -> str:
    return "DRY-RUN" if value is True else "LIVE" if value is False else "UNBEKANNT"


def _table(headers: tuple[str, ...], rows: list[tuple[Any, ...]]) -> str:
    header = "".join(f"<th>{_text(value)}</th>" for value in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_text(value)}</td>" for value in row) + "</tr>"
        for row in rows
    )
    if not body:
        body = f'<tr><td colspan="{len(headers)}">Keine Daten</td></tr>'
    return f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>"


def _page(content: str) -> str:
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta http-equiv="refresh" content="30"><title>Trading Dashboard</title>
<style>body{{font:16px system-ui;max-width:1200px;margin:auto;padding:1rem;background:#111827;color:#f3f4f6}}
h1,h2{{color:#e5e7eb}} .notice{{padding:1rem;background:#374151;border-left:4px solid #fbbf24}}
section{{overflow-x:auto;margin:1.5rem 0}} table{{border-collapse:collapse;width:100%}}
th,td{{padding:.5rem;text-align:left;border-bottom:1px solid #4b5563;vertical-align:top}}
th{{color:#93c5fd}} small{{color:#d1d5db}}
svg{{width:100%;max-width:760px;height:auto;background:#1f2937}}
svg text{{fill:#e5e7eb;font-size:12px}} svg .axis{{stroke:#9ca3af}}
svg .series{{fill:none;stroke:#60a5fa;stroke-width:2}} svg circle{{fill:#fbbf24}}</style></head>
<body><h1>Trading Dashboard · nur lesend</h1>{content}</body></html>"""


def _portfolio_charts(snapshots: list[dict[str, Any]]) -> str:
    """Draw independent time-scaled series for each mode and base currency."""
    groups: dict[tuple[bool, str], list[tuple[datetime, float]]] = {}
    skipped = 0
    for snapshot in snapshots:
        mode = snapshot.get("dry_run")
        currency = snapshot.get("base_currency")
        timestamp = snapshot.get("snapshot_ts")
        value = snapshot.get("portfolio_value_base")
        if (
            type(mode) is not bool
            or not isinstance(currency, str)
            or not currency.strip()
            or not isinstance(timestamp, datetime)
            or timestamp.utcoffset() is None
            or isinstance(value, bool)
        ):
            skipped += 1
            continue
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            skipped += 1
            continue
        if not math.isfinite(number):
            skipped += 1
            continue
        groups.setdefault((mode, currency), []).append((timestamp.astimezone(UTC), number))

    charts = []
    for (mode, currency), points in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1])):
        points.sort(key=lambda point: point[0])
        first, last = points[0][0], points[-1][0]
        duration = (last - first).total_seconds()
        values = [point[1] for point in points]
        low, high = min(values), max(values)
        scale = max(abs(low), abs(high), 1.0)
        scaled_low, scaled_high = low / scale, high / scale
        value_range = scaled_high - scaled_low
        coordinates = [
            (
                64 + (point_time - first).total_seconds() / duration * 556 if duration else 342,
                200 - (value / scale - scaled_low) / value_range * 180 if value_range else 110,
            )
            for point_time, value in points
        ]
        mode_label = _mode(mode)
        label = f"{mode_label} · {currency}"
        polyline = " ".join(f"{x:.2f},{y:.2f}" for x, y in coordinates)
        dots = "".join(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3">'
            f"<title>{_text(timestamp)}: {_text(value)} {_text(currency)}</title></circle>"
            for (timestamp, value), (x, y) in zip(points, coordinates)
        )
        charts.append(
            f"<h3>{_text(label)}</h3>"
            f'<svg viewBox="0 0 640 230" role="img" aria-label="{_text(label)}: '
            f'Portfolioverlauf mit {len(points)} Datenpunkten">'
            f"<title>Portfolioverlauf {_text(label)}</title>"
            '<line class="axis" x1="64" y1="20" x2="64" y2="200"/>'
            '<line class="axis" x1="64" y1="200" x2="620" y2="200"/>'
            f'<text x="60" y="24" text-anchor="end">{_text(f"{high:.5g}")}</text>'
            f'<text x="60" y="200" text-anchor="end">{_text(f"{low:.5g}")}</text>'
            f'<polyline class="series" points="{polyline}"/>{dots}</svg>'
            f"<p><small>{len(points)} Punkte · {_text(first)} bis {_text(last)} · "
            f"Werte in {_text(currency)}</small></p>"
        )
    note = f"<p><small>{skipped} unvollständige oder ungültige Snapshots nicht dargestellt.</small></p>" if skipped else ""
    if not charts:
        return "<p>Keine gültigen Daten für den Portfolioverlauf.</p>" + note
    return "".join(charts) + note


def _render(snapshots: list[dict[str, Any]], events: list[dict[str, Any]], stale_seconds: int) -> str:
    now = datetime.now(UTC)
    latest = snapshots[0] if snapshots else None
    positions: list[tuple[Any, ...]] = []
    if latest:
        timestamp = latest.get("snapshot_ts")
        if not isinstance(timestamp, datetime) or timestamp.utcoffset() is None:
            freshness = "Aktualität unbekannt – ungültiger Zeitstempel"
        else:
            age = (now - timestamp).total_seconds()
            if age < -30:
                freshness = "Zeitstempel liegt in der Zukunft – Uhrzeit prüfen"
            elif age > stale_seconds:
                freshness = f"VERALTET ({int(age)} s alt)"
            else:
                freshness = f"Aktuell ({max(0, int(age))} s alt)"
        trades = latest["open_trades_json"]
        if not isinstance(trades, dict):
            raise TypeError("Invalid positions")
        for coin, trade in trades.items():
            if not isinstance(trade, dict):
                raise TypeError("Invalid position")
            positions.append((
                coin, trade.get("amount_coin"), trade.get("buy_price"),
                trade.get("amount_base"), trade.get("timestamp"), trade.get("signal_source"),
            ))
        status = (
            f"<strong>Modus laut letztem Snapshot: {_mode(latest.get('dry_run'))}</strong><br>"
            f"Snapshot: {_text(timestamp)} · {_text(freshness)} · "
            f"offene Positionen: {_text(latest['open_positions_count'])}<br>"
            f"Portfolio: {_text(latest.get('portfolio_value_base'))} {_text(latest.get('base_currency'))} · "
            f"Cash: {_text(latest['cash_base'])} {_text(latest.get('base_currency'))}"
        )
    else:
        status = "<strong>Modus: UNBEKANNT</strong><br>Kein Portfolio-Snapshot vorhanden; Aktualität unbekannt."

    portfolio_rows = [
        (s.get("snapshot_ts"), s["iteration"], _mode(s.get("dry_run")), s.get("portfolio_value_base"),
         s["cash_base"], s.get("base_currency"), s["open_positions_count"])
        for s in snapshots
    ]
    event_rows = [
        (e["event_ts"], e["iteration"], _mode(e["dry_run"]), e["coin"], e["action"],
         e["price"], e["amount_coin"], e["amount_base"], e["pnl_base"], e["pnl_pct"],
         e["base_currency"], e["signal_source"], e["reason"])
        for e in events
    ]
    return _page(
        f'<div class="notice">{status}</div>'
        "<p><small>Snapshots sind keine Live-Order- oder Börsenabfrage. "
        "Aktualisierung alle 30 Sekunden; Anzeige auf je 100 Datensätze begrenzt.</small></p>"
        "<section><h2>Offene Positionen · letzter Snapshot</h2>"
        + _table(("Coin", "Menge", "Kaufpreis", "Investiert", "Seit", "Quelle"), positions)
        + "</section><section><h2>Ereignisse · neueste zuerst</h2>"
        + _table(("Zeit", "Iteration", "Modus", "Coin", "Aktion", "Preis", "Menge",
                  "Betrag", "PnL", "PnL %", "Währung", "Quelle", "Grund"), event_rows)
        + "</section><section><h2>Portfolioverlauf · grafisch</h2>"
        + "<p><small>Getrennte Diagramme je Modus und Währung; Zeitachse von links nach rechts. "
        "Linien verbinden verfügbare Werte, auch über Datenlücken hinweg.</small></p>"
        + _portfolio_charts(snapshots)
        + "</section><section><h2>Portfolioverlauf · neueste zuerst</h2>"
        + _table(("Zeit", "Iteration", "Modus", "Portfoliowert", "Cash", "Währung",
                  "Offene Positionen"), portfolio_rows)
        + "</section>"
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else Settings.from_env()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    basic = HTTPBasic(auto_error=False)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
        )
        return response

    def authenticate(credentials: HTTPBasicCredentials | None = Depends(basic)) -> None:  # noqa: B008
        if not settings.user or not settings.password:
            raise HTTPException(status_code=503, detail="Dashboard-Authentifizierung nicht konfiguriert")
        username = credentials.username if credentials else ""
        password = credentials.password if credentials else ""
        # Fixed-size digests allow constant-time comparisons even with non-ASCII configuration.
        user_ok = hmac.compare_digest(
            hashlib.sha256(username.encode()).digest(), hashlib.sha256(settings.user.encode()).digest()
        )
        password_ok = hmac.compare_digest(
            hashlib.sha256(password.encode()).digest(), hashlib.sha256(settings.password.encode()).digest()
        )
        if not (user_ok and password_ok and credentials):
            raise HTTPException(
                status_code=401, detail="Authentifizierung erforderlich",
                headers={"WWW-Authenticate": 'Basic realm="Trading Dashboard"'},
            )

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(authenticate)])
    def dashboard() -> Response:
        if not settings.database_url:
            return HTMLResponse(
                _page('<div class="notice">DB-Fehler: Datenbank nicht konfiguriert. '
                      "Modus und Aktualität unbekannt.</div>"), status_code=503,
            )
        try:
            snapshots, events = _read_analytics(settings)
            return HTMLResponse(_render(snapshots, events, settings.stale_seconds))
        except Exception:  # noqa: BLE001
            # Driver errors can contain connection strings or credentials.
            return HTMLResponse(
                _page('<div class="notice">DB-Fehler: Analytics-Daten nicht verfügbar. '
                      "Modus und Aktualität unbekannt.</div>"), status_code=503,
            )

    return app


app = create_app()
