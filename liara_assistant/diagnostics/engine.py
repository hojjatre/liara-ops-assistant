"""Diagnosis: evidence -> signals -> ranked hypotheses -> proposed actions; plus verification."""
from __future__ import annotations

import time

from ..probe import default_url, probe
from . import evidence as ev_mod
from . import signals as sig_mod


def _ids(signals: list[dict]) -> set[str]:
    return {s["id"] for s in signals}


def _get(signals: list[dict], sid: str) -> dict | None:
    return next((s for s in signals if s["id"] == sid), None)


def next_plan(plans: list[dict], current_id: str | None) -> dict | None:
    from ..liara import parse
    cur = parse.plan_by_id(plans, current_id)
    if not cur:
        return None
    bigger = [p for p in parse.deployable_plans(plans) if (p["ram_gb"] or 0) > (cur["ram_gb"] or 0)]
    return bigger[0] if bigger else None


def hypotheses(signals: list[dict], ev: dict, plans: list[dict] | None = None) -> list[dict]:
    ids = _ids(signals)
    app = ev["app"]
    plan_id = (ev.get("plan") or {}).get("id")
    H: list[dict] = []

    def h(hid, title, score, evidence, actions=None, manual=None):
        H.append({"id": hid, "title_fa": title, "score": round(min(score, 0.99), 2),
                  "evidence": [s for s in evidence if s in ids],
                  "proposed_actions": actions or [], "manual_steps": manual or []})

    if "app_stopped" in ids:
        h("app_off", "برنامه خاموش است", 0.95, ["app_stopped", "probe_502", "probe_down"],
          [{"tool": "start_app", "args": {"app": app}}])

    if "missing_env" in ids:
        names = _get(signals, "missing_env").get("names", [])
        score = 0.7 + 0.1 * bool(ids & {"probe_5xx", "error_logs"}) + 0.1 * ("errors_after_release" in ids)
        h("missing_env", f"متغیر محیطی لازم تنظیم نشده ({', '.join(names)})", score,
          ["missing_env", "probe_5xx", "error_logs", "errors_after_release"],
          [{"tool": "set_env_vars", "args": {"app": app, "variables": {n: "<از کاربر بپرسید>" for n in names}}}],
          ["مقدار را حدس نزنید؛ با ASK_USER رابط کاربری مقدار را مستقیماً و امن از کاربر می‌گیرد"])

    if "log_db_connection" in ids:
        h("db_connection", "اتصال به دیتابیس/سرویس خارجی برقرار نمی‌شود", 0.75 + 0.1 * ("probe_5xx" in ids),
          ["log_db_connection", "probe_5xx", "error_logs"], [],
          ["وضعیت سرویس دیتابیس را در لیارا بررسی کنید", "متغیرهای DATABASE_URL / DB_HOST / رمز را بررسی کنید",
           "اگر دیتابیس در شبکه خصوصی است، برنامه و دیتابیس باید در یک شبکه باشند"])

    if ids & {"errors_after_release", "release_failed"}:
        prev = _get(signals, "_prev_release")
        acts = [{"tool": "rollback_release", "args": {"app": app, "release_id": prev["release"]["id"]}}] if prev else []
        h("regression", "release جدید مشکل ایجاد کرده (regression)", 0.6 + 0.15 * ("probe_5xx" in ids)
          + 0.1 * ("release_failed" in ids), ["errors_after_release", "release_failed", "recent_release",
                                               "probe_5xx"], acts,
          ["اگر علت دقیق‌تری (مثل env یا وابستگی) پیدا شد، به‌جای rollback آن را اصلاح و دوباره دیپلوی کنید"])

    if ids & {"mem_high", "log_oom", "mem_growing", "mem_peak"}:
        score = 0.45 + 0.25 * ("mem_high" in ids) + 0.2 * ("log_oom" in ids) + 0.1 * ("mem_growing" in ids) \
            + 0.1 * ("restarts" in ids) + 0.05 * ("probe_slow" in ids)
        bigger = next_plan(plans or [], plan_id)
        acts = [{"tool": "restart_app", "args": {"app": app}, "note": "راه‌حل موقت: آزادکردن حافظه"}]
        if bigger:
            acts.append({"tool": "resize_app", "args": {"app": app, "plan_id": bigger["id"]},
                         "note": f"پلن بزرگ‌تر: {bigger['ram_gb']}GB رم"})
        h("memory", "کمبود حافظه / نشت حافظه", score,
          ["mem_high", "log_oom", "mem_growing", "mem_peak", "restarts", "probe_slow"], acts,
          ["اگر رم مدام بالا می‌رود (mem_growing)، کد را برای نشت حافظه بررسی کنید (کش بی‌حد، آرایه‌های سراسری)"])

    if "cpu_saturated" in ids:
        bigger = next_plan(plans or [], plan_id)
        h("cpu", "CPU به سقف پلن رسیده (throttling)", 0.65 + 0.1 * ("probe_slow" in ids),
          ["cpu_saturated", "probe_slow"],
          [{"tool": "resize_app", "args": {"app": app, "plan_id": bigger["id"]}}] if bigger else [],
          ["مسیرهای پرهزینه (حلقه‌های سنگین، پردازش همگام) را بهینه یا به صف منتقل کنید"])

    if "log_module_missing" in ids:
        h("dependency", "وابستگی/ماژول نصب نشده", 0.8, ["log_module_missing", "release_failed", "probe_5xx"], [],
          ["وابستگی را به package.json / requirements.txt اضافه و دوباره دیپلوی کنید"])

    if ids & {"port_mismatch", "probe_502"} and "app_stopped" not in ids:
        pm = _get(signals, "port_mismatch")
        acts = [{"tool": "update_port", "args": {"app": app, "port": pm["app_ports"][0]}}] if pm else []
        h("port", "پورت برنامه با پورت ثبت‌شده در لیارا یکی نیست / برنامه بالا نیامده",
          0.8 if pm else 0.45, ["port_mismatch", "probe_502", "release_failed"], acts,
          ["پورت را در liara.json و کد یکسان کنید", "health check را بررسی کنید"])

    if ids & {"disk_full", "log_disk_full"}:
        h("disk", "فضای دیسک تمام شده", 0.8, ["disk_full", "log_disk_full"], [],
          ["فایل‌های اضافی/لاگ‌ها را پاک کنید یا دیسک را بزرگ کنید"])

    if "log_timeout" in ids or ("probe_slow" in ids and not ids & {"mem_high", "cpu_saturated"}):
        h("slow_dependency", "کندی از وابستگی بیرونی یا کوئری‌های کند (منابع سالم‌اند)",
          0.5 + 0.1 * ("log_timeout" in ids), ["probe_slow", "log_timeout"], [],
          ["کوئری‌های کند دیتابیس و فراخوانی‌های API بیرونی را بررسی کنید",
           "برای فایل‌های استاتیک از CDN استفاده کنید", "کش (Redis) برای پاسخ‌های پرتکرار"])

    if "error_logs" in ids and not H:
        top = (_get(signals, "error_logs").get("top") or [{}])[0]
        h("app_exception", "خطای کد برنامه (exception)", 0.6, ["error_logs", "probe_5xx"], [],
          [f"پرتکرارترین خطا: {top.get('example', '')[:200]}", "کد را اصلاح و دوباره دیپلوی کنید"])

    H.sort(key=lambda x: x["score"], reverse=True)
    return H[:4]


def _baseline(ev: dict, signals: list[dict]) -> dict:
    m = ev.get("metrics") or {}
    pr = ev.get("probe") or {}
    errs = next((s for s in signals if s["id"] == "error_logs"), None)
    return {"ts": ev["collected_at"], "statuses": pr.get("statuses"), "median_ms": pr.get("median_ms"),
            "memory_pct": m.get("memory_pct_now"), "cpu_pct": m.get("cpu_pct_now"),
            "error_count": errs["count"] if errs else 0, "window_minutes": ev.get("window_minutes")}


async def diagnose(ctx, app: str, symptom: str = "", minutes: int = 60, url: str | None = None) -> dict:
    await ctx.report(f"جمع‌آوری شواهد برای «{app}» (وضعیت، release‌ها، رویدادها، لاگ‌ها، متریک‌ها، تست HTTP)")
    ev = await ev_mod.collect(ctx, app, minutes=minutes, url=url)
    sigs = sig_mod.extract(ev)
    await ctx.report(f"{len([s for s in sigs if not s['id'].startswith('_')])} نشانه استخراج شد")
    plans = (ctx.state.get("cache", {}).get("plans") or (0, []))[1]
    hyps = hypotheses(sigs, ev, plans)
    base = _baseline(ev, sigs)
    ctx.state.setdefault("baselines", {})[app] = base
    m = ev["metrics"]
    return {
        "app": app,
        "symptom": symptom,
        "window_minutes": minutes,
        "app_info": ev["app_info"],
        "plan": ev["plan"],
        "probe": ev["probe"],
        "metrics_now": {"memory_mb": m["memory_mb_now"], "memory_pct": m["memory_pct_now"],
                        "cpu_pct": m["cpu_pct_now"], "disks": m["disks"],
                        "memory_peak_mb": max((v for _, v in m["memory_series_mb"]), default=None),
                        "cpu_peak_pct": max((v for _, v in m["cpu_series_pct"]), default=None)},
        "latest_releases": ((ev.get("releases") or {}).get("releases") or [])[:3],
        "recent_events": [e["text"] for e in ev["events"][-8:]],
        "signals": [{k: v for k, v in s.items() if k not in ("release",)} for s in sigs if not s["id"].startswith("_")],
        "hypotheses": hyps,
        "unavailable_sources": ev["unavailable"],
        "baseline_saved": base,
        "log_lines_analyzed": len(ev["logs"]),
    }


async def health(ctx, app: str, url: str | None = None) -> dict:
    """Fast health snapshot used by the monitor loop and the status panel."""
    ev = await ev_mod.collect(ctx, app, minutes=10, url=url)
    sigs = sig_mod.extract(ev)
    bad = [s for s in sigs if s["severity"] in ("critical", "warning") and not s["id"].startswith("_")]
    return {"app": app, "healthy": not any(s["severity"] == "critical" for s in bad),
            "app_info": ev["app_info"], "probe": ev["probe"], "metrics": {
                "memory_pct": ev["metrics"]["memory_pct_now"], "memory_mb": ev["metrics"]["memory_mb_now"],
                "cpu_pct": ev["metrics"]["cpu_pct_now"], "disks": ev["metrics"]["disks"]},
            "issues": [{"id": s["id"], "severity": s["severity"], "title_fa": s["title_fa"]} for s in bad],
            "unavailable_sources": ev["unavailable"]}


async def verify(ctx, app: str, url: str | None = None, settle_sec: int = 30) -> dict:
    """Re-check after a fix and compare with the saved baseline."""
    base = (ctx.state.get("baselines") or {}).get(app)
    since = (ctx.state.get("last_action_ts") or {}).get(app) or (base or {}).get("ts") or time.time() - 600
    await ctx.report("صبر برای پایدار شدن سرویس...")
    deadline = time.time() + 180
    info = {}
    while time.time() < deadline:
        try:
            from ..liara import parse
            info = parse.app_summary(await ctx.client.get_app(app))
            if info.get("running"):
                break
        except Exception:  # noqa: BLE001
            pass
        await _sleep(5)
    await _sleep(settle_sec)
    await ctx.report("تست مجدد HTTP، متریک‌ها و لاگ‌های بعد از اقدام")
    pr = await probe(url or default_url(app), n=3)
    minutes = max(1, int((time.time() - since) / 60) + 1)
    ev = await ev_mod.collect(ctx, app, minutes=minutes, url=url)
    new_logs = [line for line in ev["logs"] if (line["ts"] or 0) >= since - 5]
    new_errors = sig_mod.error_lines(new_logs)
    # Liara's metrics lag behind (the summary still shows the old container right after a restart),
    # so judge resources only from samples taken after the action; otherwise report them as "not updated yet".
    ram_mb = (ev.get("plan") or {}).get("ram_mb")
    fresh_mem = [v for t, v in ev["metrics"]["memory_series_mb"] if t >= since + 45]
    fresh_cpu = [v for t, v in ev["metrics"]["cpu_series_pct"] if t >= since + 45]
    mem_pct = round(100 * fresh_mem[-1] / ram_mb, 1) if fresh_mem and ram_mb else None
    cpu_pct = fresh_cpu[-1] if fresh_cpu else None
    after = {"statuses": pr["statuses"], "median_ms": pr["median_ms"],
             "memory_pct": mem_pct, "cpu_pct": cpu_pct,
             "metrics_summary_memory_pct": ev["metrics"]["memory_pct_now"],
             "new_error_lines": len(new_errors)}
    if mem_pct is None and cpu_pct is None:
        after["metrics_note"] = ("متریک‌های لیارا هنوز بعد از اقدام به‌روز نشده‌اند (چند دقیقه تأخیر دارند)؛ "
                                 "چند دقیقه بعد دوباره check_health بزنید")
    healthy_http = pr["healthy"]
    resources_ok = (mem_pct or 0) < 85 and (cpu_pct or 0) < 90
    if healthy_http and not new_errors and resources_ok:
        verdict, verdict_fa = "fixed", "مشکل برطرف شد"
    elif healthy_http and (new_errors or not resources_ok):
        verdict, verdict_fa = "partial", "سایت پاسخ می‌دهد ولی هنوز نشانه مشکل هست"
    else:
        verdict, verdict_fa = "not_fixed", "مشکل برطرف نشد — فرضیه بعدی را بررسی کنید"
    return {"app": app, "verdict": verdict, "verdict_fa": verdict_fa, "before": base, "after": after,
            "app_running": info.get("running"), "top_new_errors": sig_mod.top_errors(new_logs, 3),
            "unavailable_sources": ev["unavailable"]}


async def _sleep(sec: float) -> None:
    import asyncio
    await asyncio.sleep(sec)
