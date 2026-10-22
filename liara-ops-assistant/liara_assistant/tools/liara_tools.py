"""All assistant tools. Importing this module registers them in REGISTRY."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Annotated, Literal

import httpx
from pydantic import Field

from .. import plans as plans_mod
from ..deploy import packager, pipeline
from ..diagnostics import engine
from ..diagnostics.evidence import fetch_logs, get_plans, parse_events
from ..diagnostics.signals import error_lines, top_errors
from ..liara import parse
from ..liara.client import LiaraError
from ..probe import default_url, probe
from ..redact import redact
from .registry import Ctx, Risk, tool

ASK_USER = "ASK_USER"
App = Annotated[str, Field(description="Liara app name (project_id), e.g. 'my-shop'")]
Minutes = Annotated[int, Field(description="Look-back window in minutes", ge=1, le=10080)]


def _mark_action(ctx: Ctx, app: str) -> None:
    ctx.state.setdefault("last_action_ts", {})[app] = time.time()


def _downsample(points: list, n: int = 12) -> list:
    if len(points) <= n:
        return points
    step = len(points) / n
    return [points[int(i * step)] for i in range(n)]


def _hhmm(ts):
    return time.strftime("%H:%M", time.localtime(ts)) if ts else None


# ============================================================ READ: account/apps
@tool(Risk.READ, "فهرست برنامه‌ها")
async def list_apps(ctx: Ctx) -> dict:
    """List all apps (projects) in the Liara account/team with status and plan."""
    return {"apps": parse.apps_list(await ctx.client.list_apps())}


@tool(Risk.READ, "وضعیت برنامه")
async def get_app_status(ctx: Ctx, app: App) -> dict:
    """Current status of one app: running or not, plan, platform, env var keys (values hidden), current
    release, and live CPU/RAM usage."""
    info = parse.app_summary(await ctx.client.get_app(app))
    out: dict = {"app": info}
    try:
        rel = parse.releases_list(await ctx.client.list_releases(app, 1, 3))
        out["releases"] = rel["releases"]
    except LiaraError as e:
        out["releases_error"] = e.friendly
    try:
        plans = await get_plans(ctx)
        plan = parse.plan_by_id(plans, info.get("plan_id")) or {}
        s = await ctx.client.metrics_summary(app)
        mem = parse.memory_mb(parse.summary_value(s.get("memoryUsage")))
        out["resources"] = {
            "plan": {"id": plan.get("id"), "ram_gb": plan.get("ram_gb"), "cpu_cores": plan.get("cpu_cores"),
                     "monthly_toman": plan.get("monthly_toman")},
            "memory_mb": round(mem, 1) if mem is not None else None,
            "memory_pct": round(100 * mem / (plan["ram_gb"] * 1024), 1) if mem and plan.get("ram_gb") else None,
            "cpu_pct": parse.cpu_percent(parse.summary_value(s.get("cpuUsage")), plan.get("cpu_cores")),
            "disks": parse.disk_usage(s.get("disksUsage")),
        }
    except LiaraError as e:
        out["resources_error"] = e.friendly
    out["default_url"] = default_url(app)
    return out


@tool(Risk.READ, "اطلاعات حساب و اعتبار")
async def account_overview(ctx: Ctx) -> dict:
    """Account summary: remaining free credit and billing balance (for checking before cost-changing actions)."""
    out: dict = {"team_id": ctx.settings.liara_team_id or None}
    for key, fn in (("free_credit", ctx.client.free_credit), ("billing", ctx.client.billing)):
        try:
            data = await fn()
            out[key] = {k: v for k, v in data.items() if isinstance(v, (int, float, str, bool))} \
                if isinstance(data, dict) else data
        except LiaraError as e:
            out[key] = {"unavailable": e.friendly}
    return out


@tool(Risk.READ, "فهرست پلن‌ها")
async def list_plans(ctx: Ctx) -> dict:
    """List deployable hardware plans (RAM, CPU, disk, monthly price in Toman) and feature-bundle prices."""
    plans = parse.deployable_plans(await get_plans(ctx))
    return {"plans": plans, "tiers_fa": plans_mod.TIER_FA, "note": "monthly_toman = hourly price × 720"}


@tool(Risk.READ, "پیشنهاد پلن")
async def recommend_plan(
    ctx: Ctx,
    platform: Annotated[str, Field(description="Platform, e.g. node, django, laravel, static")],
    daily_visitors: Annotated[int, Field(description="Expected visitors per day (ask the user)")] = 500,
    has_database_work: Annotated[bool, Field(description="App uses a database/cache heavily")] = False,
) -> dict:
    """Recommend a hardware plan + feature bundle with reasons and monthly cost."""
    cache = ctx.state.get("cache", {})
    if "me" not in cache:
        await get_plans(ctx)
    me = ctx.state["cache"]["me"][1]
    return plans_mod.recommend(me, platform, daily_visitors, has_database_work)


@tool(Risk.READ, "تحلیل پروژه")
async def analyze_project(
    ctx: Ctx, path: Annotated[str, Field(description="Local path of the project folder to deploy")],
) -> dict:
    """Inspect a local project folder: detect platform and port, size, liara.json, health check, expected env
    vars and database hints. Use before the first deploy."""
    try:
        return packager.analyze(path)
    except packager.PackagingError as e:
        return {"error": str(e)}


# ======================================================== READ: observability
@tool(Risk.READ, "دریافت لاگ‌ها")
async def get_logs(
    ctx: Ctx, app: App, minutes: Minutes = 30,
    errors_only: Annotated[bool, Field(description="Only return error lines")] = False,
    search: Annotated[str | None, Field(description="Case-insensitive text filter")] = None,
    limit: Annotated[int, Field(ge=1, le=300)] = 80,
) -> dict:
    """Fetch runtime logs of an app for the last N minutes (secrets are masked). Also returns the top error
    signatures. Note: log retention depends on the feature bundle (Free: 1 hour)."""
    lines = await fetch_logs(ctx, app, int(time.time()) - minutes * 60)
    for line in lines:
        line["line"] = redact(line["line"])
    sel = error_lines(lines) if errors_only else lines
    if search:
        sel = [line for line in sel if search.lower() in line["line"].lower()]
    return {"total_lines": len(lines), "error_lines": len(error_lines(lines)), "top_errors": top_errors(lines),
            "lines": [f"{_hhmm(line['ts']) or ''} {line['line'][:400]}" for line in sel[-limit:]]}


@tool(Risk.READ, "دریافت متریک‌ها")
async def get_metrics(ctx: Ctx, app: App, minutes: Minutes = 60) -> dict:
    """CPU and memory usage now, peak and trend over the window (as % of the plan), plus disk usage."""
    ev = await engine.ev_mod.collect(ctx, app, minutes=minutes)
    m = ev["metrics"]
    return {
        "plan": ev["plan"],
        "now": {"memory_mb": m["memory_mb_now"], "memory_pct": m["memory_pct_now"], "cpu_pct": m["cpu_pct_now"]},
        "peak": {"memory_mb": max((v for _, v in m["memory_series_mb"]), default=None),
                 "cpu_pct": max((v for _, v in m["cpu_series_pct"]), default=None)},
        "memory_trend_mb": [(_hhmm(t), v) for t, v in _downsample(m["memory_series_mb"])],
        "cpu_trend_pct": [(_hhmm(t), v) for t, v in _downsample(m["cpu_series_pct"])],
        "disks": m["disks"],
        "unavailable_sources": ev["unavailable"],
        "note": "Liara provides CPU/RAM/network/disk only — no request latency or HTTP status metrics.",
    }


@tool(Risk.READ, "رویدادهای برنامه")
async def get_events(ctx: Ctx, app: App, minutes: Minutes = 120) -> dict:
    """Platform events (deploys, restarts, crashes, OOM kills) in the window."""
    now = int(time.time())
    data = await ctx.client.events(app, now - minutes * 60, now)
    evs = parse_events(data)
    return {"events": [{"time": _hhmm(e["ts"]), "text": e["text"]} for e in evs[-40:]]}


@tool(Risk.READ, "تاریخچه release‌ها")
async def list_releases(ctx: Ctx, app: App) -> dict:
    """Release history with state (READY/FAILED/...) and the current release id."""
    return parse.releases_list(await ctx.client.list_releases(app, 1, 10))


@tool(Risk.READ, "تست HTTP وب‌سایت")
async def probe_website(
    ctx: Ctx, target: Annotated[str, Field(description="App name or full URL")],
    path: Annotated[str, Field(description="Optional path, e.g. /api/orders")] = "",
) -> dict:
    """Send 3 HTTP requests to the site (default https://<app>.liara.run) and report status codes and timing."""
    url = target if target.startswith("http") else default_url(target)
    return await probe(url, n=3, path=path)


@tool(Risk.READ, "عیب‌یابی هوشمند")
async def diagnose_app(
    ctx: Ctx, app: App,
    symptom: Annotated[str, Field(description="User's description, e.g. 'HTTP 500' or 'site is slow'")] = "",
    minutes: Minutes = 60,
    url: Annotated[str | None, Field(description="Custom domain URL to test; default <app>.liara.run")] = None,
) -> dict:
    """Full investigation: collects status, releases, events, logs, metrics and an HTTP probe in parallel,
    extracts signals with rules, and returns ranked hypotheses with evidence and proposed actions. Always
    call this first when the user reports a problem."""
    return await engine.diagnose(ctx, app, symptom, minutes, url)


@tool(Risk.READ, "بررسی نتیجه اقدام")
async def verify_fix(
    ctx: Ctx, app: App,
    url: Annotated[str | None, Field(description="URL to test; default <app>.liara.run")] = None,
) -> dict:
    """After a corrective action: wait for the app to settle, re-probe, check new errors and resources, and
    compare with the baseline saved by diagnose_app. Always call after a fix."""
    return await engine.verify(ctx, app, url)


@tool(Risk.READ, "بررسی سلامت سریع")
async def check_health(ctx: Ctx, app: App, url: str | None = None) -> dict:
    """Quick health snapshot (last 10 minutes): HTTP probe, resources and any warning/critical issues."""
    return await engine.health(ctx, app, url)


# ============================================================ READ: domains/DNS
@tool(Risk.READ, "فهرست دامنه‌ها")
async def list_domains(ctx: Ctx, app: Annotated[str | None, Field(description="Filter by app")] = None) -> dict:
    """List custom domains (optionally for one app) with status and SSL status."""
    data = await ctx.client.list_domains(app)
    items = data.get("domains", data) if isinstance(data, dict) else data
    return {"domains": [{"id": d.get("_id"), "name": d.get("name"), "status": d.get("status"),
                         "ssl": d.get("certificatesStatus"), "app": (d.get("project") or {}).get("project_id")}
                        for d in items or [] if isinstance(d, dict)]}


@tool(Risk.READ, "بررسی وضعیت دامنه")
async def check_domain(ctx: Ctx, domain_id: Annotated[str, Field(description="Domain _id from list_domains")]) -> dict:
    """Check whether a domain's DNS records are set correctly and its SSL status."""
    return await ctx.client.check_domain(domain_id)


@tool(Risk.READ, "اطلاعات زون DNS")
async def get_dns_zone(ctx: Ctx, zone: Annotated[str, Field(description="Domain, e.g. example.ir")]) -> dict:
    """Liara DNS zone info, including the nameservers the user must set at the registrar (e.g. nic.ir) and the
    nameservers currently active."""
    data = await ctx.client.get_zone(zone)
    d = data.get("data", data) if isinstance(data, dict) else data
    return {"zone": zone, "status": d.get("status"), "name_servers_to_set": d.get("nameServers"),
            "current_name_servers": d.get("currentNameServers")}


@tool(Risk.READ, "رکوردهای DNS")
async def list_dns_records(ctx: Ctx, zone: Annotated[str, Field(description="Domain, e.g. example.ir")]) -> dict:
    """List DNS records of a Liara DNS zone."""
    return await ctx.client.list_records(zone)


# ================================================================== READ: docs
_DOCS_INDEX: list[dict] | None = None


def _docs_index() -> list[dict]:
    global _DOCS_INDEX
    if _DOCS_INDEX is None:
        _DOCS_INDEX = json.loads((Path(__file__).parent.parent / "data" / "liara_docs_index.json")
                                 .read_text(encoding="utf-8"))
    return _DOCS_INDEX


@tool(Risk.READ, "جستجو در مستندات لیارا")
async def search_liara_docs(
    ctx: Ctx, query: Annotated[str, Field(description="Keywords in English or Persian, e.g. "
                                                      "'nodejs fix-common-errors cors' or 'health check'")],
    fetch_top: Annotated[int, Field(ge=0, le=3)] = 2,
) -> dict:
    """Search Liara's official docs (LLM-ready Markdown) and return the most relevant pages with content.
    Useful for platform-specific fixes (e.g. paas/<platform>/fix-common-errors)."""
    tokens = [t for t in re.split(r"[\s/,_-]+", query.lower()) if len(t) > 1]
    scored = []
    for d in _docs_index():
        hay = (d["path"] + " " + d["title"]).lower()
        score = sum(2 if t in d["path"].lower() else 1 for t in tokens if t in hay)
        if score:
            scored.append((score, d))
    scored.sort(key=lambda x: (-x[0], len(x[1]["path"])))
    top = [d for _, d in scored[:6]]
    pages = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as http:
        for d in top[:fetch_top]:
            for url in (f"https://docs.liara.ir/llms/{d['path']}",
                        f"https://raw.githubusercontent.com/liara-cloud/docs/master/public/llms/{d['path']}"):
                try:
                    r = await http.get(url)
                    if r.status_code == 200 and r.text.strip():
                        pages.append({"title": d["title"], "url": d["url"], "content": r.text[:3500]})
                        break
                except httpx.HTTPError:
                    continue
    return {"results": [{"title": d["title"], "url": d["url"], "path": d["path"]} for d in top], "pages": pages}


@tool(Risk.READ, "گزارش عملیات (audit)")
async def show_audit_log(ctx: Ctx, n: Annotated[int, Field(ge=1, le=50)] = 15) -> dict:
    """Show the most recent actions the assistant executed or that the user approved/rejected."""
    audit = ctx.state.get("audit")
    return {"entries": audit.tail(n) if audit else []}


# ======================================================================== LOW
@tool(Risk.LOW, "صدور گواهی SSL", describe=lambda a: f"صدور گواهی SSL برای {a.get('domain')}")
async def issue_ssl(ctx: Ctx, domain: Annotated[str, Field(description="Domain name, e.g. example.ir")]) -> dict:
    """Provision an SSL certificate for a domain whose DNS records are already verified."""
    return await ctx.client.provision_ssl(domain)


@tool(Risk.LOW, "بررسی nameserverها", describe=lambda a: f"بررسی تغییر nameserverهای {a.get('zone')}")
async def check_dns_zone(ctx: Ctx, zone: Annotated[str, Field(description="Domain, e.g. example.ir")]) -> dict:
    """Ask Liara DNS to re-check whether the registrar now points to Liara's nameservers."""
    return await ctx.client.check_zone(zone)


@tool(Risk.LOW, "پایش خودکار", describe=lambda a: f"شروع پایش دوره‌ای {a.get('app')}")
async def watch_app(ctx: Ctx, app: App, url: str | None = None) -> dict:
    """Start periodic background health checks for an app; alerts appear in the chat. Read-only."""
    ctx.state.setdefault("watch", {})[app] = {"url": url, "since": time.time()}
    return {"watching": list(ctx.state["watch"].keys()),
            "interval_sec": ctx.settings.monitor_interval_sec,
            "note": "Only alerts are automatic; any fix still needs approval."}


# ===================================================================== MEDIUM
async def _deploy_impact(ctx: Ctx, a: dict) -> dict:
    zd = None
    try:
        zd = parse.app_summary(await ctx.client.get_app(a["app"])).get("zero_downtime")
    except LiaraError:
        pass
    return {"downtime": "بدون قطعی (zero-downtime فعال است)" if zd else "قطعی کوتاه هنگام جابه‌جایی نسخه",
            "rollback": "release فعلی در تاریخچه می‌ماند و قابل بازگشت است"}


@tool(Risk.MEDIUM, "دیپلوی نسخه جدید", impact=_deploy_impact,
      describe=lambda a: f"دیپلوی پوشه `{a.get('path')}` روی برنامه «{a.get('app')}»")
async def deploy_project(
    ctx: Ctx, app: App,
    path: Annotated[str, Field(description="Local path of the project folder")],
    platform: Annotated[str | None, Field(description="Override detected platform")] = None,
    port: Annotated[int | None, Field(description="Override detected port")] = None,
    message: Annotated[str | None, Field(description="Release message")] = None,
) -> dict:
    """Package the folder (respecting .liaraignore/.gitignore, never uploading .env), upload it, create a
    release and follow the build until READY/FAILED/UNHEALTHY. Returns build errors if it fails."""
    _mark_action(ctx, app)
    try:
        res = await pipeline.deploy(ctx, app, path, platform, port, message)
    except packager.PackagingError as e:
        return {"error": str(e)}
    if res.get("success"):
        res["url"] = default_url(app)
        res["probe"] = await probe(res["url"], n=2)
    return res


async def _env_impact(ctx: Ctx, a: dict) -> dict:
    return {"downtime": "برنامه بعد از تغییر متغیرها ری‌استارت می‌شود",
            "rollback": "مقادیر قبلی در لیارا باقی می‌مانند مگر این‌که همان کلید بازنویسی شود",
            "note": "مقادیر برای امنیت نمایش داده نمی‌شوند: " + ", ".join(a.get("variables", {}).keys())}


@tool(Risk.MEDIUM, "تنظیم متغیرهای محیطی", impact=_env_impact,
      describe=lambda a: f"تنظیم متغیرهای {', '.join(a.get('variables', {}).keys())} روی «{a.get('app')}»")
async def set_env_vars(
    ctx: Ctx, app: App,
    variables: Annotated[dict[str, str], Field(description="KEY -> value. Use the value 'ASK_USER' for secrets "
                                                         "(passwords, DB URLs, API keys): the UI then asks the user "
                                                         "privately and the value never reaches the LLM. Never "
                                                         "invent values.")],
) -> dict:
    """Add or update environment variables (merged with existing ones). The app restarts afterwards."""
    variables = dict(variables)
    for k, v in variables.items():
        if v == ASK_USER:
            if not ctx.ask_secret:
                return {"error": f"امکان دریافت امن مقدار {k} در این رابط وجود ندارد"}
            val = await ctx.ask_secret(k)
            if not val:
                return {"error": f"مقدار {k} وارد نشد؛ تغییری اعمال نشد"}
            variables[k] = val.strip()
    bad = [k for k, v in variables.items() if not v or "<" in v or "از کاربر" in v]
    if bad:
        return {"error": f"مقدار واقعی این متغیرها مشخص نیست: {bad}. از کاربر بپرسید یا ASK_USER بدهید."}
    current = parse.env_list(await ctx.client.get_app(app))
    merged = {e["key"]: e["value"] for e in current}
    merged.update(variables)
    _mark_action(ctx, app)
    await ctx.client.update_envs(app, [{"key": k, "value": v} for k, v in merged.items()])
    return {"updated_keys": list(variables.keys()), "total_keys": len(merged), "restarting": True,
            "note": "values are not shown"}


@tool(Risk.MEDIUM, "تغییر پورت برنامه",
      impact=lambda ctx, a: _const({"downtime": "استقرار مجدد با پورت جدید"}),
      describe=lambda a: f"ثبت پورت {a.get('port')} برای «{a.get('app')}»")
async def update_port(ctx: Ctx, app: App, port: Annotated[int, Field(ge=1, le=65535)]) -> dict:
    """Change the port Liara routes traffic to (must match the port the app listens on)."""
    _mark_action(ctx, app)
    return await ctx.client.update_port(app, port)


@tool(Risk.MEDIUM, "استقرار بدون قطعی",
      describe=lambda a: f"{'فعال' if a.get('enabled') else 'غیرفعال'}‌سازی zero-downtime برای «{a.get('app')}»")
async def set_zero_downtime(ctx: Ctx, app: App, enabled: bool = True) -> dict:
    """Enable/disable zero-downtime deployment (recommended together with a health check)."""
    info = parse.app_summary(await ctx.client.get_app(app))
    return await ctx.client.set_setting(info["id"], "zero-downtime", enabled)


@tool(Risk.MEDIUM, "اتصال دامنه",
      impact=lambda ctx, a: _const({"downtime": "ندارد", "note": "بعد از اتصال باید رکوردهای DNS تنظیم شوند"}),
      describe=lambda a: f"اتصال دامنه {a.get('domain')} به «{a.get('app')}»")
async def add_domain(ctx: Ctx, app: App, domain: Annotated[str, Field(description="e.g. example.ir")]) -> dict:
    """Attach a custom domain to an app. Then show the user the DNS records Liara needs (use check_domain)."""
    return await ctx.client.add_domain(domain, app)


@tool(Risk.MEDIUM, "ساخت زون DNS",
      describe=lambda a: f"ساخت زون DNS لیارا برای {a.get('zone')}")
async def create_dns_zone(ctx: Ctx, zone: Annotated[str, Field(description="e.g. example.ir")]) -> dict:
    """Create a Liara DNS zone. Afterwards call get_dns_zone and tell the user which nameservers to set at
    the registrar (nic.ir has no API, so this step is manual)."""
    return await ctx.client.create_zone(zone)


@tool(Risk.MEDIUM, "ساخت رکورد DNS",
      describe=lambda a: f"ساخت رکورد {a.get('type')} «{a.get('name')}» → {a.get('value')} در {a.get('zone')}")
async def create_dns_record(
    ctx: Ctx, zone: str,
    type: Literal["A", "AAAA", "ALIAS", "CNAME", "MX", "TXT"],  # noqa: A002
    name: Annotated[str, Field(description="'@' for root, or a subdomain like 'www'")],
    value: Annotated[str, Field(description="IP for A/AAAA, host for CNAME/ALIAS/MX, text for TXT")],
    ttl: int = 3600,
    priority: int | None = None,
) -> dict:
    """Create a DNS record in a Liara DNS zone."""
    if type in ("A", "AAAA"):
        contents = [{"ip": value}]
    elif type in ("CNAME", "ALIAS"):
        contents = [{"host": value}]
    elif type == "MX":
        contents = [{"host": value, "priority": str(priority or 10)}]
    else:
        contents = [{"text": value}]
    return await ctx.client.create_record(zone, {"name": name, "type": type, "ttl": ttl, "contents": contents})


# ======================================================================= HIGH
async def _const(d: dict) -> dict:
    return d


async def _plan_cost(ctx: Ctx, plan_id: str) -> dict | None:
    return parse.plan_by_id(await get_plans(ctx), plan_id)


async def _create_impact(ctx: Ctx, a: dict) -> dict:
    p = await _plan_cost(ctx, a.get("plan_id", ""))
    cost = f"حدود {p['monthly_toman']:,} تومان در ماه ({p['ram_gb']}GB رم، {p['cpu_cores']} هسته)" if p else "نامشخص"
    return {"cost": cost + f" + بسته ویژگی {a.get('bundle_plan_id') or 'پیش‌فرض'}",
            "rollback": "برنامه را می‌توان از کنسول حذف کرد"}


@tool(Risk.HIGH, "ساخت برنامه جدید", impact=_create_impact,
      describe=lambda a: f"ساخت برنامه «{a.get('name')}» ({a.get('platform')}) با پلن {a.get('plan_id')}")
async def create_app(
    ctx: Ctx,
    name: Annotated[str, Field(description="Unique app name: lowercase letters, digits, dashes")],
    plan_id: Annotated[str, Field(description="Hardware plan id from list_plans/recommend_plan")],
    platform: Annotated[str, Field(description="node, next, react, vue, angular, static, django, flask, "
                                               "python, laravel, php, go, dotnet, docker")],
    bundle_plan_id: Annotated[str | None, Field(description="Feature bundle: free, basic, standard, pro")] = None,
) -> dict:
    """Create a new app on Liara (costs money)."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,30}", name):
        return {"error": "نام برنامه فقط حروف کوچک انگلیسی، عدد و خط تیره (۳ تا ۳۱ کاراکتر)"}
    res = await ctx.client.create_app(name, plan_id, platform, bundle_plan_id)
    return {"created": name, "url": default_url(name), "response": res}


@tool(Risk.HIGH, "ری‌استارت برنامه",
      impact=lambda ctx, a: _const({"downtime": "چند ثانیه تا یک دقیقه",
                                    "rollback": "اثر ماندگاری ندارد؛ حافظه آزاد می‌شود"}),
      describe=lambda a: f"ری‌استارت برنامه «{a.get('app')}»")
async def restart_app(ctx: Ctx, app: App) -> dict:
    """Restart the app."""
    _mark_action(ctx, app)
    return {"restarted": app, "response": await ctx.client.restart_app(app)}


@tool(Risk.HIGH, "روشن کردن برنامه", impact=lambda ctx, a: _const({"cost": "هزینه ساعتی پلن دوباره محاسبه می‌شود"}),
      describe=lambda a: f"روشن کردن برنامه «{a.get('app')}»")
async def start_app(ctx: Ctx, app: App) -> dict:
    """Turn the app on (scale=1)."""
    _mark_action(ctx, app)
    return {"started": app, "response": await ctx.client.scale_app(app, True)}


@tool(Risk.HIGH, "خاموش کردن برنامه",
      impact=lambda ctx, a: _const({"downtime": "وب‌سایت تا روشن شدن دوباره در دسترس نیست"}),
      describe=lambda a: f"خاموش کردن برنامه «{a.get('app')}»")
async def stop_app(ctx: Ctx, app: App) -> dict:
    """Turn the app off (scale=0). The site goes offline."""
    _mark_action(ctx, app)
    return {"stopped": app, "response": await ctx.client.scale_app(app, False)}


async def _resize_impact(ctx: Ctx, a: dict) -> dict:
    plans = await get_plans(ctx)
    new = parse.plan_by_id(plans, a.get("plan_id"))
    cur_id = parse.app_summary(await ctx.client.get_app(a["app"])).get("plan_id")
    cur = parse.plan_by_id(plans, cur_id)
    if new and cur:
        diff = new["monthly_toman"] - cur["monthly_toman"]
        cost = (f"{cur['id']} ({cur['ram_gb']}GB/{cur['cpu_cores']}c) → {new['id']} ({new['ram_gb']}GB/"
                f"{new['cpu_cores']}c): {'+' if diff >= 0 else ''}{diff:,} تومان در ماه")
    else:
        cost = "قیمت پلن پیدا نشد"
    return {"cost": cost, "downtime": "برنامه با منابع جدید ری‌استارت می‌شود",
            "rollback": f"بازگشت به پلن {cur_id} با همین ابزار"}


@tool(Risk.HIGH, "تغییر پلن (منابع)", impact=_resize_impact,
      describe=lambda a: f"تغییر پلن «{a.get('app')}» به {a.get('plan_id')}")
async def resize_app(ctx: Ctx, app: App, plan_id: Annotated[str, Field(description="Target plan id")]) -> dict:
    """Change the app's hardware plan (vertical scaling). Changes the monthly cost."""
    _mark_action(ctx, app)
    return {"resized": app, "plan_id": plan_id, "response": await ctx.client.resize_app(app, plan_id)}


@tool(Risk.HIGH, "بازگشت به release قبلی",
      impact=lambda ctx, a: _const({"downtime": "جابه‌جایی کوتاه نسخه",
                                    "rollback": "release فعلی در تاریخچه می‌ماند",
                                    "note": "این endpoint در OpenAPI رسمی لیارا نیست (⚠️)؛ اگر خطا داد، سورس "
                                            "نسخه قبلی را دوباره دیپلوی کنید"}),
      describe=lambda a: f"بازگشت «{a.get('app')}» به release {a.get('release_id')}")
async def rollback_release(ctx: Ctx, app: App, release_id: Annotated[str, Field(description="Previous READY "
                                                                                           "release id")]) -> dict:
    """Roll back to a previous release."""
    _mark_action(ctx, app)
    try:
        return {"rolled_back_to": release_id, "response": await ctx.client.rollback(app, release_id)}
    except LiaraError as e:
        return {"error": e.friendly, "limitation": "Rollback API در دسترس نیست؛ نسخه قبلی را دوباره دیپلوی کنید "
                                                   "(deploy_project با سورس قبلی)."}


# ==================================================================== BLOCKED
@tool(Risk.BLOCKED, "حذف برنامه")
async def delete_app(ctx: Ctx, app: App) -> dict:
    """Delete an app. Blocked for safety: the assistant never deletes resources."""
    return {"error": "blocked"}
