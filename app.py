"""Chainlit UI for the Liara Ops Assistant.

Run:  chainlit run app.py -w
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

import chainlit as cl
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from liara_assistant.agent.graph import build_graph, tool_schemas
from liara_assistant.agent.llm import make_llm
from liara_assistant.config import Settings
from liara_assistant.deploy.packager import PackagingError, extract_zip
from liara_assistant.diagnostics import engine
from liara_assistant.diagnostics.evidence import usage_now
from liara_assistant.liara import parse
from liara_assistant.liara.client import LiaraClient, LiaraError
from liara_assistant.policy import AuditLog, safe_args
from liara_assistant.probe import default_url, probe
from liara_assistant.tools.registry import REGISTRY, RISK_FA, Ctx, Risk

TEAM = "Human in the Liara"

SETTINGS = Settings.from_env()
CHECKPOINTER = InMemorySaver()
_GRAPH = None


def graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph(make_llm(SETTINGS, tool_schemas()), CHECKPOINTER)
    return _GRAPH


# ------------------------------------------------------------------ starters
@cl.set_starters
async def starters():
    return [
        cl.Starter(label="برنامه‌های من", message="برنامه‌های من روی لیارا را با وضعیتشان نشان بده.",
                   icon="/public/icons/server.svg"),
        cl.Starter(label="سایتم خطای ۵۰۰ می‌دهد", message="سایتم خطای 500 می‌دهد. بررسی کن مشکل چیست.",
                   icon="/public/icons/alert.svg"),
        cl.Starter(label="سایتم کند شده", message="سایتم خیلی کند شده. علتش را پیدا کن.",
                   icon="/public/icons/gauge.svg"),
        cl.Starter(label="دیپلوی پروژه", message="می‌خواهم پروژه‌ام را روی لیارا دیپلوی کنم.",
                   icon="/public/icons/rocket.svg"),
    ]


# ------------------------------------------------------------------ session
@cl.on_chat_start
async def start():
    missing = SETTINGS.missing()
    if missing:
        await cl.Message(content="تنظیمات کامل نیست. این متغیرها را در فایل `.env` قرار دهید و برنامه را "
                                 f"دوباره اجرا کنید: `{', '.join(missing)}`").send()
        return
    client = LiaraClient(SETTINGS.liara_api_token, SETTINGS.liara_team_id, SETTINGS.liara_api_url,
                         SETTINGS.liara_dns_url)
    ctx = Ctx(client=client, settings=SETTINGS, ask_secret=ask_secret)
    ctx.state["audit"] = AuditLog(SETTINGS.audit_log_path)
    if SETTINGS.default_app:
        ctx.state["last_app"] = SETTINGS.default_app
    cl.user_session.set("ctx", ctx)
    cl.user_session.set("thread_id", str(uuid.uuid4()))
    cl.user_session.set("lock", asyncio.Lock())
    cl.user_session.set("alerts", {})
    cl.user_session.set("monitor", asyncio.create_task(monitor_loop()))
    await refresh_sidebar()


@cl.on_chat_end
async def end():
    task = cl.user_session.get("monitor")
    if task:
        task.cancel()
    ctx: Ctx | None = cl.user_session.get("ctx")
    if ctx:
        await ctx.client.aclose()


# ------------------------------------------------------------------ messages
@cl.on_message
async def on_message(msg: cl.Message):
    ctx: Ctx | None = cl.user_session.get("ctx")
    if ctx is None:
        await cl.Message(content="ابتدا فایل `.env` را تنظیم کنید.").send()
        return
    text = msg.content or ""
    for el in msg.elements or []:
        path = getattr(el, "path", None)
        if path and str(path).lower().endswith(".zip"):
            try:
                folder = extract_zip(path)
                ctx.state.setdefault("uploads", []).append(str(folder))
                text += f"\n\n[کاربر پروژه‌ای آپلود کرد؛ مسیر محلی: {folder}]"
            except (PackagingError, OSError) as e:
                await cl.Message(content=f"فایل zip باز نشد: {e}").send()
    await run_turn(text)


@cl.action_callback("diagnose")
async def on_diagnose(action: cl.Action):
    app = action.payload.get("app")
    issues = action.payload.get("issues", "")
    await run_turn(f"پایش خودکار برای برنامه «{app}» هشدار داد ({issues}). عیب‌یابی کن و راه‌حل پیشنهاد بده.")


# ------------------------------------------------------------------ the turn
async def run_turn(text: str) -> None:
    ctx: Ctx = cl.user_session.get("ctx")
    lock: asyncio.Lock = cl.user_session.get("lock")
    async with lock:
        config = {"configurable": {"thread_id": cl.user_session.get("thread_id"), "ctx": ctx,
                                   "ui_hooks": {"tool_start": tool_start, "tool_end": tool_end,
                                                "tool_skipped": tool_skipped}},
                  "recursion_limit": 60}
        payload: object = {"messages": [HumanMessage(content=text)]}
        while True:
            reply: cl.Message | None = None  # created on the first token so it is ordered after tool steps
            try:
                async for mode, data in graph().astream(payload, config, stream_mode=["messages", "updates"]):
                    if mode == "messages":
                        chunk, meta = data
                        if meta.get("langgraph_node") == "agent" and isinstance(chunk, AIMessage) \
                                and isinstance(chunk.content, str) and chunk.content:
                            if reply is None:
                                reply = cl.Message(content="")
                            await reply.stream_token(chunk.content)
                    elif mode == "updates" and "agent" in data and reply is not None:
                        await reply.send()
                        reply = None
            except Exception as e:  # noqa: BLE001
                await cl.ErrorMessage(content=f"خطا در اجرای دستیار: {type(e).__name__}: {e}").send()
                return
            if reply is not None:
                await reply.send()

            snap = await graph().aget_state(config)
            pending = [i for i in (snap.interrupts or []) if (i.value or {}).get("type") == "approval"]
            if not pending:
                break
            decisions = {}
            for card in pending[0].value["cards"]:
                decisions[card["call_id"]] = await ask_approval(card)
            payload = Command(resume=decisions)
    await refresh_sidebar()


async def ask_secret(key: str) -> str | None:
    """Collect a secret in a private input card. The value goes to Liara only: it is never posted as a chat
    message, sent to the LLM or written to the logs."""
    ask = cl.AskElementMessage(
        content="",
        element=cl.CustomElement(name="SecretInput", props={"name": key}, display="inline"),
        author="Secure input",
        timeout=600,
    )
    res = await ask.send()
    value = res.get("value") if res and res.get("submitted") else None
    ask.content = f"مقدار `{key}` به‌صورت امن ثبت شد." if value else f"ورود مقدار `{key}` لغو شد."
    await ask.update()
    return value


def _card_props(card: dict) -> dict:
    return {"title": card["title"], "risk": card["risk"], "action": card["action"], "reason": card.get("reason"),
            "impact": card.get("impact") or {}, "tool": card["tool"], "args": card.get("args") or {}}


async def ask_approval(card: dict) -> dict:
    props = _card_props(card)
    ask = cl.AskElementMessage(
        content="",
        element=cl.CustomElement(name="ApprovalCard", props=props, display="inline"),
        author="Approval",
        timeout=900,
    )
    res = await ask.send()
    approved = bool(res and res.get("submitted") and res.get("approved"))
    note = "" if approved else ("زمان پاسخ تمام شد؛ اقدامی انجام نشد" if res is None else "")
    ask.content = ""
    await ask.update()
    decided = cl.CustomElement(name="ApprovalCard", display="inline",
                               props={**props, "decided": "approved" if approved else "rejected", "note": note})
    await decided.send(for_id=ask.id)
    return {"approved": approved, "note": note}


# ------------------------------------------------------------------ tool UI hooks
async def tool_start(spec, args):
    ctx: Ctx = cl.user_session.get("ctx")
    if isinstance(args, dict) and args.get("app"):
        ctx.state["last_app"] = args["app"]
    step = cl.Step(name=f"{spec.title_fa}", type="tool", show_input="json", default_open=False)
    step.input = json.dumps(safe_args(args), ensure_ascii=False, indent=1)
    await step.send()

    async def progress(msg: str):
        await step.stream_token(msg + "\n")

    ctx.progress = progress
    return step


async def tool_end(step, spec, result, status, seconds):
    ctx: Ctx = cl.user_session.get("ctx")
    ctx.progress = None
    if step is None:
        return
    summary = json.dumps(result, ensure_ascii=False, default=str, indent=1)
    if len(summary) > 3500:
        summary = summary[:3500] + "\n…"
    prefix = step.output + "\n" if step.output else ""
    state = "موفق" if status == "ok" else "خطا"
    step.output = f"{prefix}{state} · ریسک {spec.risk.value} · {seconds} ثانیه\n```json\n{summary}\n```"
    step.name = spec.title_fa if status == "ok" else f"{spec.title_fa} — خطا"
    await step.update()


async def tool_skipped(spec, args, status):
    title = spec.title_fa if spec else "?"
    text = "طبق سیاست امنیتی مسدود شد" if status == "blocked" else "توسط شما رد شد؛ اجرا نشد"
    await cl.Message(content=f"**{title}** — {text}", author="Policy").send()


# ------------------------------------------------------------------ status sidebar
async def app_status(ctx: Ctx, app: str) -> dict:
    out: dict = {"name": app, "watching": app in (ctx.state.get("watch") or {})}
    try:
        info = parse.app_summary(await ctx.client.get_app(app))
        out.update(running=info["running"], platform=info["platform"], plan=info["plan_id"])
        cache = (ctx.state.get("cache") or {}).get("plans")
        plan = parse.plan_by_id(cache[1], info["plan_id"]) if cache else None
        use = await usage_now(ctx, app, plan and plan.get("cpu_cores"))
        out["memory_mb"], out["cpu_pct"] = use["memory_mb"], use["cpu_pct"]
        if use["memory_mb"] is not None and plan and plan.get("ram_gb"):
            out["memory_pct"] = round(100 * use["memory_mb"] / (plan["ram_gb"] * 1024), 1)
    except LiaraError as e:
        out["error"] = e.friendly
    pr = await probe(default_url(app), n=1, timeout=8)
    out["http_status"] = pr["statuses"][0] if pr["statuses"] else None
    out["http_ok"] = pr["healthy"]
    out["latency_ms"] = pr["median_ms"]
    return out


async def refresh_sidebar():
    ctx: Ctx | None = cl.user_session.get("ctx")
    if not ctx:
        return
    apps = list((ctx.state.get("watch") or {}).keys()) or ([ctx.state["last_app"]] if ctx.state.get("last_app")
                                                         else [])
    statuses = []
    for app in apps[:3]:
        try:
            statuses.append(await asyncio.wait_for(app_status(ctx, app), 30))
        except Exception as e:  # noqa: BLE001
            statuses.append({"name": app, "error": str(e)})
    audit = []
    for a in reversed(ctx.state["audit"].tail(6)):
        spec = REGISTRY.get(a.get("tool"))
        audit.append({"time": (a.get("ts") or "")[11:16], "tool": a.get("tool"),
                      "title": spec.title_fa if spec else a.get("tool"),
                      "status": a.get("status"), "by": a.get("approved_by")})
    if not statuses:  # open the side panel only once there is an app to show (keeps mobile welcome clear)
        return
    await cl.ElementSidebar.set_title(f"{TEAM} · وضعیت سرویس")
    await cl.ElementSidebar.set_elements([cl.CustomElement(
        name="StatusPanel", props={"apps": statuses, "audit": audit, "updated": time.strftime("%H:%M:%S")})])


# ------------------------------------------------------------------ monitoring
MONITOR_LOG = "data/monitor.log"


def _mlog(msg: str) -> None:
    try:
        with open(MONITOR_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass


async def monitor_loop():
    _mlog("monitor started")
    while True:
        await asyncio.sleep(SETTINGS.monitor_interval_sec)
        try:
            await monitor_tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - never let the monitor die silently
            import traceback
            _mlog(f"tick failed: {type(e).__name__}: {e}\n{traceback.format_exc()}")


async def monitor_tick():
    ctx: Ctx | None = cl.user_session.get("ctx")
    if not ctx or not ctx.state.get("watch"):
        _mlog(f"tick: ctx={'yes' if ctx else 'no'} watch={list((ctx.state.get('watch') or {}).keys()) if ctx else []}")
        return
    alerts: dict = cl.user_session.get("alerts")
    for app, cfg in list(ctx.state["watch"].items()):
        try:
            h = await engine.health(ctx, app, cfg.get("url"))
        except Exception as e:  # noqa: BLE001
            _mlog(f"{app}: health failed: {type(e).__name__}: {e}")
            continue
        found = h["issues"]  # warning + critical
        _mlog(f"{app}: healthy={h['healthy']} issues={[i['id'] for i in found]} metrics={h['metrics']}")
        if found and time.time() - alerts.get(app, 0) > 600:
            alerts[app] = time.time()
            severity = "critical" if any(i["severity"] == "critical" for i in found) else "warning"
            await cl.Message(content="", author="Monitor", elements=[cl.CustomElement(
                name="AlertCard", display="inline",
                props={"app": app, "severity": severity, "time": time.strftime("%H:%M"),
                       "issues": [{"title": i["title_fa"]} for i in found]})]).send()
            _mlog(f"{app}: alert sent")
    await refresh_sidebar()


__all__ = ["RISK_FA", "Risk"]

