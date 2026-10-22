"""Helpers that turn raw Liara API responses into compact, typed summaries.

Metric units are not documented in the OpenAPI spec. Responses look like Prometheus results
([unix_ts, "value"]); see the notes above `series_by_applet` for what was observed on a live app:
  * memory -> bytes (converted to MB here)
  * cpu    -> CPU-seconds over a trailing 5-minute window (converted to % of the plan's cores)
`scripts/smoke_test.py` prints raw samples so this can be re-checked against a real app.
"""
from __future__ import annotations

from typing import Any


def to_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------- apps
def app_summary(data: dict) -> dict:
    p = data.get("project", data) or {}
    return {
        "name": p.get("project_id") or p.get("name"),
        "id": p.get("_id"),
        "platform": p.get("type"),
        "status": p.get("status"),
        "running": p.get("scale", 0) not in (0, "0", None),
        "scale": p.get("scale"),
        "plan_id": p.get("planID"),
        "bundle_plan_id": p.get("bundlePlanID"),
        "is_deployed": p.get("isDeployed"),
        "zero_downtime": p.get("zeroDowntime"),
        "default_subdomain": p.get("defaultSubdomain"),
        "hourly_price": p.get("hourlyPrice"),
        "env_keys": [e.get("key") for e in p.get("envs", []) or [] if isinstance(e, dict)],
        "created_at": p.get("created_at"),
    }


def env_list(data: dict) -> list[dict]:
    p = data.get("project", data) or {}
    return [{"key": e["key"], "value": e.get("value", "")} for e in p.get("envs", []) or [] if e.get("key")]


def apps_list(data: Any) -> list[dict]:
    items = data.get("projects", data) if isinstance(data, dict) else data
    out = []
    for p in items or []:
        out.append({
            "name": p.get("project_id") or p.get("name"),
            "platform": p.get("type"),
            "status": p.get("status"),
            "running": p.get("scale", 0) not in (0, "0", None),
            "plan_id": p.get("planID"),
        })
    return out


# -------------------------------------------------------------- releases
def releases_list(data: dict) -> dict:
    rel = []
    for r in data.get("releases", []) or []:
        git = r.get("gitInfo") or {}
        rel.append({
            "id": r.get("_id"),
            "tag": r.get("tag"),
            "state": r.get("state"),
            "created_at": r.get("created_at") or r.get("createdAt"),
            "port": r.get("port"),
            "message": r.get("message") or git.get("message"),
            "source_id": r.get("sourceID"),
        })
    return {"current_release": data.get("currentRelease"), "total": data.get("total"), "releases": rel}


# ------------------------------------------------------------------- logs
def logs_v2(data: dict) -> list[dict]:
    """v2 logs: {data: [{metaData: {releaseId}, values: [[ts, line], ...]}]} -> sorted lines."""
    lines = []
    for entry in (data or {}).get("data", []) or []:
        rid = (entry.get("metaData") or {}).get("releaseId")
        for value in entry.get("values", []) or []:
            if not value or len(value) < 2:
                continue
            ts = _ts_seconds(value[0])
            lines.append({"ts": ts, "line": str(value[1]), "release_id": rid})
    lines.sort(key=lambda x: x["ts"] or 0)
    return lines


def _ts_seconds(v: Any) -> float | None:
    f = to_float(v)
    if f is None:
        return None
    if f > 1e17:  # nanoseconds
        return f / 1e9
    if f > 1e14:  # microseconds
        return f / 1e6
    if f > 1e11:  # milliseconds
        return f / 1e3
    return f


# ---------------------------------------------------------------- metrics
# Observed on a live app (Oct 2026):
#  * each container ("applet") is its own series; after a restart the old container's series keeps being
#    reported with its last value for a few minutes, so summing applets double-counts memory;
#  * CPU values are CPU-seconds consumed over a trailing 5-minute window: after a restart they climb
#    linearly for ~5 minutes and then plateau (an idle Node app plateaus around 0.6-0.7). Cores in use =
#    value / 300. Check with `scripts/smoke_test.py` if Liara changes this.
CPU_WINDOW_SEC = 300
def series_by_applet(data: dict) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list[tuple[float, float]]] = {}
    for i, r in enumerate((data or {}).get("result", []) or []):
        pts = []
        for pair in r.get("values", []) or []:
            if len(pair) < 2:
                continue
            ts, val = _ts_seconds(pair[0]), to_float(pair[1])
            if ts is not None and val is not None:
                pts.append((ts, val))
        if pts:
            out[str(r.get("applet") or i)] = sorted(pts)
    return out


def current_series(data: dict) -> list[tuple[float, float]]:
    """One value per timestamp, taken from the newest container that reported at that time."""
    by = series_by_applet(data)
    first_seen = {a: p[0][0] for a, p in by.items() if p}
    acc: dict[float, tuple[float, float]] = {}
    for a, pts in by.items():
        for t, v in pts:
            cur = acc.get(t)
            if cur is None or first_seen[a] > cur[0]:
                acc[t] = (first_seen[a], v)
    return sorted((t, v) for t, (_, v) in acc.items())


def series(data: dict) -> list[tuple[float, float]]:
    """Reports {result: [{values: [[ts, v]...], applet}]} -> [(ts, summed value)] across applets."""
    acc: dict[float, float] = {}
    for r in (data or {}).get("result", []) or []:
        for pair in r.get("values", []) or []:
            if len(pair) < 2:
                continue
            ts, val = _ts_seconds(pair[0]), to_float(pair[1])
            if ts is None or val is None:
                continue
            acc[ts] = acc.get(ts, 0.0) + val
    return sorted(acc.items())


def summary_value(items: list[dict] | None) -> float | None:
    total = None
    for it in items or []:
        v = it.get("value")
        if isinstance(v, list) and len(v) >= 2:
            f = to_float(v[1])
        else:
            f = to_float(v)
        if f is not None:
            total = (total or 0.0) + f
    return total


def memory_mb(raw: float | None) -> float | None:
    if raw is None:
        return None
    return raw / (1024 * 1024) if raw > 65536 else raw  # bytes -> MB, else assume MB


def cpu_percent(raw: float | None, plan_cores: float | None) -> float | None:
    """CPU-seconds over the trailing window -> % of the plan's cores."""
    if raw is None:
        return None
    cores_used = raw / CPU_WINDOW_SEC
    if plan_cores:
        return round(100 * cores_used / plan_cores, 1)
    return round(100 * cores_used, 1)


def disk_usage(items: list[dict] | None) -> list[dict]:
    out = []
    for d in items or []:
        size, usage = to_float(d.get("size")), to_float(d.get("usage"))
        pct = round(100 * usage / size, 1) if size and usage is not None else None
        out.append({"name": d.get("name"), "size": d.get("size"), "usage": d.get("usage"), "percent": pct})
    return out


# ------------------------------------------------------------------ plans
def plans(me: dict) -> list[dict]:
    """Parse the plan catalogue from GET /v1/me (same fields the official CLI reads)."""
    catalogue = ((me or {}).get("plans") or {})
    projects = catalogue.get("projects") or {}
    bundles = catalogue.get("projectBundlePlans") or {}
    out = []
    for plan_id, p in projects.items():
        if not isinstance(p, dict):
            continue
        ram = to_float((p.get("RAM") or {}).get("amount"))
        cpu = to_float((p.get("CPU") or {}).get("amount"))
        hourly = to_float(p.get("price")) or 0.0
        b = bundles.get(plan_id) or {}
        out.append({
            "id": plan_id,
            "ram_gb": ram,
            "cpu_cores": cpu,
            "disk_gb": to_float(p.get("volume")),
            "monthly_toman": round(hourly * 720),
            "available": bool(p.get("available", True)),
            "region": p.get("region"),
            "bundles": {k: (v or {}).get("displayPrice") for k, v in b.items()
                        if isinstance(v, dict) and v.get("available", True)},
        })
    out.sort(key=lambda x: (x["ram_gb"] or 0, x["cpu_cores"] or 0))
    return out


def deployable_plans(all_plans: list[dict]) -> list[dict]:
    # Same filter the official CLI uses for `liara plan list`.
    return [p for p in all_plans
            if p["available"] and (p["id"] == "free" or "g2" in p["id"]) and p.get("region") in (None, "iran")]


def plan_by_id(all_plans: list[dict], plan_id: str | None) -> dict | None:
    return next((p for p in all_plans if p["id"] == plan_id), None)
