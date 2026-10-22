"""Thin async client for Liara's PaaS and DNS APIs.

Endpoints follow Liara's official OpenAPI spec (github.com/liara-cloud/openapi)
and the official CLI (github.com/liara-cloud/cli), which is the reference for
the deploy / logs flow.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

_FRIENDLY = {
    400: "درخواست نامعتبر بود",
    401: "توکن API لیارا نامعتبر است یا منقضی شده",
    403: "این حساب/تیم دسترسی لازم برای این عملیات را ندارد",
    404: "پیدا نشد (نام برنامه یا شناسه را بررسی کنید)",
    409: "تداخل با وضعیت فعلی منبع",
    429: "محدودیت نرخ درخواست لیارا؛ کمی بعد دوباره تلاش کنید",
}


class LiaraError(Exception):
    def __init__(self, status: int, message: str, method: str = "", path: str = ""):
        self.status = status
        self.message = message
        self.method = method
        self.path = path
        super().__init__(f"[{status}] {method} {path}: {message}")

    @property
    def friendly(self) -> str:
        base = _FRIENDLY.get(self.status, "خطای سرور لیارا" if self.status >= 500 else "خطا")
        return f"{base} — {self.message}" if self.message else base

    def to_dict(self) -> dict:
        return {"error": True, "status": self.status, "message": self.message, "friendly": self.friendly,
                "endpoint": f"{self.method} {self.path}"}


class LiaraClient:
    def __init__(
        self,
        token: str,
        team_id: str = "",
        api_url: str = "https://api.liara.ir",
        dns_url: str = "https://dns-service.iran.liara.ir",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ):
        self.team_id = team_id
        self.api_url = api_url.rstrip("/")
        self.dns_url = dns_url.rstrip("/")
        self._http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}", "User-Agent": "liara-ops-assistant/0.1"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ core
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        files: Any = None,
        dns: bool = False,
        timeout: float | None = None,
        retries: int = 2,
    ) -> Any:
        base = self.dns_url if dns else self.api_url
        url = f"{base}/{path.lstrip('/')}"
        q = {k: v for k, v in (params or {}).items() if v is not None}
        if self.team_id:
            q["teamID"] = self.team_id  # the official CLI adds this to every request
        attempt = 0
        while True:
            try:
                resp = await self._http.request(
                    method, url, params=q, json=json, files=files,
                    timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
                )
            except httpx.TransportError as e:
                if method == "GET" and attempt < retries:
                    attempt += 1
                    await asyncio.sleep(0.5 * 2**attempt)
                    continue
                raise LiaraError(0, f"network error: {e}", method, path) from e
            if resp.status_code >= 500 and method == "GET" and attempt < retries:
                attempt += 1
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            if resp.status_code >= 400:
                raise LiaraError(resp.status_code, _error_message(resp), method, path)
            if not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {"raw": resp.text}

    async def get(self, path: str, **kw) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw) -> Any:
        return await self.request("POST", path, **kw)

    async def put(self, path: str, **kw) -> Any:
        return await self.request("PUT", path, **kw)

    async def delete(self, path: str, **kw) -> Any:
        return await self.request("DELETE", path, **kw)

    # -------------------------------------------------------------- account
    async def me(self) -> dict:
        return await self.get("v1/me")

    async def free_credit(self) -> dict:
        return await self.get("v1/me/free-credit")

    async def billing(self) -> dict:
        return await self.get("v1/billing")

    # ----------------------------------------------------------------- apps
    async def list_apps(self) -> dict:
        return await self.get("v1/projects")

    async def get_app(self, name: str) -> dict:
        return await self.get(f"v1/projects/{name}")

    async def create_app(self, name: str, plan_id: str, platform: str, bundle_plan_id: str | None = None,
                         network: str | None = None, read_only_root_fs: bool | None = None) -> dict:
        body: dict[str, Any] = {"name": name, "planID": plan_id, "platform": platform}
        if bundle_plan_id:
            body["bundlePlanID"] = bundle_plan_id
        if network:
            body["network"] = network
        if read_only_root_fs is not None:
            body["readOnlyRootFilesystem"] = read_only_root_fs
        return await self.post("v1/projects", json=body)

    async def restart_app(self, name: str) -> dict:
        return await self.post(f"v1/projects/{name}/actions/restart")

    async def scale_app(self, name: str, on: bool) -> dict:
        return await self.post(f"v1/projects/{name}/actions/scale", json={"scale": 1 if on else 0})

    async def resize_app(self, name: str, plan_id: str) -> dict:
        return await self.post(f"v1/projects/{name}/resize", json={"planID": plan_id})

    async def update_envs(self, name: str, variables: list[dict]) -> dict:
        """NOTE: replaces the whole env list (the CLI fetches existing envs and appends)."""
        return await self.post("v1/projects/update-envs", json={"project": name, "variables": variables})

    async def update_port(self, name: str, port: int) -> dict:
        return await self.post("v1/projects/update-port", json={"project": name, "port": port})

    async def set_setting(self, project_id: str, setting: str, enabled: bool) -> dict:
        # setting in {"zero-downtime", "default-subdomain", "fixed-ip", "read-only"}
        status = "enable" if enabled else "disable"
        return await self.post(f"v1/projects/{project_id}/{setting}/{status}")

    # --------------------------------------------------------------- deploy
    async def upload_source(self, name: str, archive: Path) -> str:
        with open(archive, "rb") as fh:
            data = await self.post(
                f"v2/projects/{name}/sources",
                files={"file": (archive.name, fh, "application/gzip")},
                timeout=600,
            )
        return data["sourceID"]

    async def create_release(self, name: str, body: dict) -> str:
        data = await self.post(f"v2/projects/{name}/releases", json=body, timeout=60)
        return data["releaseID"]

    async def build_logs(self, release_id: str, since: str | None = None) -> dict:
        return await self.get(f"v2/releases/{release_id}/build-logs", params={"since": since or ""})

    async def get_release(self, release_id: str) -> dict:
        return await self.get(f"v1/releases/{release_id}")

    async def cancel_release(self, release_id: str) -> dict:
        return await self.post(f"v2/releases/{release_id}/cancel")

    async def list_releases(self, name: str, page: int = 1, count: int = 10) -> dict:
        return await self.get(f"v1/projects/{name}/releases", params={"page": page, "count": count})

    async def rollback(self, name: str, release_id: str) -> dict:
        # Not in the official OpenAPI spec; used by a community MCP server. Verify before relying on it.
        return await self.post(f"v2/projects/{name}/releases/{release_id}/rollback")

    # -------------------------------------------------------- observability
    async def logs(self, name: str, start_unix: int, last_lines: bool = False,
                   release_id: str | None = None) -> dict:
        params = {"start": start_unix, "direction": "forward", "last_lines": str(last_lines).lower(),
                  "releaseId": release_id}
        return await self.get(f"v2/projects/{name}/logs", params=params, timeout=25)

    async def metrics_summary(self, name: str) -> dict:
        return await self.get(f"v1/projects/{name}/metrics/summary")

    async def metrics_series(self, name: str, kind: str, since_unix: int) -> dict:
        # kind in {"cpu", "memory", "network-receive", "network-transmit"}
        return await self.get(f"v1/projects/{name}/metrics/{kind}", params={"since": since_unix})

    async def events(self, name: str, start_unix: int | None = None, end_unix: int | None = None,
                     search: str | None = None) -> dict:
        return await self.get(f"v1/projects/{name}/events",
                              params={"start": start_unix, "end": end_unix, "search": search})

    # -------------------------------------------------------------- domains
    async def list_domains(self, project: str | None = None) -> Any:
        return await self.get("v1/domains", params={"project": project})

    async def add_domain(self, domain: str, project: str) -> dict:
        return await self.post("v1/domains", json={"name": domain, "project": project, "type": "PROJECT"})

    async def check_domain(self, domain_id: str) -> dict:
        return await self.get(f"v1/domains/{domain_id}/check")

    async def provision_ssl(self, domain: str) -> dict:
        return await self.post("v1/domains/provision-ssl-certs", json={"domain": domain})

    # ------------------------------------------------------------------ DNS
    async def list_zones(self) -> dict:
        return await self.get("api/v1/zones", dns=True)

    async def create_zone(self, zone: str) -> dict:
        return await self.post("api/v1/zones", json={"name": zone}, dns=True)

    async def get_zone(self, zone: str) -> dict:
        return await self.get(f"api/v1/zones/{zone}", dns=True)

    async def check_zone(self, zone: str) -> dict:
        return await self.put(f"api/v1/zones/{zone}/check", dns=True)

    async def list_records(self, zone: str) -> dict:
        return await self.get(f"api/v1/zones/{zone}/dns-records", dns=True)

    async def create_record(self, zone: str, record: dict) -> dict:
        return await self.post(f"api/v1/zones/{zone}/dns-records", json=record, dns=True)


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(data, dict):
        for key in ("message", "error", "msg", "data"):
            if data.get(key):
                return str(data[key])[:300]
    return str(data)[:300]
