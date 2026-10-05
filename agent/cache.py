"""Two-level answer cache in Postgres: exact (sha256 key) then semantic (pgvector cosine distance)."""

import hashlib

from django.conf import settings
from django.db.models import F
from pgvector.django import CosineDistance

from .core.guards import same_constraints
from .core.llm import MODEL
from .core.prompts import PROMPT_VERSION
from .models import CacheEntry


def cache_key(question: str, client_id: int, data_version: str) -> str:
    raw = f"{question.strip().lower()}|{client_id}|{data_version}|{PROMPT_VERSION}|{MODEL}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _hit(entry: CacheEntry) -> dict:
    CacheEntry.objects.filter(pk=entry.pk).update(hit_count=F("hit_count") + 1)
    return entry.response


def cache_get_exact(key: str) -> dict | None:
    entry = CacheEntry.objects.filter(pk=key).first()
    return _hit(entry) if entry else None


def cache_get_semantic(client_id: int, question: str, vector: list[float], data_version: str) -> dict | None:
    """Nearest cached question for the SAME client, data version, prompt and model. Accepted only if it is
    within the distance threshold AND passes the constraint guard (numbers, dates, metrics, dimensions match)."""
    entry = (
        CacheEntry.objects.filter(client_id=client_id, data_version=data_version,
                                  prompt_version=PROMPT_VERSION, model=MODEL)
        .exclude(embedding=None)
        .annotate(distance=CosineDistance("embedding", vector))
        .filter(distance__lte=settings.SEMANTIC_CACHE_MAX_DISTANCE)
        .order_by("distance")
        .first()
    )
    # ponytail: sequential scan per client; add an HNSW index when the table passes ~10k rows.
    return _hit(entry) if entry and same_constraints(question, entry.question) else None


def cache_set(key: str, client_id: int, question: str, vector: list[float], data_version: str, response: dict):
    CacheEntry.objects.update_or_create(key=key, defaults={
        "client_id": client_id, "question": question, "embedding": vector, "data_version": data_version,
        "prompt_version": PROMPT_VERSION, "model": MODEL, "response": response,
    })
