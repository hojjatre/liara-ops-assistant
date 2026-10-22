"""LLM factory: BoxAPI (OpenAI-compatible) via langchain-openai, with model fallbacks."""
from __future__ import annotations

from langchain_openai import ChatOpenAI


def chat_model(settings, model: str) -> ChatOpenAI:
    return ChatOpenAI(
        model=model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=120,
        max_retries=2,
        use_responses_api=False,  # BoxAPI documents /chat/completions
    )


def make_llm(settings, tools: list[dict]):
    primary = chat_model(settings, settings.llm_model).bind_tools(tools)
    fallbacks = [chat_model(settings, m).bind_tools(tools)
                 for m in settings.llm_fallback_models if m and m != settings.llm_model]
    return primary.with_fallbacks(fallbacks) if fallbacks else primary
