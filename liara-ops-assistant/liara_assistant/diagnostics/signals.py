"""Deterministic signal extraction: turns raw evidence into explainable findings."""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

ERROR_RE = re.compile(r"(?i)\b(error|exception|traceback|fatal|panic|unhandled|critical|uncaught)\b"
                      r"|\b(GET|POST|PUT|PATCH|DELETE)\b[^\n]{0,120}\s5\d\d\b")
NOT_ERROR_RE = re.compile(r"(?i)\b0 errors?\b|no errors?|error_log|errorhandler")

PATTERNS: dict[str, re.Pattern] = {
    "oom": re.compile(r"(?i)OOMKilled|out of memory|heap out of memory|MemoryError|Cannot allocate memory|"
                      r"Allowed memory size of \d+ bytes exhausted"),
    "db_connection": re.compile(r"(?i)ECONNREFUSED|could not connect to server|connection refused|"
                                r"getaddrinfo (ENOTFOUND|EAI_AGAIN)|SequelizeConnection\w*|MongoNetworkError|"
                                r"MongoServerSelectionError|OperationalError|password authentication failed|"
                                r"Access denied for user|SQLSTATE\[HY000\] \[2002\]|too many connections"),
    "module_missing": re.compile(r"(?i)Cannot find module|ModuleNotFoundError|No module named|ImportError|"
                                 r"Class \"?[\w\\]+\"? not found|undefined method"),
    "port": re.compile(r"(?i)EADDRINUSE|address already in use"),
    "disk_full": re.compile(r"(?i)ENOSPC|No space left on device|disk quota exceeded"),
    "timeout": re.compile(r"(?i)WORKER TIMEOUT|worker timeout|ETIMEDOUT|timed out|504 Gateway"),
    "restart": re.compile(r"(?i)\b(restart(ed|ing)?|crash(ed)?|back-?off|exited with code|killed|"
                          r"container (died|stopped))\b"),
}

ENV_PATTERNS = [
    re.compile(r"\b([A-Z][A-Z0-9_]{2,})['\"`]?\s+(?:env(?:ironment)?\s+)?(?:var(?:iable)?\s+)?"
               r"(is not set|is missing|is undefined|is required|is not defined|not defined|must be set|"
               r"not configured|is empty)"),
    re.compile(r"(?i)(missing|undefined|required|unset)\s+(env(ironment)?\s+)?(var(iable)?\s+)?[\"'`]?"
               r"([A-Z][A-Z0-9_]{2,})"),
    re.compile(r"KeyError: ['\"]([A-Z][A-Z0-9_]{2,})['\"]"),
    re.compile(r"process\.env\.([A-Z][A-Z0-9_]{2,})"),
]
LISTEN_RE = re.compile(r"(?i)(listening|running|started|server)\D{0,30}(port|:)\s*(\d{2,5})")

_NORMALISE = [
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ][\d:.]+Z?"), "<ts>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-f]{12,}\b"), "<hex>"),
    (re.compile(r"\d+"), "<n>"),
    (re.compile(r"'[^']{0,80}'|\"[^\"]{0,80}\""), "<s>"),
]


def signature(line: str) -> str:
    s = line.strip()
    for pat, rep in _NORMALISE:
        s = pat.sub(rep, s)
    return re.sub(r"\s+", " ", s)[:150]


def _fmt_ts(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else None


def _parse_iso(v) -> float | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def error_lines(logs: list[dict]) -> list[dict]:
    return [line for line in logs if ERROR_RE.search(line["line"]) and not NOT_ERROR_RE.search(line["line"])]


def top_errors(logs: list[dict], k: int = 5) -> list[dict]:
    errs = error_lines(logs)
    counts: Counter[str] = Counter()
    first: dict[str, dict] = {}
    last: dict[str, float | None] = {}
    for e in errs:
        sig = signature(e["line"])
        counts[sig] += 1
        first.setdefault(sig, e)
        last[sig] = e["ts"]
    return [{"signature": sig, "count": n, "example": first[sig]["line"][:300],
             "first_seen": _fmt_ts(first[sig]["ts"]), "last_seen": _fmt_ts(last[sig]),
             "first_ts": first[sig]["ts"]} for sig, n in counts.most_common(k)]


def extract(ev: dict) -> list[dict]:
    S: list[dict] = []

    def add(sid: str, severity: str, title: str, detail: str = "", **data):
        S.append({"id": sid, "severity": severity, "title_fa": title, "detail": detail, **data})

    info = ev.get("app_info") or {}
    m = ev.get("metrics") or {}
    pr = ev.get("probe") or {}
    logs = ev.get("logs") or []
    events = ev.get("events") or []
    rel = (ev.get("releases") or {}).get("releases") or []
    plan = ev.get("plan") or {}

    # --- app state
    if info.get("running") is False:
        add("app_stopped", "critical", "برنامه خاموش است (scale=0)")
    if info.get("is_deployed") is False:
        add("not_deployed", "critical", "هنوز هیچ نسخه‌ای روی این برنامه مستقر نشده")

    # --- probe
    statuses = [s for s in pr.get("statuses", []) if s]
    if pr:
        if any(s >= 500 for s in statuses):
            add("probe_5xx", "critical", f"وب‌سایت خطای سرور برمی‌گرداند ({', '.join(map(str, statuses))})",
                pr.get("sample_body") or "", statuses=statuses)
        if not statuses and pr.get("errors"):
            add("probe_down", "critical", "وب‌سایت پاسخ نمی‌دهد", "; ".join(pr["errors"]))
        if 502 in statuses or 503 in statuses:
            add("probe_502", "warning", "خطای 502/503: برنامه پشت پروکسی لیارا در دسترس نیست "
                                        "(خاموش، کرش یا پورت اشتباه)")
        if pr.get("median_ms") and pr["median_ms"] > 2000:
            add("probe_slow", "warning", f"پاسخ‌دهی کند است (میانه {pr['median_ms']}ms)", ms=pr["median_ms"])

    # --- resources
    mem_pct = m.get("memory_pct_now")
    ram_mb = plan.get("ram_mb")
    mem_series = m.get("memory_series_mb") or []
    peak_mem = max((v for _, v in mem_series), default=None)
    if mem_pct is not None and mem_pct >= 90:
        add("mem_high", "critical", f"مصرف رم {mem_pct}% از سقف پلن", value=mem_pct)
    elif mem_pct is not None and mem_pct >= 80:
        add("mem_high", "warning", f"مصرف رم {mem_pct}% از سقف پلن", value=mem_pct)
    elif peak_mem and ram_mb and peak_mem / ram_mb >= 0.9:
        add("mem_peak", "warning", f"اوج مصرف رم در بازه {round(100 * peak_mem / ram_mb)}% سقف پلن بوده")
    if len(mem_series) >= 6:
        first = sum(v for _, v in mem_series[:3]) / 3
        last = sum(v for _, v in mem_series[-3:]) / 3
        if first > 0 and last / first >= 1.3:
            add("mem_growing", "warning", f"رم به‌صورت پیوسته بالا رفته ({round(first)}MB → {round(last)}MB)؛ "
                                          "الگوی نشت حافظه", first_mb=round(first), last_mb=round(last))
    cpu_series = [v for _, v in m.get("cpu_series_pct") or []]
    cpu_now = m.get("cpu_pct_now")
    if cpu_series and sum(1 for v in cpu_series if v >= 90) / len(cpu_series) >= 0.5:
        add("cpu_saturated", "critical", "CPU در بیشتر بازه نزدیک سقف پلن بوده (throttling)")
    elif cpu_now is not None and cpu_now >= 90:
        add("cpu_saturated", "warning", f"مصرف فعلی CPU {cpu_now}% سقف پلن")
    for d in m.get("disks") or []:
        if d.get("percent") and d["percent"] >= 90:
            add("disk_full", "critical", f"دیسک {d['name']} {d['percent']}% پر است")

    # --- logs
    errs = top_errors(logs)
    all_err = error_lines(logs)
    if all_err:
        sev = "critical" if len(all_err) >= 10 or "probe_5xx" in {s["id"] for s in S} else "warning"
        add("error_logs", sev, f"{len(all_err)} خط خطا در لاگ‌های {ev.get('window_minutes')} دقیقه اخیر",
            top=errs, count=len(all_err))
    text = "\n".join(line["line"] for line in logs[-1500:])
    ev_text = "\n".join(e["text"] for e in events)
    for key in ("oom", "db_connection", "module_missing", "port", "disk_full", "timeout"):
        hit = PATTERNS[key].search(text) or (PATTERNS[key].search(ev_text) if key == "oom" else None)
        if hit:
            title = {
                "oom": "نشانه کمبود حافظه (OOM) در لاگ/رویدادها",
                "db_connection": "خطای اتصال به دیتابیس/سرویس خارجی در لاگ‌ها",
                "module_missing": "ماژول/وابستگی پیدا نشد",
                "port": "پورت در حال استفاده است",
                "disk_full": "فضای دیسک تمام شده",
                "timeout": "timeout در پردازش درخواست‌ها",
            }[key]
            add(f"log_{key}", "critical" if key != "timeout" else "warning", title, hit.group(0)[:160])

    env_keys = set(info.get("env_keys") or [])
    missing = []
    for pat in ENV_PATTERNS:
        for mt in pat.finditer(text):
            name = next((g for g in reversed(mt.groups()) if g and re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", g)), None)
            if name and name not in env_keys and name not in missing and name not in ("GET", "POST", "ERROR",
                                                                                     "FATAL", "HTTP", "NODE_ENV"):
                missing.append(name)
    if missing:
        add("missing_env", "critical", f"متغیر محیطی تنظیم نشده: {', '.join(missing[:4])}",
            "در لاگ‌ها به این متغیرها اشاره شده ولی روی برنامه تعریف نشده‌اند", names=missing[:4])

    listen = LISTEN_RE.findall(text)
    if listen and rel:
        ports = {int(p[2]) for p in listen}
        rel_port = rel[0].get("port")
        if rel_port and rel_port not in ports:
            add("port_mismatch", "critical", f"برنامه روی پورت {sorted(ports)} گوش می‌دهد ولی در لیارا "
                                             f"پورت {rel_port} ثبت شده", app_ports=sorted(ports), liara_port=rel_port)

    restarts = [e for e in events if PATTERNS["restart"].search(e["text"]) or PATTERNS["oom"].search(e["text"])]
    if len(restarts) >= 2:
        add("restarts", "warning", f"{len(restarts)} رویداد ری‌استارت/کرش در بازه", count=len(restarts))

    # --- releases
    if rel:
        latest = rel[0]
        if latest.get("state") in ("FAILED", "UNHEALTHY", "TIMEDOUT"):
            add("release_failed", "critical", f"آخرین release در وضعیت {latest['state']} است", release=latest)
        rel_ts = _parse_iso(latest.get("created_at"))
        if rel_ts and ev.get("collected_at") and ev["collected_at"] - rel_ts < 3 * 3600:
            mins = round((ev["collected_at"] - rel_ts) / 60)
            add("recent_release", "info", f"آخرین release حدود {mins} دقیقه پیش منتشر شده",
                release=latest, release_ts=rel_ts)
            first_err = min((e["first_ts"] for e in errs if e.get("first_ts")), default=None)
            if first_err and first_err >= rel_ts - 60:
                add("errors_after_release", "critical", "خطاها بعد از انتشار آخرین release شروع شده‌اند",
                    release=latest)
        if len(rel) > 1:
            prev_ready = next((r for r in rel[1:] if r.get("state") == "READY"), None)
            if prev_ready:
                S.append({"id": "_prev_release", "severity": "info", "title_fa": "release سالم قبلی",
                          "detail": prev_ready.get("id"), "release": prev_ready})
    return S
