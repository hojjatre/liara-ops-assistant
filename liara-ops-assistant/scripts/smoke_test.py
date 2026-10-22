"""Smoke test: checks your .env against the real Liara API and BoxAPI.

    python scripts/smoke_test.py            # everything
    python scripts/smoke_test.py --app my-app
    python scripts/smoke_test.py --skip-llm

Prints raw samples of metrics/logs so metric units can be checked, and tests which LLM models
support tool calling. Read-only: it never changes anything on Liara.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from liara_assistant.config import Settings  # noqa: E402
from liara_assistant.liara import parse  # noqa: E402
from liara_assistant.liara.client import LiaraClient, LiaraError  # noqa: E402

CANDIDATES = ["gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-astra", "gpt-6-luna",
              "gpt-5.6-luna", "gpt-5-mini", "gpt-5.4-mini"]

OK, FAIL = "✅", "❌"


def show(label, ok, detail=""):
    print(f"{OK if ok else FAIL} {label}" + (f" — {detail}" if detail else ""))


async def liara_checks(s: Settings, app: str | None):
    print("\n=== Liara API ===")
    c = LiaraClient(s.liara_api_token, s.liara_team_id, s.liara_api_url, s.liara_dns_url)
    try:
        try:
            me = await c.me()
            plans = parse.deployable_plans(parse.plans(me))
            show("GET /v1/me", True, f"{len(plans)} deployable plans: "
                 + ", ".join(f"{p['id']}({p['ram_gb']}GB, {p['monthly_toman']:,} T/mo)" for p in plans[:6]))
        except LiaraError as e:
            show("GET /v1/me", False, e.friendly)
            return
        apps = parse.apps_list(await c.list_apps())
        show("GET /v1/projects", True, f"{len(apps)} apps: " + ", ".join(a["name"] for a in apps[:10]))
        app = app or s.default_app or (apps[0]["name"] if apps else None)
        if not app:
            print("   (no app yet — create one to test logs/metrics)")
            return
        print(f"\n--- app: {app}")
        now = int(time.time())
        checks = {
            "app": c.get_app(app),
            "releases": c.list_releases(app, 1, 3),
            "summary": c.metrics_summary(app),
            "memory": c.metrics_series(app, "memory", now - 3600),
            "cpu": c.metrics_series(app, "cpu", now - 3600),
            "events": c.events(app, now - 3600, now),
            "logs_v2": c.logs(app, now - 3600),
        }
        for name, coro in checks.items():
            try:
                data = await coro
                sample = json.dumps(data, ensure_ascii=False)[:300]
                show(name, True, sample)
            except LiaraError as e:
                show(name, False, e.friendly)
        try:
            summary = await c.metrics_summary(app)
            info = parse.app_summary(await c.get_app(app))
            plan = parse.plan_by_id(parse.plans(me), info["plan_id"]) or {}
            raw_mem = parse.summary_value(summary.get("memoryUsage"))
            raw_cpu = parse.summary_value(summary.get("cpuUsage"))
            print(f"\n   Metric units check: raw memory={raw_mem} -> {parse.memory_mb(raw_mem)} MB; "
                  f"raw cpu={raw_cpu} -> {parse.cpu_percent(raw_cpu, plan.get('cpu_cores'))}% of "
                  f"{plan.get('cpu_cores')} cores. Compare with the Liara console graphs.")
        except LiaraError:
            pass
        try:
            zones = await c.list_zones()
            show("DNS zones", True, json.dumps(zones, ensure_ascii=False)[:200])
        except LiaraError as e:
            show("DNS zones", False, e.friendly)
    finally:
        await c.aclose()


TOOL = {"type": "function", "function": {
    "name": "get_app_status", "description": "Get the status of a Liara app",
    "parameters": {"type": "object", "properties": {"app": {"type": "string"}}, "required": ["app"]}}}


async def llm_checks(s: Settings):
    print("\n=== BoxAPI LLM ===")
    headers = {"Authorization": f"Bearer {s.llm_api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=90, headers=headers) as http:
        try:
            r = await http.get(f"{s.llm_base_url}/models")
            names = [m.get("id") for m in r.json().get("data", [])] if r.status_code == 200 else []
            show("GET /models", r.status_code == 200, f"{len(names)} models")
        except Exception as e:  # noqa: BLE001
            show("GET /models", False, str(e))
        working = []
        for model in CANDIDATES:
            body = {"model": model, "tools": [TOOL], "messages": [
                {"role": "system", "content": "You are a Liara DevOps assistant. Use tools when needed."},
                {"role": "user", "content": "وضعیت برنامه demo-shop چطوره؟"}]}
            t0 = time.time()
            try:
                r = await http.post(f"{s.llm_base_url}/chat/completions", json=body)
                dt = round(time.time() - t0, 1)
                if r.status_code != 200:
                    show(model, False, f"HTTP {r.status_code}: {r.text[:120]}")
                    continue
                msg = r.json()["choices"][0]["message"]
                calls = msg.get("tool_calls") or []
                ok = bool(calls) and calls[0]["function"]["name"] == "get_app_status"
                show(model, ok, f"{dt}s, tool_call={'yes' if ok else 'no'}")
                if ok:
                    working.append((model, dt))
            except Exception as e:  # noqa: BLE001
                show(model, False, f"{type(e).__name__}: {e}")
        if working:
            print(f"\nModels with tool calling, in preference order: {', '.join(m for m, _ in working)}")
            print(f"Suggested .env: LLM_MODEL={working[0][0]}  "
                  f"LLM_FALLBACK_MODELS={','.join(m for m, _ in working[1:3])}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app")
    ap.add_argument("--skip-llm", action="store_true")
    ap.add_argument("--skip-liara", action="store_true")
    a = ap.parse_args()
    s = Settings.from_env()
    if s.missing():
        print(f"{FAIL} Missing in .env: {', '.join(s.missing())}")
    print(f"team id: {s.liara_team_id or '(personal account)'} · model: {s.llm_model}")
    if not a.skip_liara and s.liara_api_token:
        await liara_checks(s, a.app)
    if not a.skip_llm and s.llm_api_key:
        await llm_checks(s)


if __name__ == "__main__":
    asyncio.run(main())
