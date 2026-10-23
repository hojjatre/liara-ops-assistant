"""Tool registry with risk levels.

Every tool is an async function `fn(ctx, **args)`. The decorator records its
risk level and builds a JSON schema for the LLM. Risky tools (MEDIUM/HIGH)
automatically get a required `reason` argument, which is shown to the user on
the approval card.

The *policy gate* (liara_assistant/policy.py) decides whether a tool runs
automatically, needs approval, or is blocked. That decision is made in code;
the LLM can only request a tool.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field, create_model


class Risk(str, Enum):
    READ = "read"        # auto-run, no side effects
    LOW = "low"          # auto-run + notify; reversible, no downtime or cost
    MEDIUM = "medium"    # one-click approval
    HIGH = "high"        # approval with full impact summary
    BLOCKED = "blocked"  # never executed by the assistant


RISK_FA = {
    Risk.READ: "فقط‌خواندنی",
    Risk.LOW: "کم‌ریسک",
    Risk.MEDIUM: "نیازمند تأیید",
    Risk.HIGH: "حساس — نیازمند تأیید",
    Risk.BLOCKED: "مسدود",
}

ProgressFn = Callable[[str], Awaitable[None]]


@dataclass
class Ctx:
    """Per-session dependencies passed to every tool."""
    client: Any                      # LiaraClient
    settings: Any                    # Settings
    progress: ProgressFn | None = None
    state: dict = field(default_factory=dict)  # session scratch (baselines, watched apps, cache)
    # UI callback that asks the user for a secret value directly (the value never reaches the LLM)
    ask_secret: Callable[[str], Awaitable[str | None]] | None = None

    async def report(self, msg: str) -> None:
        if self.progress:
            await self.progress(msg)


@dataclass
class ToolSpec:
    name: str
    func: Callable[..., Awaitable[Any]]
    risk: Risk
    title_fa: str
    description: str
    args_model: type[BaseModel]
    describe: Callable[[dict], str] | None = None
    impact: Callable[[Ctx, dict], Awaitable[dict]] | None = None

    def openai_schema(self) -> dict:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": schema}}

    def summary_fa(self, args: dict) -> str:
        if self.describe:
            try:
                return self.describe(args)
            except Exception:  # noqa: BLE001
                pass
        shown = ", ".join(f"{k}={v}" for k, v in args.items() if k != "reason")
        return f"{self.title_fa} ({shown})"


REGISTRY: dict[str, ToolSpec] = {}

_REASON = (str, Field(description="Why this action is needed, in Persian, citing the evidence. "
                                  "Shown to the user on the approval card."))


def tool(risk: Risk, title_fa: str, describe: Callable[[dict], str] | None = None,
         impact: Callable[[Ctx, dict], Awaitable[dict]] | None = None):
    def deco(fn: Callable[..., Awaitable[Any]]):
        sig = inspect.signature(fn, eval_str=True)  # modules use `from __future__ import annotations`
        fields: dict[str, Any] = {}
        for i, (pname, p) in enumerate(sig.parameters.items()):
            if i == 0:  # ctx
                continue
            default = ... if p.default is inspect.Parameter.empty else p.default
            fields[pname] = (p.annotation, default)
        if risk in (Risk.MEDIUM, Risk.HIGH):
            fields["reason"] = _REASON
        model = create_model(f"{fn.__name__}_args", **fields)
        doc = inspect.getdoc(fn) or title_fa
        REGISTRY[fn.__name__] = ToolSpec(fn.__name__, fn, risk, title_fa, doc, model, describe, impact)
        return fn
    return deco


async def run_tool(spec: ToolSpec, ctx: Ctx, args: dict) -> Any:
    clean = spec.args_model(**args).model_dump()
    clean.pop("reason", None)
    return await spec.func(ctx, **clean)
