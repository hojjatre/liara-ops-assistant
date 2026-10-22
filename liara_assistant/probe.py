"""Synthetic HTTP probe.

Liara exposes CPU/RAM/network/disk metrics but no request latency or HTTP
status-code metrics, so the assistant measures those itself.
"""
from __future__ import annotations

import asyncio
import statistics
import time

import httpx


def default_url(app: str) -> str:
    return f"https://{app}.liara.run"


async def probe(url: str, n: int = 3, timeout: float = 15.0, path: str = "") -> dict:
    target = url.rstrip("/") + ("/" + path.lstrip("/") if path else "")
    results = []
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout,
                                 headers={"User-Agent": "liara-ops-assistant-probe"}) as http:
        for i in range(n):
            t0 = time.perf_counter()
            try:
                r = await http.get(target)
                ms = round((time.perf_counter() - t0) * 1000)
                results.append({"status": r.status_code, "ms": ms, "snippet": r.text[:160]})
            except httpx.HTTPError as e:
                ms = round((time.perf_counter() - t0) * 1000)
                results.append({"status": None, "ms": ms, "error": type(e).__name__ + ": " + str(e)[:120]})
            if i < n - 1:
                await asyncio.sleep(0.3)
    ok = [r for r in results if r.get("status") and r["status"] < 400]
    times = [r["ms"] for r in results if r.get("status")]
    return {
        "url": target,
        "attempts": n,
        "ok": len(ok),
        "statuses": [r.get("status") for r in results],
        "median_ms": int(statistics.median(times)) if times else None,
        "max_ms": max(times) if times else None,
        "errors": [r["error"] for r in results if r.get("error")][:2],
        "sample_body": next((r.get("snippet") for r in results if r.get("status", 0) and r["status"] >= 400), None),
        "healthy": len(ok) == n,
    }
