"""LangGraph agent: agent -> review (human-in-the-loop) -> tools -> agent.

* `agent`  : the LLM decides what to do and may request tools.
* `review` : for every requested medium/high-risk tool, builds an approval card and pauses the graph with
             `interrupt()`. The UI shows the cards and resumes with {tool_call_id: {approved, note}}.
             This node has no side effects, so re-running it on resume is safe.
* `tools`  : executes auto-approved and user-approved tools, refuses blocked/rejected ones, writes the
             audit log and returns ToolMessages.
"""
from __future__ import annotations

import json
import time
from typing import Annotated, Any, Awaitable, Callable, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from pydantic import ValidationError

from .. import policy
from ..liara.client import LiaraError
from ..redact import redact
from ..tools import liara_tools  # noqa: F401  (registers tools)
from ..tools.registry import REGISTRY, Ctx, run_tool
from .prompts import SYSTEM_PROMPT, context_block

MAX_TOOL_RESULT_CHARS = 12_000
MAX_HISTORY = 40


class AgentState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    decisions: dict


def tool_schemas() -> list[dict]:
    return [spec.openai_schema() for spec in REGISTRY.values()]


def _history(messages: list[BaseMessage]) -> list[BaseMessage]:
    msgs = messages[-MAX_HISTORY:]
    # never start with an orphan ToolMessage / tool-calling AIMessage
    while msgs and not isinstance(msgs[0], HumanMessage):
        msgs = msgs[1:]
    return msgs or messages[-1:]


def _dump(result: Any) -> str:
    text = json.dumps(result, ensure_ascii=False, default=str)
    text = redact(text)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + '..."[truncated]"'
    return text


UiHook = Callable[..., Awaitable[Any]]


def build_graph(llm, checkpointer=None):
    """`llm` must already have tools bound (see llm.make_llm)."""

    async def agent(state: AgentState, config) -> dict:
        ctx: Ctx = config["configurable"]["ctx"]
        system = SystemMessage(SYSTEM_PROMPT + "\n" + context_block(ctx.settings, ctx.state))
        resp = await llm.ainvoke([system, *_history(state["messages"])])
        return {"messages": [resp]}

    async def review(state: AgentState, config) -> dict:
        ctx: Ctx = config["configurable"]["ctx"]
        last = state["messages"][-1]
        decisions = dict(state.get("decisions") or {})
        cards = []
        for tc in getattr(last, "tool_calls", []) or []:
            spec = REGISTRY.get(tc["name"])
            if policy.decide(spec) == policy.APPROVE and tc["id"] not in decisions:
                cards.append(await policy.approval_card(spec, ctx, tc["args"], tc["id"]))
        if not cards:
            return {}
        answer = interrupt({"type": "approval", "cards": cards})
        for card in cards:
            d = (answer or {}).get(card["call_id"]) or {}
            decisions[card["call_id"]] = {"approved": bool(d.get("approved")), "note": d.get("note", "")}
        return {"decisions": decisions}

    async def tools(state: AgentState, config) -> dict:
        ctx: Ctx = config["configurable"]["ctx"]
        hooks: dict[str, UiHook] = config["configurable"].get("ui_hooks") or {}
        audit: policy.AuditLog | None = ctx.state.get("audit")
        decisions = state.get("decisions") or {}
        last = state["messages"][-1]
        out: list[ToolMessage] = []
        for tc in getattr(last, "tool_calls", []) or []:
            name, args, cid = tc["name"], tc["args"] or {}, tc["id"]
            spec = REGISTRY.get(name)
            verdict = policy.decide(spec)
            approved_by = "auto"
            if verdict == policy.BLOCK:
                result: Any = {"refused": True, "why": "این عملیات طبق سیاست امنیتی دستیار مسدود است "
                                                       "(حذف منابع). کاربر می‌تواند از کنسول لیارا انجام دهد."}
                status = "blocked"
            elif verdict == policy.APPROVE and not (decisions.get(cid) or {}).get("approved"):
                note = (decisions.get(cid) or {}).get("note")
                result = {"refused": True, "why": "کاربر این اقدام را تأیید نکرد" + (f": {note}" if note else "")}
                status = "rejected"
            else:
                approved_by = "user" if verdict == policy.APPROVE else "auto"
                handle = await hooks["tool_start"](spec, args) if "tool_start" in hooks else None
                t0 = time.time()
                try:
                    result = await run_tool(spec, ctx, args)
                    status = "error" if isinstance(result, dict) and result.get("error") else "ok"
                except LiaraError as e:
                    result, status = e.to_dict(), "error"
                except ValidationError as e:
                    result, status = {"error": True, "invalid_arguments": json.loads(e.json())[:5]}, "error"
                except Exception as e:  # noqa: BLE001
                    result, status = {"error": True, "message": f"{type(e).__name__}: {e}"}, "error"
                if "tool_end" in hooks:
                    await hooks["tool_end"](handle, spec, result, status, round(time.time() - t0, 1))
            if audit and spec and spec.name != "show_audit_log":
                audit.write(tool=name, risk=spec.risk.value if spec else None, status=status,
                            approved_by=approved_by if status not in ("rejected", "blocked") else status,
                            args=policy.safe_args(args), reason=args.get("reason"))
            if status in ("rejected", "blocked") and "tool_skipped" in hooks:
                await hooks["tool_skipped"](spec, args, status)
            out.append(ToolMessage(content=_dump(result), tool_call_id=cid, name=name))
        return {"messages": out}

    def route_after_agent(state: AgentState) -> str:
        last = state["messages"][-1]
        return "review" if isinstance(last, AIMessage) and last.tool_calls else END

    g = StateGraph(AgentState)
    g.add_node("agent", agent)
    g.add_node("review", review)
    g.add_node("tools", tools)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route_after_agent, {"review": "review", END: END})
    g.add_edge("review", "tools")
    g.add_edge("tools", "agent")
    return g.compile(checkpointer=checkpointer)
