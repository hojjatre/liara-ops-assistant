"""Settings loaded from environment variables (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    liara_api_token: str = ""
    liara_team_id: str = ""
    liara_api_url: str = "https://api.liara.ir"
    liara_dns_url: str = "https://dns-service.iran.liara.ir"
    llm_base_url: str = "https://ai.boxapi.ir/ai/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-5.6-sol"
    llm_fallback_models: list[str] = field(default_factory=list)
    monitor_interval_sec: int = 120
    audit_log_path: str = "data/audit.jsonl"
    default_app: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            liara_api_token=os.getenv("LIARA_API_TOKEN", "").strip(),
            liara_team_id=os.getenv("LIARA_TEAM_ID", "").strip(),
            liara_api_url=os.getenv("LIARA_API_URL", cls.liara_api_url).rstrip("/"),
            liara_dns_url=os.getenv("LIARA_DNS_URL", cls.liara_dns_url).rstrip("/"),
            llm_base_url=os.getenv("LLM_BASE_URL", cls.llm_base_url).rstrip("/"),
            llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
            llm_model=os.getenv("LLM_MODEL", cls.llm_model).strip(),
            llm_fallback_models=_list(os.getenv("LLM_FALLBACK_MODELS", "gpt-5.6-terra,gpt-5-mini")),
            monitor_interval_sec=int(os.getenv("MONITOR_INTERVAL_SEC", "120")),
            audit_log_path=os.getenv("AUDIT_LOG_PATH", cls.audit_log_path),
            default_app=os.getenv("DEFAULT_APP", "").strip(),
        )

    def missing(self) -> list[str]:
        out = []
        if not self.liara_api_token:
            out.append("LIARA_API_TOKEN")
        if not self.llm_api_key:
            out.append("LLM_API_KEY")
        return out
