import json

import httpx
import respx

from liara_assistant.deploy import pipeline

from .test_core import API, DEMO, make_ctx


@respx.mock
async def test_deploy_flow_upload_release_track(monkeypatch):
    monkeypatch.setattr(pipeline.asyncio, "sleep", _nosleep)
    upload = respx.post(f"{API}/v2/projects/demo-shop/sources").mock(
        return_value=httpx.Response(200, json={"sourceID": "src1"}))
    release = respx.post(f"{API}/v2/projects/demo-shop/releases").mock(
        return_value=httpx.Response(200, json={"releaseID": "rel1"}))
    respx.get(f"{API}/v2/releases/rel1/build-logs").mock(side_effect=[
        httpx.Response(200, json={"release": {"state": "BUILDING"}, "buildOutput": [
            {"line": "npm install", "stream": "STDOUT", "createdAt": "t1"}]}),
        httpx.Response(200, json={"release": {"state": "DEPLOYING"}, "buildOutput": []}),
        httpx.Response(200, json={"release": {"state": "READY"}, "buildOutput": []}),
    ])
    progress = []
    ctx = make_ctx()

    async def rep(m):
        progress.append(m)
    ctx.progress = rep

    res = await pipeline.deploy(ctx, "demo-shop", str(DEMO))
    assert res["success"] and res["state"] == "READY" and res["release_id"] == "rel1"
    assert upload.call_count == 1
    body = json.loads(release.calls.last.request.content)
    assert body["sourceID"] == "src1" and body["type"] == "node" and body["port"] == 3000
    assert body["healthCheck"]["command"][0] == "CMD"
    assert any("آماده" in p for p in progress)


@respx.mock
async def test_build_failure_returns_errors(monkeypatch):
    monkeypatch.setattr(pipeline.asyncio, "sleep", _nosleep)
    respx.get(f"{API}/v2/releases/rel2/build-logs").mock(return_value=httpx.Response(200, json={
        "release": {"state": "BUILDING"},
        "buildOutput": [{"line": "npm ERR! missing script: start", "stream": "STDERR", "createdAt": "t"}]}))
    res = await pipeline.track_release(make_ctx(), "rel2")
    assert res["state"] == "FAILED" and "missing script" in res["build_errors"][0]


async def _nosleep(*_):
    return None
