import json
import tarfile
from pathlib import Path

import httpx
import pytest
import respx

from liara_assistant.config import Settings
from liara_assistant.deploy import packager
from liara_assistant.diagnostics import engine
from liara_assistant.liara import parse
from liara_assistant.liara.client import LiaraClient, LiaraError
from liara_assistant.redact import redact
from liara_assistant.tools.registry import Ctx

from . import fixtures as F

API = "https://api.liara.ir"
DEMO = Path(__file__).parent.parent / "demo-app"


def make_ctx():
    s = Settings(liara_api_token="t0k", liara_team_id="aaaaaaaaaaaaaaaaaaaaaaaa", llm_api_key="x")
    return Ctx(client=LiaraClient("t0k", s.liara_team_id), settings=s)


# ------------------------------------------------------------------ client
@respx.mock
async def test_client_sends_bearer_and_team_id():
    route = respx.get(f"{API}/v1/projects").mock(return_value=httpx.Response(200, json={"projects": []}))
    c = LiaraClient("secret-token", "aaaaaaaaaaaaaaaaaaaaaaaa")
    await c.list_apps()
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer secret-token"
    assert req.url.params["teamID"] == "aaaaaaaaaaaaaaaaaaaaaaaa"


@respx.mock
async def test_client_maps_errors():
    respx.get(f"{API}/v1/projects/nope").mock(return_value=httpx.Response(404, json={"message": "App not found"}))
    c = LiaraClient("t")
    with pytest.raises(LiaraError) as ei:
        await c.get_app("nope")
    assert ei.value.status == 404 and "App not found" in ei.value.friendly


# ------------------------------------------------------------------ parsing
def test_plans_parse_and_filter():
    plans = parse.plans(F.ME)
    ids = [p["id"] for p in parse.deployable_plans(plans)]
    assert "old-plan" not in ids and "g2-mars" in ids
    earth = parse.plan_by_id(plans, "g2-earth")
    assert earth["monthly_toman"] == 250 * 720 and earth["bundles"]["basic"] == 100000


def test_logs_v2_sorted_and_ns_timestamps():
    data = {"data": [{"metaData": {"releaseId": "r"}, "values": [["1700000002000000000", "b"],
                                                                 ["1700000001000000000", "a"]]}]}
    lines = parse.logs_v2(data)
    assert [x["line"] for x in lines] == ["a", "b"] and 1.6e9 < lines[0]["ts"] < 1.8e9


def test_metric_normalisation():
    assert parse.memory_mb(256 * 1024 * 1024) == 256
    assert parse.cpu_percent(75.0, 0.5) == 50.0     # 75 CPU-seconds in 5 min = 0.25 cores = 50% of 0.5
    assert parse.cpu_percent(0.66, 0.5) == 0.4      # idle Node app plateau seen on Liara


def test_redact():
    s = redact("DATABASE_URL=postgres://user:hunter2@db:5432/x token=abc123 Bearer abcdefghijklmnop")
    assert "hunter2" not in s and "abc123" not in s and "abcdefghijklmnop" not in s


# ------------------------------------------------------------------ packager
def test_detect_demo_app():
    info = packager.analyze(DEMO)
    assert info["platform"] == "node" and info["port"] == 3000 and info["has_liara_json"]
    assert "DATABASE_URL" in info["env_keys_expected"]


def test_archive_excludes_secrets_and_node_modules(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"express": "4"}}))
    (tmp_path / "index.js").write_text("app.listen(process.env.PORT || 8081)")
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "i.js").write_text("")
    (tmp_path / ".gitignore").write_text("dist\n")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "big.js").write_text("x")
    assert packager.detect_port(tmp_path, "node")[0] == 8081
    archive = packager.make_archive(tmp_path)
    with tarfile.open(archive) as t:
        names = t.getnames()
    archive.unlink()
    assert "index.js" in names and ".env" not in names
    assert not any(n.startswith("node_modules") or n.startswith("dist") for n in names)


# ------------------------------------------------------------------ diagnosis
def _mock_app(mem_bytes, logs, ev_texts=(), mem_series=None, release_minutes_ago=10, probe_status=500):
    respx.get(f"{API}/v1/projects/demo-shop").mock(return_value=httpx.Response(200, json=F.project()))
    respx.get(f"{API}/v1/projects/demo-shop/releases").mock(
        return_value=httpx.Response(200, json=F.releases(release_minutes_ago)))
    respx.get(f"{API}/v1/projects/demo-shop/events").mock(
        return_value=httpx.Response(200, json=F.events(list(ev_texts))))
    respx.get(f"{API}/v2/projects/demo-shop/logs").mock(return_value=httpx.Response(200, json=logs))
    respx.get(f"{API}/v1/projects/demo-shop/metrics/summary").mock(
        return_value=httpx.Response(200, json=F.summary(mem_bytes)))
    respx.get(f"{API}/v1/projects/demo-shop/metrics/cpu").mock(
        return_value=httpx.Response(200, json=F.series([0.05] * 8)))
    respx.get(f"{API}/v1/projects/demo-shop/metrics/memory").mock(
        return_value=httpx.Response(200, json=F.series(mem_series or [mem_bytes] * 8)))
    respx.get(f"{API}/v1/me").mock(return_value=httpx.Response(200, json=F.ME))
    respx.get("https://demo-shop.liara.run").mock(return_value=httpx.Response(probe_status, text="500 — err"))


@respx.mock
async def test_diagnose_missing_env_after_release():
    _mock_app(60 * 1024 * 1024, F.logs_missing_env())
    ctx = make_ctx()
    rep = await engine.diagnose(ctx, "demo-shop", "HTTP 500")
    ids = {s["id"] for s in rep["signals"]}
    assert {"probe_5xx", "missing_env", "error_logs", "errors_after_release"} <= ids
    top = rep["hypotheses"][0]
    assert top["id"] == "missing_env" and top["score"] >= 0.85
    assert top["proposed_actions"][0]["tool"] == "set_env_vars"
    assert "DATABASE_URL" in top["proposed_actions"][0]["args"]["variables"]
    assert ctx.state["baselines"]["demo-shop"]["error_count"] > 0


@respx.mock
async def test_diagnose_memory_pressure_proposes_resize():
    mb = 1024 * 1024
    _mock_app(490 * mb, F.logs_quiet(), ev_texts=["container killed: OOMKilled", "app restarted",
                                                  "app restarted"],
              mem_series=[200 * mb, 250 * mb, 300 * mb, 380 * mb, 420 * mb, 470 * mb, 490 * mb],
              release_minutes_ago=600, probe_status=200)
    ctx = make_ctx()
    rep = await engine.diagnose(ctx, "demo-shop", "slow")
    top = rep["hypotheses"][0]
    assert top["id"] == "memory"
    tools = [a["tool"] for a in top["proposed_actions"]]
    assert tools == ["restart_app", "resize_app"]
    assert top["proposed_actions"][1]["args"]["plan_id"] == "g2-mars"


def test_restart_newest_container_wins():
    """Shape observed on a live app: the old container keeps reporting its last RAM for minutes after a
    restart, so applets must not be summed."""
    t0 = 1_800_000_000
    mb = 1024 * 1024
    memory = {"result": [
        {"applet": "old", "values": [[t0 + i * 60, str(v * mb)] for i, v in enumerate([58, 460, 459, 430, 430, 430])]},
        {"applet": "new", "values": [[t0 + i * 60, str(v * mb)] for i, v in [(4, 30), (5, 31)]]},
    ]}
    cur = parse.current_series(memory)
    assert [round(v / mb) for _, v in cur] == [58, 460, 459, 430, 30, 31]
