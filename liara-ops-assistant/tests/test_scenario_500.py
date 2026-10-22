"""End-to-end demo scenario with a scripted LLM: HTTP 500 -> diagnose -> approve env fix -> verify."""
import json

import httpx
import respx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from liara_assistant.diagnostics import engine

from . import fixtures as F
from .test_core import API, _mock_app
from .test_graph import call, setup


async def _nosleep(*_):
    return None


@respx.mock
async def test_http_500_full_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_sleep", _nosleep)
    _mock_app(60 * 1024 * 1024, F.logs_missing_env())
    upd = respx.post(f"{API}/v1/projects/update-envs").mock(return_value=httpx.Response(200, json={}))

    ctx, llm, g, cfg = setup(tmp_path, [
        AIMessage(content="", tool_calls=[call("diagnose_app", {"app": "demo-shop", "symptom": "500"}, "d1")]),
        AIMessage(content="علت محتمل: DATABASE_URL تنظیم نشده. مقدارش را بفرستید."),
        AIMessage(content="", tool_calls=[call("set_env_vars", {
            "app": "demo-shop", "variables": {"DATABASE_URL": "postgres://u:p@db:5432/shop"},
            "reason": "لاگ‌ها: DATABASE_URL is not set (۱۲ بار) بعد از release v2"}, "s1")]),
        AIMessage(content="", tool_calls=[call("verify_fix", {"app": "demo-shop"}, "v1")]),
        AIMessage(content="✅ مشکل برطرف شد."),
    ])

    out = await g.ainvoke({"messages": [HumanMessage("سایتم خطای 500 می‌دهد")]}, cfg)
    diag = json.loads([m for m in out["messages"] if isinstance(m, ToolMessage)][-1].content)
    assert diag["hypotheses"][0]["id"] == "missing_env"

    await g.ainvoke({"messages": [HumanMessage("postgres://u:p@db:5432/shop")]}, cfg)
    snap = await g.aget_state(cfg)
    assert snap.interrupts[0].value["cards"][0]["tool"] == "set_env_vars"

    # after the fix the site is healthy and logs are clean
    respx.get("https://demo-shop.liara.run").mock(return_value=httpx.Response(200, text="ok"))
    respx.get(f"{API}/v2/projects/demo-shop/logs").mock(return_value=httpx.Response(200, json={"data": []}))
    out = await g.ainvoke(Command(resume={"s1": {"approved": True}}), cfg)
    assert upd.call_count == 1
    verify = json.loads([m for m in out["messages"] if isinstance(m, ToolMessage)][-1].content)
    assert verify["verdict"] == "fixed", verify
    assert verify["before"]["error_count"] > 0 and verify["after"]["new_error_lines"] == 0
    assert out["messages"][-1].content.startswith("✅")
