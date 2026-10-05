from django.contrib import admin
from django.db.models import Avg, Count, Sum

from .models import CacheEntry, QueryLog


@admin.register(QueryLog)
class QueryLogAdmin(admin.ModelAdmin):
    """The built-in monitoring dashboard: filter by status/feedback, read SQL, see cost and latency."""

    list_display = ("created_at", "user", "client", "question", "status", "cache_hit", "retries",
                    "latency_ms", "cost_usd", "feedback", "flagged")
    list_filter = ("status", "cache_hit", "feedback", "flagged", "client", "prompt_version")
    search_fields = ("question", "sql", "error")
    readonly_fields = [f.name for f in QueryLog._meta.fields]
    date_hierarchy = "created_at"

    def changelist_view(self, request, extra_context=None):
        response = super().changelist_view(request, extra_context)
        try:
            qs = response.context_data["cl"].queryset
            stats = qs.aggregate(n=Count("id"), cost=Sum("cost_usd"), latency=Avg("latency_ms"))
            response.context_data["title"] = (
                f"Queries: {stats['n']} · total cost ${stats['cost'] or 0:.4f} · avg latency {stats['latency'] or 0:.0f} ms")
        except (AttributeError, KeyError):
            pass
        return response


@admin.register(CacheEntry)
class CacheEntryAdmin(admin.ModelAdmin):
    list_display = ("question", "client", "hit_count", "prompt_version", "data_version", "created_at")
    list_filter = ("client", "prompt_version")
    exclude = ("embedding",)
