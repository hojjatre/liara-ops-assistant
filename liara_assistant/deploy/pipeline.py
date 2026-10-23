"""Deploy pipeline: package -> upload source -> create release -> track build until a terminal state.

Same sequence as the official Liara CLI (`liara deploy`).
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from ..liara.client import LiaraError
from ..redact import redact
from . import packager

TERMINAL = {"READY", "FAILED", "UNHEALTHY", "TIMEDOUT", "CANCELED"}
STATE_FA = {
    "QUEUED": "در صف ساخت", "BUILDING": "در حال ساخت (build)", "PUSHING": "در حال ارسال image",
    "PUSHED": "image ارسال شد", "DEPLOYING": "در حال استقرار", "READY": "آماده", "FAILED": "ناموفق",
    "UNHEALTHY": "ناسالم (health check رد شد)", "TIMEDOUT": "زمان ساخت تمام شد", "CANCELED": "لغو شد",
}


def release_body(path: Path, platform: str, port: int, message: str | None) -> dict:
    cfg = packager.read_liara_json(path)
    body: dict = {
        "type": platform,
        "port": port,
        "message": message or "deployed by liara-ops-assistant",
        "platformConfig": cfg.get(platform) or {},
        "build": {"cache": cfg.get("build", {}).get("cache", True)} if isinstance(cfg.get("build"), dict)
        else {"cache": True},
    }
    for key in ("cron", "args", "disks"):
        if cfg.get(key):
            body[key] = cfg[key]
    hc = cfg.get("healthCheck")
    if hc:
        hc = dict(hc)
        if isinstance(hc.get("command"), str):
            hc["command"] = hc["command"].split(" ")
        body["healthCheck"] = hc
    return body


async def deploy(ctx, app: str, path: str, platform: str | None = None, port: int | None = None,
                 message: str | None = None, timeout_sec: int = 900) -> dict:
    t0 = time.time()
    p = Path(path).expanduser().resolve()
    info = packager.analyze(p)
    platform = platform or info["platform"]
    port = port or info["port"]

    await ctx.report(f"بسته‌بندی سورس ({info['files']} فایل، پلتفرم {platform}، پورت {port})")
    archive = packager.make_archive(p)
    size_mb = round(archive.stat().st_size / 1024 / 1024, 2)
    try:
        await ctx.report(f"آپلود سورس ({size_mb}MB)")
        source_id = await ctx.client.upload_source(app, archive)
    finally:
        archive.unlink(missing_ok=True)

    body = release_body(p, platform, port, message)
    body["sourceID"] = source_id
    await ctx.report("ساخت release جدید")
    release_id = await ctx.client.create_release(app, body)
    ctx.state.setdefault("releases", {})[app] = release_id

    result = await track_release(ctx, release_id, timeout_sec=timeout_sec)
    result.update({"app": app, "release_id": release_id, "source_id": source_id, "platform": platform,
                   "port": port, "duration_sec": round(time.time() - t0)})
    return result


async def track_release(ctx, release_id: str, timeout_sec: int = 900, poll_sec: float = 2.5) -> dict:
    since: str | None = None
    last_state = None
    build_lines: list[str] = []
    error_lines: list[str] = []
    deadline = time.time() + timeout_sec
    fail_reason = None
    state = None

    while time.time() < deadline:
        try:
            data = await ctx.client.build_logs(release_id, since)
            release = data.get("release") or {}
            outputs = data.get("buildOutput") or []
        except LiaraError as e:
            if e.status in (404, 405):  # fall back to plain release status
                release = (await ctx.client.get_release(release_id)).get("release") or {}
                outputs = []
            else:
                raise
        for out in outputs:
            line = redact(str(out.get("line", "")).rstrip())
            if out.get("stream", "STDOUT") != "STDOUT":
                error_lines.append(line)
            build_lines.append(line)
        if outputs:
            since = outputs[-1].get("createdAt") or since
            await ctx.report(f"build: {build_lines[-1][:160]}")
        state = release.get("state") or state
        fail_reason = release.get("failReason") or fail_reason
        if state and state != last_state:
            await ctx.report(f"وضعیت: {STATE_FA.get(state, state)}")
            last_state = state
        if error_lines:  # the CLI treats any non-STDOUT build output as a failed build
            state = "FAILED"
            await ctx.report(f"وضعیت: {STATE_FA['FAILED']}")
            break
        if state in TERMINAL and not outputs:
            break
        await asyncio.sleep(poll_sec)
    else:
        state = state or "TIMEDOUT"

    return {
        "state": state,
        "state_fa": STATE_FA.get(state or "", state),
        "success": state == "READY",
        "fail_reason": fail_reason,
        "build_errors": error_lines[-15:],
        "build_log_tail": build_lines[-25:],
    }
