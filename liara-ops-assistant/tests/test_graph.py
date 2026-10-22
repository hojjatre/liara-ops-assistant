"""Agent graph + human-in-the-loop gate, with a scripted fake LLM and a mocked Liara API."""
import json

import httpx
import respx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from liara_assistant.agent.graph import build_graph
from liara_assistant.policy import AuditLog

from . import fixtures as F
from .test_core import API, make_ctx


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.seen = []

    async def ainvoke(self, messages, *a, **k):
        self.seen.append(messages)
        return self.responses.pop(0)


def call(name, args, cid):
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


def setup(tmp_path, responses):
    ctx = make_ctx()
    ctx.state["audit"] = AuditLog(str(tmp_path / "audit.jsonl"))
    llm = FakeLLM(responses)
    g = build_graph(llm, InMemorySaver())
    cfg = {"configurable": {"thread_id": "t1", "ctx": ctx}}
    return ctx, llm, g, cfg


@respx.mock
async def test_high_risk_action_waits_for_approval_then_runs(tmp_path):
    respx.get(f"{API}/v1/projects/demo-shop").mock(return_value=httpx.Response(200, json=F.project()))
    restart = respx.post(f"{API}/v1/projects/demo-shop/actions/restart").mock(
        return_value=httpx.Response(200, json={}))
    ctx, llm, g, cfg = setup(tmp_path, [
        AIMessage(content="", tool_calls=[
            call("get_app_status", {"app": "demo-shop"}, "c1"),
            call("restart_app", {"app": "demo-shop", "reason": "رم ۹۶٪ است"}, "c2")]),
        AIMessage(content="ری‌استارت انجام شد."),
    ])
    respx.get(f"{API}/v1/projects/demo-shop/releases").mock(return_value=httpx.Response(200, json=F.releases()))
    respx.get(f"{API}/v1/me").mock(return_value=httpx.Response(200, json=F.ME))
    respx.get(f"{API}/v1/projects/demo-shop/metrics/summary").mock(
        return_value=httpx.Response(200, json=F.summary()))

    await g.ainvoke({"messages": [HumanMessage("ری‌استارت کن")]}, cfg)
    snap = await g.aget_state(cfg)
    assert snap.interrupts, "graph must pause for approval"
    cards = snap.interrupts[0].value["cards"]
    assert [c["tool"] for c in cards] == ["restart_app"]          # read tool is not gated
    assert cards[0]["reason"] == "رم ۹۶٪ است" and cards[0]["impact"]["downtime"]
    assert restart.call_count == 0                                 # nothing executed yet

    out = await g.ainvoke(Command(resume={"c2": {"approved": True}}), cfg)
    assert restart.call_count == 1
    tool_msgs = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert {m.tool_call_id for m in tool_msgs} == {"c1", "c2"}
    assert out["messages"][-1].content == "ری‌استارت انجام شد."
    audit = ctx.state["audit"].tail()
    assert any(a["tool"] == "restart_app" and a["approved_by"] == "user" for a in audit)


@respx.mock
async def test_rejected_action_is_not_executed(tmp_path):
    resize = respx.post(f"{API}/v1/projects/demo-shop/resize").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{API}/v1/me").mock(return_value=httpx.Response(200, json=F.ME))
    respx.get(f"{API}/v1/projects/demo-shop").mock(return_value=httpx.Response(200, json=F.project()))
    ctx, llm, g, cfg = setup(tmp_path, [
        AIMessage(content="", tool_calls=[call("resize_app", {"app": "demo-shop", "plan_id": "g2-mars",
                                                              "reason": "کمبود رم"}, "c1")]),
        AIMessage(content="باشد، تغییری ندادم."),
    ])
    await g.ainvoke({"messages": [HumanMessage("پلن را بزرگ کن")]}, cfg)
    card = (await g.aget_state(cfg)).interrupts[0].value["cards"][0]
    assert "+180,000" in card["impact"]["cost"]                   # (500-250) * 720 Toman/month
    out = await g.ainvoke(Command(resume={"c1": {"approved": False}}), cfg)
    assert resize.call_count == 0
    tm = [m for m in out["messages"] if isinstance(m, ToolMessage)][-1]
    assert json.loads(tm.content)["refused"] is True


@respx.mock
async def test_blocked_tool_never_runs_and_needs_no_approval(tmp_path):
    ctx, llm, g, cfg = setup(tmp_path, [
        AIMessage(content="", tool_calls=[call("delete_app", {"app": "demo-shop"}, "c1")]),
        AIMessage(content="حذف برنامه توسط دستیار ممکن نیست."),
    ])
    out = await g.ainvoke({"messages": [HumanMessage("برنامه را پاک کن")]}, cfg)
    assert not (await g.aget_state(cfg)).interrupts
    tm = [m for m in out["messages"] if isinstance(m, ToolMessage)][-1]
    assert json.loads(tm.content)["refused"] is True
    assert ctx.state["audit"].tail()[-1]["status"] == "blocked"


@respx.mock
async def test_env_values_never_shown_on_card(tmp_path):
    respx.get(f"{API}/v1/projects/demo-shop").mock(return_value=httpx.Response(200, json=F.project()))
    upd = respx.post(f"{API}/v1/projects/update-envs").mock(return_value=httpx.Response(200, json={}))
    ctx, llm, g, cfg = setup(tmp_path, [
        AIMessage(content="", tool_calls=[call("set_env_vars", {
            "app": "demo-shop", "variables": {"DATABASE_URL": "postgres://u:supersecret@db/x"},
            "reason": "DATABASE_URL تنظیم نشده"}, "c1")]),
        AIMessage(content="تنظیم شد."),
    ])
    await g.ainvoke({"messages": [HumanMessage("env را تنظیم کن")]}, cfg)
    card = (await g.aget_state(cfg)).interrupts[0].value["cards"][0]
    assert "supersecret" not in json.dumps(card, ensure_ascii=False)
    await g.ainvoke(Command(resume={"c1": {"approved": True}}), cfg)
    body = json.loads(upd.calls.last.request.content)
    keys = {v["key"] for v in body["variables"]}
    assert keys == {"NODE_ENV", "DATABASE_URL"}                   # merged, not replaced
    assert "supersecret" not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


@respx.mock
async def test_secret_is_collected_by_ui_and_never_reaches_llm(tmp_path):
    respx.get(f"{API}/v1/projects/demo-shop").mock(return_value=httpx.Response(200, json=F.project()))
    upd = respx.post(f"{API}/v1/projects/update-envs").mock(return_value=httpx.Response(200, json={}))
    ctx, llm, g, cfg = setup(tmp_path, [
        AIMessage(content="", tool_calls=[call("set_env_vars", {
            "app": "demo-shop", "variables": {"DATABASE_URL": "ASK_USER"}, "reason": "DATABASE_URL تنظیم نشده"},
            "c1")]),
        AIMessage(content="تنظیم شد."),
    ])

    async def ask(key):
        assert key == "DATABASE_URL"
        return "postgres://u:topsecret@db/x"
    ctx.ask_secret = ask

    await g.ainvoke({"messages": [HumanMessage("درستش کن")]}, cfg)
    await g.ainvoke(Command(resume={"c1": {"approved": True}}), cfg)
    sent = json.loads(upd.calls.last.request.content)
    assert {"key": "DATABASE_URL", "value": "postgres://u:topsecret@db/x"} in sent["variables"]
    seen_by_llm = json.dumps([[m.content for m in msgs] for msgs in llm.seen], ensure_ascii=False)
    assert "topsecret" not in seen_by_llm
    assert "topsecret" not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
