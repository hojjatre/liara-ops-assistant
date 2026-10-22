"""Policy gate (human-in-the-loop) and audit log.

The gate is enforced in code. Text inside logs or tool results can't change it,
which also neutralises prompt injection hidden in application logs.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .redact import redact_obj
from .tools.registry import RISK_FA, Ctx, Risk, ToolSpec

AUTO, APPROVE, BLOCK = "auto", "approve", "block"


def safe_args(args: dict) -> dict:
    """Arguments safe to show in the UI / audit log: env var values are never displayed."""
    out = {}
    for k, v in args.items():
        if k == "reason":
            continue
        if k == "variables" and isinstance(v, dict):
            out[k] = {key: "***" for key in v}
        else:
            out[k] = redact_obj(v)
    return out


def decide(spec: ToolSpec | None) -> str:
    if spec is None or spec.risk is Risk.BLOCKED:
        return BLOCK
    if spec.risk in (Risk.READ, Risk.LOW):
        return AUTO
    return APPROVE


async def approval_card(spec: ToolSpec, ctx: Ctx, args: dict, call_id: str) -> dict:
    impact: dict[str, Any] = {}
    if spec.impact:
        try:
            impact = await spec.impact(ctx, args)
        except Exception as e:  # noqa: BLE001 - impact is best effort
            impact = {"note": f"محاسبه اثر ممکن نشد: {e}"}
    return {
        "call_id": call_id,
        "tool": spec.name,
        "risk": spec.risk.value,
        "risk_fa": RISK_FA[spec.risk],
        "title": spec.title_fa,
        "action": spec.summary_fa(args),
        "reason": args.get("reason", ""),
        "args": safe_args(args),
        "impact": impact,
        "created_at": time.time(),
    }


def card_markdown(card: dict) -> str:
    imp = card.get("impact") or {}
    lines = [
        f"### {card['risk_fa']} — {card['title']}",
        f"**اقدام:** {card['action']}",
    ]
    if card.get("reason"):
        lines.append(f"**دلیل:** {card['reason']}")
    if imp.get("downtime"):
        lines.append(f"**قطعی سرویس:** {imp['downtime']}")
    if imp.get("cost"):
        lines.append(f"**هزینه:** {imp['cost']}")
    if imp.get("rollback"):
        lines.append(f"**بازگشت‌پذیری:** {imp['rollback']}")
    if imp.get("note"):
        lines.append(f"**نکته:** {imp['note']}")
    lines.append(f"`{card['tool']}` · `{json.dumps(card['args'], ensure_ascii=False)}`")
    return "\n\n".join(lines)


class AuditLog:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, **entry: Any) -> None:
        entry.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def tail(self, n: int = 20) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out
