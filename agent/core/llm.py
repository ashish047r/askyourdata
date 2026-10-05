"""Model clients, embeddings, token cost and (optional) Langfuse tracing. No Django imports."""

import os
import re
from functools import lru_cache

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
EMBED_MODEL = "text-embedding-3-small"

# USD per 1M tokens (input, output). Verify at openai.com/api/pricing when changing models.
PRICES = {
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-4o-mini": (0.15, 0.60),
    EMBED_MODEL: (0.02, 0.0),
}
assert MODEL in PRICES, f"Add a price for OPENAI_MODEL={MODEL} in agent/core/llm.py PRICES"


@lru_cache
def chat_model(max_tokens: int) -> ChatOpenAI:
    """Reasoning models (gpt-5 family) reject a custom temperature; we control them with
    reasoning_effort + structured output instead. max_tokens includes hidden reasoning tokens."""
    effort = os.getenv("OPENAI_REASONING_EFFORT", "low")
    return ChatOpenAI(model=MODEL, max_tokens=max_tokens, timeout=45, max_retries=2,
                      **({"reasoning_effort": effort} if effort else {}))


@lru_cache
def embeddings() -> OpenAIEmbeddings:
    return OpenAIEmbeddings(model=EMBED_MODEL)


def embed(text: str) -> tuple[list[float], dict]:
    """One embedding per question, reused by the semantic cache AND few-shot example selection."""
    # ponytail: token count estimated (~4 chars/token); the embeddings API usage isn't surfaced by LangChain.
    return embeddings().embed_query(text), usage(EMBED_MODEL, max(1, len(text) // 4), 0)


def usage(model: str, input_tokens: int, output_tokens: int) -> dict:
    p_in, p_out = PRICES[model]
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "cost_usd": (input_tokens * p_in + output_tokens * p_out) / 1_000_000}


def usage_from(raw_message) -> dict:
    u = getattr(raw_message, "usage_metadata", None) or {}
    return usage(MODEL, u.get("input_tokens", 0), u.get("output_tokens", 0))


def add_usage(a: dict, b: dict) -> dict:
    """Sum two {key: number} dicts. Used as the LangGraph reducer for usage and timings."""
    return {k: (a or {}).get(k, 0) + (b or {}).get(k, 0) for k in {*(a or {}), *(b or {})}}


# ---------- Langfuse tracing (enabled only when LANGFUSE_PUBLIC_KEY is set) ----------

_DATA_BLOCK = re.compile(r"<(data|result)>.*?</\1>", re.S)


def mask(*, data, **_):
    """Client analytics never leave our server in traces: query results and vectors are masked."""
    if isinstance(data, str):
        return _DATA_BLOCK.sub(r"<\1>[masked]</\1>", data)
    if isinstance(data, dict):
        return {k: "[masked]" if k in ("rows", "question_vector") else mask(data=v) for k, v in data.items()}
    if isinstance(data, list):
        return [mask(data=v) for v in data]
    return data


@lru_cache
def _langfuse_enabled() -> bool:
    if not os.getenv("LANGFUSE_PUBLIC_KEY"):
        return False
    from langfuse import Langfuse

    Langfuse(mask=mask)  # registers the singleton the CallbackHandler uses
    return True


def callbacks() -> list:
    if not _langfuse_enabled():
        return []
    from langfuse.langchain import CallbackHandler

    return [CallbackHandler()]


def flush_traces():
    if _langfuse_enabled():
        from langfuse import get_client

        get_client().flush()
