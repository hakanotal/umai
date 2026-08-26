"""Admin dashboard: single-page statistics view with basic auth.

Served at /admin. Uses HTTP Basic Auth with hardcoded credentials.
Charts rendered client-side via Chart.js (CDN). No new dependencies.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import func, select, text

from umai.db.models import (
    ApiUsage,
    Food,
    FoodItem,
    LogEntry,
    User,
    UserStatus,
)

router = APIRouter(prefix="/admin", tags=["admin"])

security = HTTPBasic()

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "Albany2026"


def _verify_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    correct_username = credentials.username == ADMIN_USERNAME
    correct_password = credentials.password == ADMIN_PASSWORD
    if not (correct_username and correct_password):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return credentials.username


@router.get("", response_class=HTMLResponse)
async def admin_page(_user: str = Depends(_verify_admin)) -> HTMLResponse:
    return HTMLResponse(_DASHBOARD_HTML)


@router.get("/api/stats")
async def admin_stats(_user: str = Depends(_verify_admin)) -> JSONResponse:
    from umai.db.session import session_scope

    async with session_scope() as session:
        total_users = (await session.execute(select(func.count(User.id)))).scalar() or 0
        active_users = (
            await session.execute(
                select(func.count(User.id)).where(User.status == UserStatus.active)
            )
        ).scalar() or 0
        pending_users = (
            await session.execute(
                select(func.count(User.id)).where(User.status == UserStatus.pending)
            )
        ).scalar() or 0
        onboarding_users = (
            await session.execute(
                select(func.count(User.id)).where(User.status == UserStatus.onboarding)
            )
        ).scalar() or 0
        blocked_users = (
            await session.execute(
                select(func.count(User.id)).where(User.status == UserStatus.blocked)
            )
        ).scalar() or 0

        total_foods = (await session.execute(select(func.count(Food.id)))).scalar() or 0
        total_entries = (await session.execute(select(func.count(LogEntry.id)))).scalar() or 0
        total_food_items = (await session.execute(select(func.count(FoodItem.id)))).scalar() or 0

        total_cost = (
            await session.execute(select(func.sum(ApiUsage.cost_usd)))
        ).scalar() or Decimal("0")
        total_api_calls = (await session.execute(select(func.count(ApiUsage.id)))).scalar() or 0
        total_tokens_in = (
            await session.execute(select(func.sum(ApiUsage.input_tokens)))
        ).scalar() or 0
        total_tokens_out = (
            await session.execute(select(func.sum(ApiUsage.output_tokens)))
        ).scalar() or 0

        return JSONResponse(
            {
                "users": {
                    "total": total_users,
                    "active": active_users,
                    "pending": pending_users,
                    "onboarding": onboarding_users,
                    "blocked": blocked_users,
                },
                "foods": {"total": total_foods},
                "entries": {"total": total_entries, "food_items": total_food_items},
                "api": {
                    "total_cost_usd": float(total_cost),
                    "total_calls": total_api_calls,
                    "total_tokens_in": int(total_tokens_in),
                    "total_tokens_out": int(total_tokens_out),
                },
            }
        )


@router.get("/api/users/growth")
async def admin_user_growth(_user: str = Depends(_verify_admin)) -> JSONResponse:
    from umai.db.session import session_scope

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    func.date_trunc("day", User.created_at).label("day"),
                    func.count(User.id),
                )
                .group_by(text("day"))
                .order_by(text("day"))
            )
        ).all()
        result = [{"date": str(r[0].date()), "count": r[1]} for r in rows]
        return JSONResponse(result)


@router.get("/api/entries/daily")
async def admin_entries_daily(_user: str = Depends(_verify_admin)) -> JSONResponse:
    from umai.db.session import session_scope

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    func.date_trunc("day", LogEntry.occurred_at).label("day"),
                    func.count(LogEntry.id),
                )
                .group_by(text("day"))
                .order_by(text("day"))
            )
        ).all()
        result = [{"date": str(r[0].date()), "count": r[1]} for r in rows]
        return JSONResponse(result)


@router.get("/api/entries/by_source")
async def admin_entries_by_source(_user: str = Depends(_verify_admin)) -> JSONResponse:
    from umai.db.session import session_scope

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(LogEntry.source, func.count(LogEntry.id)).group_by(LogEntry.source)
            )
        ).all()
        result = [{"source": r[0].value, "count": r[1]} for r in rows]
        return JSONResponse(result)


@router.get("/api/entries/monthly")
async def admin_entries_monthly(_user: str = Depends(_verify_admin)) -> JSONResponse:
    from umai.db.session import session_scope

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    func.date_trunc("month", LogEntry.occurred_at).label("month"),
                    func.count(LogEntry.id),
                )
                .group_by(text("month"))
                .order_by(text("month"))
            )
        ).all()
        result = [{"month": str(r[0].date()), "count": r[1]} for r in rows]
        return JSONResponse(result)


@router.get("/api/api_usage/daily")
async def admin_api_usage_daily(_user: str = Depends(_verify_admin)) -> JSONResponse:
    from umai.db.session import session_scope

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    func.date_trunc("day", ApiUsage.called_at).label("day"),
                    func.count(ApiUsage.id),
                    func.sum(ApiUsage.input_tokens),
                    func.sum(ApiUsage.output_tokens),
                    func.sum(ApiUsage.cost_usd),
                )
                .group_by(text("day"))
                .order_by(text("day"))
            )
        ).all()
        result = [
            {
                "date": str(r[0].date()),
                "calls": r[1],
                "tokens_in": int(r[2] or 0),
                "tokens_out": int(r[3] or 0),
                "cost_usd": float(r[4] or 0),
            }
            for r in rows
        ]
        return JSONResponse(result)


@router.get("/api/api_usage/by_model")
async def admin_api_usage_by_model(_user: str = Depends(_verify_admin)) -> JSONResponse:
    from umai.db.session import session_scope

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    ApiUsage.model,
                    func.count(ApiUsage.id),
                    func.sum(ApiUsage.input_tokens),
                    func.sum(ApiUsage.output_tokens),
                    func.sum(ApiUsage.cost_usd),
                ).group_by(ApiUsage.model)
            )
        ).all()
        result = [
            {
                "model": r[0],
                "calls": r[1],
                "tokens_in": int(r[2] or 0),
                "tokens_out": int(r[3] or 0),
                "cost_usd": float(r[4] or 0),
            }
            for r in rows
        ]
        return JSONResponse(result)


@router.get("/api/api_usage/by_purpose")
async def admin_api_usage_by_purpose(_user: str = Depends(_verify_admin)) -> JSONResponse:
    from umai.db.session import session_scope

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    ApiUsage.purpose,
                    func.count(ApiUsage.id),
                    func.sum(ApiUsage.cost_usd),
                ).group_by(ApiUsage.purpose)
            )
        ).all()
        result = [
            {
                "purpose": r[0],
                "calls": r[1],
                "cost_usd": float(r[2] or 0),
            }
            for r in rows
        ]
        return JSONResponse(result)


_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Umai Admin</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  :root {
    --ink: #1D3527; --green: #2F5741; --green-soft: #4A7358;
    --sage: #7C9A5B; --gold: #B8862B; --gold-soft: #D2A94F;
    --cream: #EDE6C8; --cream-dim: #F4EFDC; --paper: #FCFBF6;
    --muted: #6E7A6E; --line: #DDD8C2;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: "Inter", "Helvetica Neue", Helvetica, Arial, sans-serif;
    background: var(--cream-dim); color: var(--ink);
    padding: 24px; max-width: 1200px; margin: 0 auto;
  }
  h1 { font-size: 1.5rem; font-weight: 600; margin-bottom: 24px; }
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 16px; margin-bottom: 32px;
  }
  .card {
    background: var(--paper); border: 1px solid var(--line);
    border-radius: 8px; padding: 20px;
  }
  .card .label {
    font-size: 0.75rem; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.05em;
  }
  .card .value { font-size: 1.8rem; font-weight: 600; margin-top: 4px; }
  .charts {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
    gap: 24px; margin-bottom: 32px;
  }
  .chart-box {
    background: var(--paper); border: 1px solid var(--line);
    border-radius: 8px; padding: 20px;
  }
  .chart-box h2 { font-size: 0.95rem; font-weight: 500; margin-bottom: 16px; }
  canvas { width: 100% !important; }
  .refresh { font-size: 0.7rem; color: var(--muted); margin-top: 16px; text-align: right; }
  .error { color: #c0392b; padding: 40px; text-align: center; }
</style>
</head>
<body>
<h1>Umai Admin</h1>
<div class="cards">
  <div class="card"><div class="label">Users</div><div class="value" id="v-users">--</div></div>
  <div class="card"><div class="label">Active</div><div class="value" id="v-active">--</div></div>
  <div class="card"><div class="label">Food Items</div><div class="value" id="v-food-items">--</div></div>
  <div class="card"><div class="label">Log Entries</div><div class="value" id="v-entries">--</div></div>
  <div class="card"><div class="label">API Calls</div><div class="value" id="v-api-calls">--</div></div>
  <div class="card"><div class="label">Total Cost</div><div class="value" id="v-cost">--</div></div>
</div>
<div class="charts">
  <div class="chart-box"><h2>User Growth</h2><canvas id="chart-growth"></canvas></div>
  <div class="chart-box"><h2>Daily Entries</h2><canvas id="chart-entries"></canvas></div>
  <div class="chart-box"><h2>Monthly Entries</h2><canvas id="chart-monthly"></canvas></div>
  <div class="chart-box"><h2>Entries by Source</h2><canvas id="chart-source"></canvas></div>
  <div class="chart-box"><h2>Daily API Calls</h2><canvas id="chart-api-daily"></canvas></div>
  <div class="chart-box"><h2>Cost by Model</h2><canvas id="chart-cost-model"></canvas></div>
</div>
<div class="refresh" id="refresh-time"></div>
<script>
const C = {
  green: "#2F5741", greenSoft: "#4A7358", sage: "#7C9A5B",
  gold: "#B8862B", goldSoft: "#D2A94F", muted: "#6E7A6E", line: "#DDD8C2"
};
function num(n) { return n.toLocaleString(); }
async function get(path) {
  var r = await fetch(path);
  if (!r.ok) throw new Error(r.status);
  return r.json();
}
function lineChart(ctx, labels, data, color, label) {
  return new Chart(ctx, {
    type: "line",
    data: { labels, datasets: [{ label: label, data: data, borderColor: color, backgroundColor: color + "22", fill: true, tension: 0.3, pointRadius: 2 }] },
    options: { responsive: true, plugins: { legend: { display: false } }, scales: { x: { grid: { color: C.line } }, y: { grid: { color: C.line }, beginAtZero: true } } }
  });
}
function barChart(ctx, labels, data, colors, label) {
  return new Chart(ctx, {
    type: "bar",
    data: { labels: labels, datasets: [{ label: label, data: data, backgroundColor: colors }] },
    options: { responsive: true, plugins: { legend: { display: false } }, scales: { x: { grid: { display: false } }, y: { grid: { color: C.line }, beginAtZero: true } } }
  });
}
async function init() {
  try {
    var results = await Promise.all([
      get("/admin/api/stats"),
      get("/admin/api/users/growth"),
      get("/admin/api/entries/daily"),
      get("/admin/api/entries/monthly"),
      get("/admin/api/entries/by_source"),
      get("/admin/api/api_usage/daily"),
      get("/admin/api/api_usage/by_model")
    ]);
    var stats = results[0], growth = results[1], daily = results[2],
        monthly = results[3], source = results[4], apiDaily = results[5], apiModel = results[6];
    document.getElementById("v-users").textContent = num(stats.users.total);
    document.getElementById("v-active").textContent = num(stats.users.active);
    document.getElementById("v-food-items").textContent = num(stats.entries.food_items);
    document.getElementById("v-entries").textContent = num(stats.entries.total);
    document.getElementById("v-api-calls").textContent = num(stats.api.total_calls);
    document.getElementById("v-cost").textContent = "$" + stats.api.total_cost_usd.toFixed(2);
    if (growth.length) {
      var cum = 0;
      var cumData = growth.map(function(g) { cum += g.count; return cum; });
      lineChart(document.getElementById("chart-growth"), growth.map(function(g) { return g.date; }), cumData, C.green, "Users");
    }
    if (daily.length) {
      lineChart(document.getElementById("chart-entries"), daily.map(function(d) { return d.date; }), daily.map(function(d) { return d.count; }), C.sage, "Entries");
    }
    if (monthly.length) {
      barChart(document.getElementById("chart-monthly"), monthly.map(function(m) { return m.month; }), monthly.map(function(m) { return m.count; }), C.goldSoft, "Entries");
    }
    if (source.length) {
      var colors = [C.green, C.greenSoft, C.sage, C.gold, C.goldSoft, C.muted];
      barChart(document.getElementById("chart-source"), source.map(function(s) { return s.source; }), source.map(function(s) { return s.count; }), source.map(function(_, i) { return colors[i % colors.length]; }), "Entries");
    }
    if (apiDaily.length) {
      lineChart(document.getElementById("chart-api-daily"), apiDaily.map(function(d) { return d.date; }), apiDaily.map(function(d) { return d.calls; }), C.greenSoft, "Calls");
    }
    if (apiModel.length) {
      var colors2 = [C.green, C.greenSoft, C.sage, C.gold, C.goldSoft, C.muted];
      barChart(document.getElementById("chart-cost-model"), apiModel.map(function(m) { return m.model; }), apiModel.map(function(m) { return m.cost_usd; }), apiModel.map(function(_, i) { return colors2[i % colors2.length]; }), "Cost (USD)");
    }
    document.getElementById("refresh-time").textContent = "Last loaded: " + new Date().toLocaleString();
  } catch (e) {
    document.body.innerHTML = '<p class="error">Failed to load dashboard data.</p>';
  }
}
init();
</script>
</body>
</html>\
"""
