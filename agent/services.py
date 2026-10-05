"""ask(): everything around the agent graph - access, budget, cache, logging, tracing."""

import logging
import time

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from data.models import Client

from .cache import cache_get_exact, cache_get_semantic, cache_key, cache_set
from .core.graph import answer_question
from .core.guards import looks_like_injection
from .core.llm import MODEL, add_usage, callbacks, embed
from .core.prompts import PROMPT_VERSION
from .models import QueryLog

HTTP = {"ok": 200, "clarify": 200, "rejected": 422, "error": 500, "blocked": 429}
CACHED_FIELDS = ("status", "answer", "clarifying_question", "sql", "columns", "rows", "truncated")
log_ = logging.getLogger(__name__)


def user_clients(user):
    qs = Client.objects.filter(is_active=True)
    return qs if user.is_superuser else qs.filter(users=user)


def spent_today(user) -> float:
    today = timezone.now().date()
    return QueryLog.objects.filter(user=user, created_at__date=today).aggregate(s=Sum("cost_usd"))["s"] or 0.0


def ask(user, client_id: int, question: str) -> tuple[dict, int]:
    t0 = time.perf_counter()
    question = " ".join(question.split())
    client = user_clients(user).filter(pk=client_id).first()
    if not client:  # authorization happens before any cache or LLM work
        return {"status": "error", "error": "No access to this client."}, 403

    log = QueryLog(user=user, client=client, question=question, model=MODEL, prompt_version=PROMPT_VERSION,
                   flagged=looks_like_injection(question))
    if spent_today(user) >= settings.DAILY_COST_CAP_USD:
        log.status, log.error = "blocked", "daily cost cap reached"
        log.save()
        return {"status": "blocked", "error": "Daily budget reached. Try again tomorrow."}, 429

    data_version = client.last_synced_at.isoformat() if client.last_synced_at else "never"
    key = cache_key(question, client.id, data_version)
    usage, vector, kind = {}, None, ""
    try:
        hit = cache_get_exact(key)
        if hit:
            kind = "exact"
        else:
            vector, usage = embed(question)  # one embedding, reused for the semantic cache AND few-shot retrieval
            hit = cache_get_semantic(client.id, question, vector, data_version)
            kind = "semantic" if hit else ""

        if hit:
            result = {**hit, "retries": 0, "timings": {}, "error": ""}
        else:
            result = answer_question(question, client.id, vector, callbacks=callbacks(),
                                     metadata={"langfuse_user_id": user.get_username(), "client": client.slug,
                                               "prompt_version": PROMPT_VERSION})
            usage = add_usage(usage, result["usage"])
            if result["status"] == "ok" and not log.flagged:  # write-side guard: only clean successes are cached
                cache_set(key, client.id, question, vector, data_version, {k: result[k] for k in CACHED_FIELDS})
    except Exception as e:  # LLM provider down, timeout, bad key: log it, answer with JSON, never a stack trace
        log_.exception("ask failed")
        result = {"status": "error", "error": f"AI service error: {type(e).__name__}", "answer": "", "sql": "",
                  "clarifying_question": "", "columns": [], "rows": [], "truncated": False, "retries": 0, "timings": {}}

    log.status, log.sql, log.answer, log.error = result["status"], result["sql"], result["answer"], result["error"]
    log.retries, log.timings, log.cache_hit = result["retries"], result["timings"], kind
    log.input_tokens, log.output_tokens = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    log.cost_usd = usage.get("cost_usd", 0.0)
    log.latency_ms = round((time.perf_counter() - t0) * 1000)
    log.save()

    payload = {"query_id": log.id, **{k: result.get(k) for k in CACHED_FIELDS}, "error": result["error"],
               "meta": {"latency_ms": log.latency_ms, "cost_usd": round(log.cost_usd, 6), "cache_hit": kind,
                        "retries": log.retries, "timings": log.timings,
                        "data_synced_at": client.last_synced_at, "currency": client.currency_code}}
    return payload, HTTP[result["status"]]
