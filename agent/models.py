from django.conf import settings
from django.db import models
from pgvector.django import VectorField

from data.models import Client


class QueryLog(models.Model):
    """One row per /api/ask request: the app's own monitoring table (also feeds the eval set)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    client = models.ForeignKey(Client, null=True, on_delete=models.SET_NULL)
    question = models.TextField()
    sql = models.TextField(blank=True)
    status = models.CharField(max_length=16)  # ok | clarify | rejected | error | blocked
    error = models.TextField(blank=True)
    answer = models.TextField(blank=True)
    retries = models.SmallIntegerField(default=0)
    latency_ms = models.IntegerField(default=0)
    timings = models.JSONField(default=dict)
    input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    cost_usd = models.FloatField(default=0)
    cache_hit = models.CharField(max_length=8, blank=True)  # "" | exact | semantic
    flagged = models.BooleanField(default=False)  # question looked like prompt injection
    feedback = models.SmallIntegerField(null=True, blank=True)  # -1 / 1
    model = models.CharField(max_length=64, blank=True)
    prompt_version = models.CharField(max_length=16, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)


class CacheEntry(models.Model):
    key = models.CharField(max_length=64, primary_key=True)  # sha256 for exact match
    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    question = models.TextField()
    embedding = VectorField(dimensions=1536, null=True)  # for semantic match
    data_version = models.CharField(max_length=40)
    prompt_version = models.CharField(max_length=16)
    model = models.CharField(max_length=64)
    response = models.JSONField()
    hit_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
