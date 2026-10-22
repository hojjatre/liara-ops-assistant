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
from liara_assistant.policy import AuditLog, card_markdown, safe_args
from liara_assistant.probe import default_url, probe
from liara_assistant.tools.registry import RISK_FA, Ctx, Risk

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
        cl.Starter(label="برنامه‌های من", message="برنامه‌های من روی لیارا را با وضعیتشان نشان بده."),
        cl.Starter(label="سایتم خطای ۵۰۰ می‌دهد", message="سایتم خطای 500 می‌دهد. بررسی کن مشکل چیست."),
        cl.Starter(label="سایتم کند شده", message="سایتم خیلی کند شده. علتش را پیدا کن."),
        cl.Starter(label="دیپلوی پروژه", message="می‌خواهم پروژه‌ام را روی لیارا دیپلوی کنم."),
    ]


# ------------------------------------------------------------------ session
@cl.on_chat_start
async def start():
    missing = SETTINGS.missing()
    if missing:
        await cl.Message(content="⚠️ تنظیمات کامل نیست. این متغیرها را در فایل `.env` قرار دهید و برنامه را "
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
                await cl.Message(content=f"❌ فایل zip باز نشد: {e}").send()
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
    """Collect a secret directly from the user. The value goes to Liara only, never to the LLM or the logs."""
    res = await cl.AskUserMessage(
        content=f"🔐 مقدار `{key}` را وارد کنید. این مقدار فقط به لیارا ارسال می‌شود و به مدل زبانی یا گزارش‌ها "
                "نمی‌رسد.",
        timeout=600,
        author="Secure input",
    ).send()
    return (res or {}).get("output")


async def ask_approval(card: dict) -> dict:
    await cl.Message(content=card_markdown(card), author="Approval").send()
    res = await cl.AskActionMessage(
        content="آیا این اقدام انجام شود؟",
        actions=[
            cl.Action(name="approve", payload={"v": "yes"}, label="✅ تأیید و اجرا"),
            cl.Action(name="reject", payload={"v": "no"}, label="❌ رد"),
        ],
        timeout=900,
    ).send()
    approved = bool(res and (res.get("payload") or {}).get("v") == "yes")
    note = "" if approved else ("زمان پاسخ تمام شد" if res is None else "")
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
    icon = {"ok": "✅", "error": "❌"}.get(status, "•")
    summary = json.dumps(result, ensure_ascii=False, default=str, indent=1)
    if len(summary) > 3500:
        summary = summary[:3500] + "\n…"
    prefix = step.output + "\n" if step.output else ""
    step.output = f"{prefix}{icon} {spec.risk.value} · {seconds}s\n```json\n{summary}\n```"
    step.name = f"{icon} {spec.title_fa}"
    await step.update()


async def tool_skipped(spec, args, status):
    title = spec.title_fa if spec else "?"
    text = "⛔ مسدود شد (سیاست امنیتی)" if status == "blocked" else "❌ توسط شما رد شد"
    await cl.Message(content=f"{text}: {title}", author="Policy").send()


# ------------------------------------------------------------------ status sidebar
async def status_markdown(ctx: Ctx, app: str) -> str:
    lines = [f"## 📊 {app}"]
    try:
        info = parse.app_summary(await ctx.client.get_app(app))
        lines.append(f"- وضعیت: {'🟢 روشن' if info['running'] else '🔴 خاموش'} · پلتفرم `{info['platform']}` · "
                     f"پلن `{info['plan_id']}`")
        cache = (ctx.state.get("cache") or {}).get("plans")
        plan = parse.plan_by_id(cache[1], info["plan_id"]) if cache else None
        use = await usage_now(ctx, app, plan and plan.get("cpu_cores"))
        mem, cpu = use["memory_mb"], use["cpu_pct"]
        if mem is not None:
            pct = f" ({round(100 * mem / (plan['ram_gb'] * 1024))}%)" if plan and plan.get("ram_gb") else ""
            lines.append(f"- رم: {round(mem)}MB{pct}")
        if cpu is not None:
            lines.append(f"- CPU: {cpu}%")
    except LiaraError as e:
        lines.append(f"- ⚠️ {e.friendly}")
    pr = await probe(default_url(app), n=1, timeout=8)
    st = pr["statuses"][0] if pr["statuses"] else None
    lines.append(f"- HTTP: {'✅' if pr['healthy'] else '❌'} {st or pr['errors'][:1]} · {pr['median_ms'] or '-'}ms")
    watch = ctx.state.get("watch") or {}
    lines.append(f"- پایش خودکار: {'فعال ✅' if app in watch else 'غیرفعال'}")
    lines.append(f"\n_به‌روزرسانی: {time.strftime('%H:%M:%S')}_")
    return "\n".join(lines)


async def refresh_sidebar():
    ctx: Ctx | None = cl.user_session.get("ctx")
    if not ctx:
        return
    apps = list((ctx.state.get("watch") or {}).keys()) or ([ctx.state["last_app"]] if ctx.state.get("last_app")
                                                         else [])
    if not apps:
        return
    parts = []
    for app in apps[:3]:
        try:
            parts.append(await asyncio.wait_for(status_markdown(ctx, app), 30))
        except Exception as e:  # noqa: BLE001
            parts.append(f"## {app}\n- ⚠️ {e}")
    audit = ctx.state["audit"].tail(5)
    if audit:
        parts.append("## 🧾 آخرین عملیات\n" + "\n".join(
            f"- {a.get('ts', '')[11:16]} `{a.get('tool')}` · {a.get('status')} · {a.get('approved_by')}"
            for a in reversed(audit)))
    await cl.ElementSidebar.set_title("وضعیت سرویس")
    await cl.ElementSidebar.set_elements([cl.Text(name="status", content="\n\n".join(parts))])


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
            issues = "؛ ".join(i["title_fa"] for i in found)
            icon = "🚨" if any(i["severity"] == "critical" for i in found) else "⚠️"
            await cl.Message(
                content=f"{icon} **هشدار پایش — {app}**\n\n{issues}",
                actions=[cl.Action(name="diagnose", payload={"app": app, "issues": issues},
                                   label="🔍 عیب‌یابی کن")],
                author="Monitor",
            ).send()
            _mlog(f"{app}: alert sent")
    await refresh_sidebar()


__all__ = ["RISK_FA", "Risk"]

