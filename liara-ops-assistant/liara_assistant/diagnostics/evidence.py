"""Collect evidence about an app from every available source, in parallel."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from ..liara import parse
from ..liara.client import LiaraError
from ..probe import default_url, probe
from ..redact import redact

COLLECT_TIMEOUT = 25


async def _safe(coro, timeout: float = COLLECT_TIMEOUT) -> Any:
    try:
        return await asyncio.wait_for(coro, timeout)
    except LiaraError as e:
        return e
    except Exception as e:  # noqa: BLE001
        return LiaraError(0, f"{type(e).__name__}: {e}")


async def get_plans(ctx) -> list[dict]:
    cache = ctx.state.setdefault("cache", {})
    hit = cache.get("plans")
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    me = await ctx.client.me()
    plans = parse.plans(me)
    cache["plans"] = (time.time(), plans)
    cache["me"] = (time.time(), me)
    return plans


async def fetch_logs(ctx, app: str, start: int) -> list[dict]:
    """v2 logs (what the CLI uses), falling back to the v1 variant in the spec."""
    try:
        return parse.logs_v2(await ctx.client.logs(app, start))
    except LiaraError as e:
        if e.status not in (404, 405, 400):
            raise
    raw = await ctx.client.get(f"v1/projects/{app}/logs", params={"since": start})
    out = []
    for item in raw if isinstance(raw, list) else []:
        out.append({"ts": None, "line": f"{item.get('datetime', '')} {item.get('type', '')} {item.get('message', '')}",
                    "release_id": None})
    return out


def parse_events(data: Any) -> list[dict]:
    out = []
    for entry in (data or {}).get("data", []) if isinstance(data, dict) else []:
        meta = entry.get("metaData") or {}
        for v in entry.get("values", []) or []:
            if len(v) >= 2:
                out.append({"ts": parse._ts_seconds(v[0]), "text": redact(str(v[1]))[:300],
                            "meta": {k: meta[k] for k in list(meta)[:4]}})
    out.sort(key=lambda x: x["ts"] or 0)
    return out


async def collect(ctx, app: str, minutes: int = 60, url: str | None = None) -> dict:
    now = int(time.time())
    start = now - minutes * 60
    c = ctx.client
    names = ["app", "releases", "events", "logs", "summary", "cpu", "memory", "plans", "probe"]
    coros = [
        _safe(c.get_app(app)),
        _safe(c.list_releases(app, 1, 5)),
        _safe(c.events(app, start, now)),
        _safe(fetch_logs(ctx, app, start)),
        _safe(c.metrics_summary(app)),
        _safe(c.metrics_series(app, "cpu", start)),
        _safe(c.metrics_series(app, "memory", start)),
        _safe(get_plans(ctx)),
        _safe(probe(url or default_url(app)), timeout=60),
    ]
    raw = dict(zip(names, await asyncio.gather(*coros)))
    errors = {k: v.friendly if isinstance(v, LiaraError) else None for k, v in raw.items()}
    errors = {k: v for k, v in errors.items() if v}
    ok = {k: v for k, v in raw.items() if not isinstance(v, LiaraError)}

    app_info = parse.app_summary(ok["app"]) if "app" in ok else {"name": app}
    plan = parse.plan_by_id(ok.get("plans") or [], app_info.get("plan_id")) or {}
    ram_mb = (plan.get("ram_gb") or 0) * 1024 or None
    cores = plan.get("cpu_cores")

    summary = ok.get("summary") or {}
    mem_now = parse.memory_mb(parse.summary_value(summary.get("memoryUsage")))
    cpu_now = parse.cpu_percent(parse.summary_value(summary.get("cpuUsage")), cores)
    mem_series = [(t, parse.memory_mb(v)) for t, v in parse.series(ok.get("memory") or {})]
    cpu_series = [(t, parse.cpu_percent(v, cores)) for t, v in parse.series(ok.get("cpu") or {})]

    logs = ok.get("logs") or []
    for line in logs:
        line["line"] = redact(line["line"])[:500]

    return {
        "app": app,
        "collected_at": now,
        "window_minutes": minutes,
        "app_info": app_info,
        "plan": {"id": plan.get("id"), "ram_mb": ram_mb, "cpu_cores": cores,
                 "monthly_toman": plan.get("monthly_toman")},
        "releases": parse.releases_list(ok["releases"]) if "releases" in ok else None,
        "events": parse_events(ok.get("events"))[-40:],
        "logs": logs[-2000:],
        "metrics": {
            "memory_mb_now": round(mem_now, 1) if mem_now is not None else None,
            "memory_pct_now": round(100 * mem_now / ram_mb, 1) if mem_now and ram_mb else None,
            "cpu_pct_now": cpu_now,
            "memory_series_mb": [(t, round(v, 1)) for t, v in mem_series if v is not None],
            "cpu_series_pct": [(t, v) for t, v in cpu_series if v is not None],
            "disks": parse.disk_usage(summary.get("disksUsage")),
        },
        "probe": ok.get("probe"),
        "unavailable": errors,
    }
